import sys
import numpy as np
from tqdm import tqdm
import h5py
import glob
from torch.utils.data import IterableDataset, DataLoader
import os
import cv2
import math
import torch
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa
from utils import bgr_to_yuv, transform_frames, printf  # noqa


MIN_SEGMENT_LENGTH = 1190

cache_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
path_to_videos_cache = os.path.join(cache_folder, 'videos.txt')
path_to_plans_cache = os.path.join(cache_folder, 'plans.txt')


class CommaLoader(IterableDataset):

    def __init__(self, recordings_basedir, train_split=0.8, seq_len=32, validation=False, shuffle=False, seed=42, single_frame_batches=False):
        super(CommaLoader, self).__init__()
        """
        Dataloader for Comma model train. pipeline

        Summary:
            This dataloader can be used for intial testing and for proper training
            Images are converted into YUV 4:2:0 channels and brought to a calib frame of reff
            as used in the official comma pipeline.

        Args: ------------------
        """
        self.recordings_basedir = recordings_basedir
        self.validation = validation
        self.train_split = train_split
        self.shuffle = shuffle
        self.seq_len = seq_len
        self.seed = seed
        self.single_frame_batches = single_frame_batches

        if self.recordings_basedir is None or not os.path.exists(self.recordings_basedir):
            raise TypeError("recordings path is wrong")

        self.hevc_file_paths, self.gt_file_paths = get_paths(self.recordings_basedir, min_segment_len=MIN_SEGMENT_LENGTH)
        n_segments = len(self.hevc_file_paths)
        printf("# of segments", n_segments)

        full_index = list(range(n_segments))

        # shuffle full train+val together *once*
        if self.shuffle:
            rng = np.random.default_rng(seed)
            rng.shuffle(full_index)

        split_idx = int(np.round(n_segments * self.train_split))

        if not self.validation:
            self.segment_indices = full_index[:split_idx]
        else:
            self.segment_indices = full_index[split_idx:]

        printf("segments ready to load:", len(self.segment_indices))

    def __len__(self):
        return len(self.segment_indices)

    def __iter__(self):

        # shuffle data subset after each epoch
        if self.shuffle:
            rng = np.random.default_rng(self.seed)
            rng.shuffle(self.segment_indices)

        # support single & multi-processing
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:
            worker_id = 0
            num_workers = 1
        else:
            worker_id = worker_info.id
            num_workers = worker_info.num_workers

        for segment_vidx in range(worker_id, len(self.segment_indices), num_workers):

            # retrieve true index of segment [0, 2331] using virtual index [0, 2331 * train_split]
            segment_idx = self.segment_indices[segment_vidx]

            segment_video = cv2.VideoCapture(self.hevc_file_paths[segment_idx])
            segment_gts = h5py.File(self.gt_file_paths[segment_idx], 'r')
            segment_length = segment_gts['plans'].shape[0]
            n_seqs = math.floor(segment_length / self.seq_len)

            _, frame2 = segment_video.read()  # initialize last frame
            yuv_frame2 = bgr_to_yuv(frame2)


            for sequence_idx in range(n_seqs):

                segment_finished = sequence_idx == n_seqs-1

                if not self.single_frame_batches:
                    stacked_frame_seq = np.zeros((self.seq_len, 12, 128, 256), dtype=np.uint8)

                # start iteration from 1 because we already read 1 frame before
                for t_idx in range(1, self.seq_len):

                    sequence_finished = t_idx == self.seq_len-1

                    yuv_frame1 = yuv_frame2
                    _, frame2 = segment_video.read()

                    yuv_frame2 = bgr_to_yuv(frame2)
                    prepared_frames = transform_frames([yuv_frame1, yuv_frame2])
                    stacked_frames = np.vstack(prepared_frames).reshape(12, 128, 256)

                    if self.single_frame_batches:
                        abs_t_idx = sequence_idx*self.seq_len + t_idx
                        gt_plan = segment_gts['plans'][abs_t_idx]
                        gt_plan_prob = segment_gts['plans_prob'][abs_t_idx]

                        yield stacked_frames, gt_plan, gt_plan_prob, segment_finished, sequence_finished
                    else:
                        stacked_frame_seq[t_idx-1, ...] = stacked_frames

                if not self.single_frame_batches:
                    # shift slice by +1 to skip the 1st step which didn't see 2 stacked frames yet
                    abs_t_indices = slice(sequence_idx*self.seq_len+1, (sequence_idx+1)*self.seq_len+1)
                    gt_plan_seq = segment_gts['plans'][abs_t_indices]
                    gt_plan_prob_seq = segment_gts['plans_prob'][abs_t_indices]

                    # yield stacked_frame_seq, gt_plan_seq, gt_plan_prob_seq, segment_finished, True
                    print(f'worker: {worker_id}, segment: {segment_idx}, seq: {sequence_idx}')
                    yield segment_idx, sequence_idx, segment_finished, True, True

            segment_gts.close()

        return


def get_segment_dirs(base_dir):
    '''Get paths to segments that have ground truths.'''

    gt_files = sorted(glob.glob(base_dir + "/**/marker_and_leads_ground_truth.npz", recursive=True))
    return sorted(list(set([os.path.dirname(f) for f in gt_files])))


def get_paths(base_dir, min_segment_len=1190):
    '''Get paths to videos and ground truths. Cache them for future reuse.'''

    os.makedirs(cache_folder, exist_ok=True)

    if os.path.exists(path_to_videos_cache) and os.path.exists(path_to_plans_cache):
        printf('Using cached paths to videos and GTs...')
        video_paths = []
        gt_paths = []
        with open(path_to_videos_cache, 'r') as f:
            video_paths = f.read().splitlines()
        with open(path_to_plans_cache, 'r') as f:
            gt_paths = f.read().splitlines()
    else:
        printf('Resolving paths to videos and GTs...')
        segment_dirs = get_segment_dirs(base_dir)

        # prevent duplicate writes
        with open(path_to_videos_cache, 'w') as video_paths:
            pass
        with open(path_to_plans_cache, 'w') as gt_paths:
            pass

        gt_filename = 'plan.h5'
        video_filenames = ['fcamera.hevc', 'video.hevc']

        video_paths = []
        gt_paths = []

        for segment_dir in tqdm(segment_dirs):

            gt_file_path = os.path.join(segment_dir, gt_filename)
            if not os.path.exists(gt_file_path):
                printf(f'WARNING: not found plan.h5 file in segment: {segment_dir}')
                continue

            gt_plan = h5py.File(gt_file_path, 'r')['plans']

            if gt_plan.shape[0] >= min_segment_len:  # keep segments that have >= 1190 samples

                video_files = os.listdir(segment_dir)
                video_files = [file for file in video_files if file in video_filenames]

                found_one_video = 0 <= len(video_files) <= 1

                if found_one_video:
                    with open(path_to_videos_cache, 'a') as video_paths_f:
                        video_path = os.path.join(segment_dir, video_files[0])
                        video_paths.append(video_path)
                        video_paths_f.write(video_path + '\n')  # cache it

                    with open(path_to_plans_cache, 'a') as gt_paths_f:
                        gt_paths.append(gt_file_path)
                        gt_paths_f.write(gt_file_path + '\n')  # cache it
                else:
                    printf(f'WARNING: found {len(video_files)} in segment: {segment_dir}')

    return video_paths, gt_paths


class BatchDataLoader:
    def __init__(self, loader, batch_size):
        self.loader = loader
        self.batch_size = batch_size

    def __iter__(self):
        batch = []
        for d in self.loader:
            batch.append(d)
            if len(batch) == self.batch_size:
                yield self.collate_fn(batch)
                batch.clear()
        if len(batch) > 0:
            yield self.collate_fn(batch)

    def collate_fn(self, batch):

        stacked_frames = torch.stack([item[0] for item in batch])
        gt_plan = torch.stack([item[1] for item in batch])
        gt_plan_prob = torch.stack([item[2] for item in batch])
        segment_finished = torch.stack([item[3] for item in batch])
        sequence_finished = torch.stack([item[4] for item in batch])

        return stacked_frames, gt_plan, gt_plan_prob, segment_finished, sequence_finished

    def __len__(self):
        return len(self.loader)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        comma_recordings_basedir = sys.argv[1]
    else:
        comma_recordings_basedir = "/gpfs/space/projects/Bolt/comma_recordings"


    # (ONLY FOR sequence batches) 
    # num_workers must be the same as `batch_size` for data loader to process different segments 
    # at the same rate (a forward path takes in input at the same step I in all M segments, instead of steps I±epsilon)
    num_workers = 30
    seq_len = 100
    simulated_forward_time = 1.000  # 100 milli-seconds for a single-frame batch (M, 1, 12, 128, 256)
    batch_size = 10
    train_split = 0.8
    single_frame_batches = False

    train_dataset = CommaLoader(comma_recordings_basedir, train_split=train_split, seq_len=seq_len, shuffle=True, single_frame_batches=single_frame_batches)

    # hack to get batches of different segments with many workers
    train_loader = DataLoader(train_dataset, batch_size=1, num_workers=num_workers, shuffle=False, prefetch_factor=8)
    train_loader = BatchDataLoader(train_loader, batch_size=batch_size)

    printf("checking the shapes of the loader outs")
    prev_time = time.time()
    for idx, batch in enumerate(train_loader):
        # frames, plans, plans_probs, segment_finished, sequence_finished = batch
        segment, sequence, segment_finished, sequence_finished, _ = batch

        if idx == 0:
            new_time = time.time()
            time_delta = new_time - prev_time - simulated_forward_time
            printf('Giving 20s to pre-fetch...')
            time.sleep(20)

            # recalculate
            new_time = time.time()
            prev_time = new_time
        else:
            new_time = time.time()
            time_delta = new_time - prev_time - simulated_forward_time
            prev_time = new_time

        printf(f'{time_delta:.3f}s - Segments: {torch.unique(segment)}. Plans: {torch.unique(sequence)}. Segment finished: {torch.unique(segment_finished)}. Sequence finished: {torch.unique(sequence_finished)}')
        # printf(f'{time_delta:.3f}s - Frames: {frames.shape}. Plans: {plans.shape}. Plan probs: {plans_probs.shape}. Segment finished: {segment_finished.shape}. Sequence finished: {sequence_finished.shape}')

        time.sleep(simulated_forward_time)

        if idx > 100:
            break

