#!/usr/bin/env python3
"""Minimal, self-contained LeRobot v3.0 dataset writer.

Replicates the on-disk layout produced by lerobot 0.4.4 (CODEBASE_VERSION
"v3.0") without importing lerobot or torch:

    <root>/meta/info.json
    <root>/meta/stats.json
    <root>/meta/tasks.parquet
    <root>/meta/episodes/chunk-000/file-000.parquet
    <root>/data/chunk-000/file-000.parquet
    <root>/videos/<video_key>/chunk-000/file-000.mp4

One episode is stored per file, which the format permits. Frames are spooled
as JPEG so a long episode does not have to sit in RAM; the video is encoded
with ffmpeg and statistics are computed from a reservoir sample of frames.
"""

import json
import math
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

CODEBASE_VERSION = "v3.0"
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_DATA_FILE_SIZE_IN_MB = 100
DEFAULT_VIDEO_FILE_SIZE_IN_MB = 200
QUANTILES = [0.01, 0.10, 0.50, 0.90, 0.99]
QUANTILE_KEYS = [f"q{int(q * 100):02d}" for q in QUANTILES]
MAX_STAT_SAMPLES = 100
STAT_DOWNSAMPLE = 150

DATA_PATH = "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"
VIDEO_PATH = "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"
EPISODES_PATH = "meta/episodes/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet"

DEFAULT_FEATURES = {
    "timestamp": {"dtype": "float32", "shape": [1], "names": None},
    "frame_index": {"dtype": "int64", "shape": [1], "names": None},
    "episode_index": {"dtype": "int64", "shape": [1], "names": None},
    "index": {"dtype": "int64", "shape": [1], "names": None},
    "task_index": {"dtype": "int64", "shape": [1], "names": None},
}


def chunk_file(index, chunks_size=DEFAULT_CHUNK_SIZE):
    return index // chunks_size, index % chunks_size


def _flatten(d, parent="", sep="/"):
    out = {}
    for key, value in d.items():
        full = f"{parent}{sep}{key}" if parent else key
        if isinstance(value, dict):
            out.update(_flatten(value, full, sep))
        else:
            out[full] = value
    return out


def _numeric_stats(values):
    """Statistics for a (n,) or (n, dim) array, matching compute_episode_stats."""
    values = np.asarray(values)
    scalar = values.ndim == 1
    if scalar:
        values = values.reshape(-1, 1)
    count = values.shape[0]
    stats = {
        "min": np.min(values, axis=0),
        "max": np.max(values, axis=0),
        "mean": np.mean(values, axis=0),
        "std": np.std(values, axis=0),
        "count": np.array([count]),
    }
    if count < 2:
        for key in QUANTILE_KEYS:
            stats[key] = stats["mean"].copy()
    else:
        for q, key in zip(QUANTILES, QUANTILE_KEYS):
            stats[key] = np.quantile(values, q, axis=0)
    if not scalar:
        stats = {k: (v if k == "count" else np.asarray(v).reshape(-1)) for k, v in stats.items()}
    return stats


def _image_stats(frames_rgb):
    """Per-channel stats normalized to [0,1], shape (3,1,1), from RGB frames."""
    array = np.stack(frames_rgb).astype(np.float32) / 255.0
    count = array.shape[0]
    flat = array.reshape(-1, array.shape[-1])
    stats = {
        "min": flat.min(axis=0),
        "max": flat.max(axis=0),
        "mean": flat.mean(axis=0),
        "std": flat.std(axis=0),
        "count": np.array([count]),
    }
    if count < 2:
        for key in QUANTILE_KEYS:
            stats[key] = stats["mean"].copy()
    else:
        for q, key in zip(QUANTILES, QUANTILE_KEYS):
            stats[key] = np.quantile(flat, q, axis=0)
    return {k: (v if k == "count" else np.asarray(v).reshape(3, 1, 1)) for k, v in stats.items()}


def aggregate_feature_stats(stats_list):
    """Weighted aggregation identical to lerobot's aggregate_feature_stats."""
    means = np.stack([s["mean"] for s in stats_list]).astype(np.float64)
    variances = np.stack([s["std"] ** 2 for s in stats_list]).astype(np.float64)
    counts = np.stack([s["count"] for s in stats_list]).astype(np.float64)
    total_count = counts.sum(axis=0)
    while counts.ndim < means.ndim:
        counts = np.expand_dims(counts, axis=-1)
    total_mean = (means * counts).sum(axis=0) / total_count
    delta = means - total_mean
    total_variance = (((variances + delta ** 2) * counts).sum(axis=0)) / total_count
    aggregated = {
        "min": np.min(np.stack([s["min"] for s in stats_list]), axis=0),
        "max": np.max(np.stack([s["max"] for s in stats_list]), axis=0),
        "mean": total_mean,
        "std": np.sqrt(total_variance),
        "count": total_count,
    }
    for key in QUANTILE_KEYS:
        if all(key in s for s in stats_list):
            values = np.stack([s[key] for s in stats_list])
            aggregated[key] = (values * counts).sum(axis=0) / total_count
    return aggregated


def aggregate_stats(stats_list):
    keys = {key for stats in stats_list for key in stats}
    return {key: aggregate_feature_stats([s[key] for s in stats_list if key in s])
            for key in keys}


def _serialize_stats(stats):
    out = {}
    for feature, values in stats.items():
        out[feature] = {key: np.asarray(value).tolist() for key, value in values.items()}
    return out


def _resize_for_stats(frame_rgb):
    height, width = frame_rgb.shape[:2]
    scale = STAT_DOWNSAMPLE / max(height, width)
    if scale >= 1.0:
        return frame_rgb
    return cv2.resize(frame_rgb, (max(1, int(width * scale)), max(1, int(height * scale))),
                      interpolation=cv2.INTER_AREA)


def _table_from_columns(columns):
    """Build a pyarrow table with the Arrow types lerobot expects."""
    arrays = []
    names = []
    for name, values in columns.items():
        if name in ("observation.pose", "action"):
            arrays.append(pa.array([list(map(float, v)) for v in values], type=pa.list_(pa.float32())))
        elif name == "timestamp":
            arrays.append(pa.array([float(v) for v in values], type=pa.float32()))
        elif name in ("observation.state", "observation.valid"):
            arrays.append(pa.array([float(v[0] if isinstance(v, (list, tuple, np.ndarray)) else v)
                                    for v in values], type=pa.float32()))
        else:
            arrays.append(pa.array([int(v) for v in values], type=pa.int64()))
        names.append(name)
    return pa.Table.from_arrays(arrays, names=names)


class EpisodeRecorder:
    def __init__(self, writer):
        self.writer = writer
        self.episode_index = writer.total_episodes
        self.length = 0
        self.rows = {key: [] for key in writer.numeric_keys}
        self.task_votes = {}
        self.qc = []
        self._reservoir = []
        self._seen = 0
        self._tmp = Path(tempfile.mkdtemp(dir=writer.root, prefix=".episode-"))
        self._frame_dir = self._tmp / "frames"
        self._frame_dir.mkdir(parents=True)
        self._finished = False

    def add_frame(self, values, frame_bgr, task, qc=None):
        """Append one synchronized sample.

        `values` holds the numeric feature vectors (observation.* and action).
        `frame_bgr` is an HxWx3 uint8 frame in OpenCV BGR order. `qc` is an
        optional per-frame diagnostics dict stored alongside the episode.
        """
        if self._finished:
            raise RuntimeError("episode already finished")
        for key in self.writer.numeric_keys:
            self.rows[key].append(values[key])
        self.qc.append(qc or {})
        self.task_votes[task] = self.task_votes.get(task, 0) + 1
        cv2.imwrite(str(self._frame_dir / f"frame-{self.length:06d}.jpg"), frame_bgr,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        sample = _resize_for_stats(rgb)
        self._seen += 1
        if len(self._reservoir) < MAX_STAT_SAMPLES:
            self._reservoir.append(sample)
        else:
            j = random.randrange(self._seen)
            if j < MAX_STAT_SAMPLES:
                self._reservoir[j] = sample
        self.length += 1

    def _encode_video(self, video_key, fps):
        (chunk, file) = chunk_file(self.episode_index, self.writer.chunks_size)
        target = self.writer.root / VIDEO_PATH.format(
            video_key=video_key, chunk_index=chunk, file_index=file)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = self._tmp / "episode.mp4"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", str(fps),
            "-i", str(self._frame_dir / "frame-%06d.jpg"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-an",
            str(temp),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:400]}")
        shutil.move(str(temp), str(target))
        return chunk, file

    def finish(self):
        """Write data parquet, video, metadata and stats for this episode."""
        if self._finished:
            raise RuntimeError("episode already finished")
        self._finished = True
        if self.length == 0:
            shutil.rmtree(self._tmp, ignore_errors=True)
            return None
        writer = self.writer
        chunk, file = chunk_file(self.episode_index, writer.chunks_size)
        from_index = writer.total_frames
        to_index = from_index + self.length

        # numeric frame table
        columns = {
            "timestamp": [i / writer.fps for i in range(self.length)],
            "frame_index": list(range(self.length)),
            "episode_index": [self.episode_index] * self.length,
            "index": list(range(from_index, to_index)),
            "task_index": [writer.task_index(self._primary_task())] * self.length,
        }
        for key in writer.numeric_keys:
            columns[key] = self.rows[key]
        table = _table_from_columns(columns)
        data_file = writer.root / DATA_PATH.format(chunk_index=chunk, file_index=file)
        data_file.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data_file, compression="snappy", use_dictionary=True)

        # video
        video_meta = {}
        for video_key in writer.video_keys:
            vchunk, vfile = self._encode_video(video_key, writer.fps)
            video_meta[f"videos/{video_key}/chunk_index"] = vchunk
            video_meta[f"videos/{video_key}/file_index"] = vfile
            video_meta[f"videos/{video_key}/from_timestamp"] = 0.0
            video_meta[f"videos/{video_key}/to_timestamp"] = self.length / writer.fps

        # episode statistics
        episode_stats = {}
        for key, data in columns.items():
            episode_stats[key] = _numeric_stats(np.asarray(data))
        for video_key in writer.video_keys:
            if self._reservoir:
                episode_stats[video_key] = _image_stats(self._reservoir)

        metadata = {
            "episode_index": self.episode_index,
            "tasks": [self._primary_task()],
            "length": self.length,
            "data/chunk_index": chunk,
            "data/file_index": file,
            "dataset_from_index": from_index,
            "dataset_to_index": to_index,
            "meta/episodes/chunk_index": chunk,
            "meta/episodes/file_index": file,
        }
        metadata.update(video_meta)
        metadata.update(_flatten({"stats": episode_stats}))

        meta_file = writer.root / EPISODES_PATH.format(chunk_index=chunk, file_index=file)
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_columns = {}
        for key, value in metadata.items():
            if isinstance(value, np.ndarray):
                value = value.tolist()
            elif isinstance(value, list):
                value = [v.tolist() if isinstance(v, np.ndarray) else v for v in value]
            meta_columns[key] = [value]
        pq.write_table(pa.Table.from_pydict(meta_columns), meta_file,
                       compression="snappy", use_dictionary=True)

        writer.total_episodes += 1
        writer.total_frames += self.length
        writer.write_info()
        writer.episode_stats.append(episode_stats)
        writer.stats = aggregate_stats(writer.episode_stats)
        writer.write_stats()
        shutil.rmtree(self._tmp, ignore_errors=True)
        return {
            "episode_index": self.episode_index,
            "length": self.length,
            "data_file": str(data_file),
            "stats": episode_stats,
        }

    def _primary_task(self):
        if not self.task_votes:
            return ""
        return max(self.task_votes, key=self.task_votes.get)

    def abort(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class LeRobotV30Writer:
    """Writes a v3.0 dataset one episode per parquet/video file."""

    def __init__(self, root, fps, features, robot_type=None,
                 chunks_size=DEFAULT_CHUNK_SIZE,
                 data_file_size_in_mb=DEFAULT_DATA_FILE_SIZE_IN_MB,
                 video_file_size_in_mb=DEFAULT_VIDEO_FILE_SIZE_IN_MB):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fps = int(fps)
        self.robot_type = robot_type
        self.chunks_size = int(chunks_size)
        self.data_file_size_in_mb = data_file_size_in_mb
        self.video_file_size_in_mb = video_file_size_in_mb
        self.features = {**DEFAULT_FEATURES, **features}
        self.numeric_keys = [k for k, ft in self.features.items()
                             if ft["dtype"] not in ("video", "image")
                             and k not in DEFAULT_FEATURES]
        self.video_keys = [k for k, ft in self.features.items()
                           if ft["dtype"] == "video"]
        self.total_episodes = 0
        self.total_frames = 0
        self.tasks = {}
        self.episode_stats = []
        self.stats = None
        self.write_tasks()
        self.write_info()

    def task_index(self, task):
        if task not in self.tasks:
            self.tasks[task] = len(self.tasks)
            self.write_tasks()
        return self.tasks[task]

    def write_tasks(self):
        import pandas as pd
        names = list(self.tasks.keys())
        df = pd.DataFrame({"task_index": list(self.tasks.values())}, index=names)
        path = self.root / "meta" / "tasks.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)

    def write_info(self):
        info = {
            "codebase_version": CODEBASE_VERSION,
            "robot_type": self.robot_type,
            "total_episodes": self.total_episodes,
            "total_frames": self.total_frames,
            "total_tasks": len(self.tasks),
            "chunks_size": self.chunks_size,
            "data_files_size_in_mb": self.data_file_size_in_mb,
            "video_files_size_in_mb": self.video_file_size_in_mb,
            "fps": self.fps,
            "splits": {"train": f"0:{self.total_episodes}"},
            "data_path": DATA_PATH,
            "video_path": VIDEO_PATH if self.video_keys else None,
            "features": {k: dict(v) for k, v in self.features.items()},
        }
        for key in self.video_keys:
            height, width, channels = self.features[key]["shape"]
            info["features"][key]["info"] = {
                "video.height": int(height),
                "video.width": int(width),
                "video.codec": "h264",
                "video.pix_fmt": "yuv420p",
                "video.is_depth_map": False,
                "video.fps": self.fps,
                "video.channels": int(channels),
                "has_audio": False,
            }
        path = self.root / "meta" / "info.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as handle:
            json.dump(info, handle, indent=4, ensure_ascii=False)

    def write_stats(self):
        if self.stats is None:
            return
        path = self.root / "meta" / "stats.json"
        with open(path, "w") as handle:
            json.dump(_serialize_stats(self.stats), handle, indent=4, ensure_ascii=False)

    def begin_episode(self):
        return EpisodeRecorder(self)

    def validate(self):
        return validate_dataset(self.root)


def validate_dataset(root):
    """Check the on-disk dataset against its own info.json."""
    root = Path(root)
    info_path = root / "meta" / "info.json"
    if not info_path.exists():
        return [f"{info_path} missing"]
    info = json.loads(info_path.read_text())
    if str(info.get("codebase_version", "")).startswith("v2"):
        from lerobot_v21 import validate_dataset_v21
        return validate_dataset_v21(root)
    problems = []
    for required in ("codebase_version", "fps", "features", "data_path"):
        if required not in info:
            problems.append(f"info.json missing {required}")
    for relative in ("meta/stats.json", "meta/tasks.parquet"):
        if not (root / relative).exists():
            problems.append(f"missing {relative}")
    episodes = info.get("total_episodes", 0)
    chunks = info.get("chunks_size", DEFAULT_CHUNK_SIZE)
    video_keys = [k for k, v in info.get("features", {}).items() if v["dtype"] == "video"]
    for episode in range(episodes):
        chunk, file = chunk_file(episode, chunks)
        data = root / DATA_PATH.format(chunk_index=chunk, file_index=file)
        meta = root / EPISODES_PATH.format(chunk_index=chunk, file_index=file)
        if not data.exists():
            problems.append(f"missing data file for episode {episode}")
        if not meta.exists():
            problems.append(f"missing episode metadata for episode {episode}")
        for key in video_keys:
            video = root / VIDEO_PATH.format(
                video_key=key, chunk_index=chunk, file_index=file)
            if not video.exists():
                problems.append(f"missing video {key} for episode {episode}")
    return problems
