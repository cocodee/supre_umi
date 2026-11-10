#!/usr/bin/env python3
"""LeRobot v2.1 dataset writer.

Same recording model as ``lerobot_v30`` but the pre-3.0 layout: one parquet and
one mp4 per episode, and JSONL metadata:

    <root>/meta/info.json                 (codebase_version "v2.1", total_chunks,
                                           total_videos, per-feature fps)
    <root>/meta/tasks.jsonl               {"task_index": i, "task": "..."}
    <root>/meta/episodes.jsonl            {"episode_index", "tasks", "length"}
    <root>/meta/episodes_stats.jsonl      {"episode_index", "stats": {...}}
    <root>/meta/stats.json                aggregated stats
    <root>/data/chunk-000/episode_000000.parquet
    <root>/videos/chunk-000/<key>/episode_000000.mp4

The layout is the one ``lerobot/datasets/v30/convert_dataset_v21_to_v30.py``
consumes, so the result can be upgraded to v3.0 with lerobot's own script.
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
import pyarrow.parquet as pq

from lerobot_v30 import (
    DEFAULT_FEATURES,
    MAX_STAT_SAMPLES,
    _image_stats,
    _numeric_stats,
    _resize_for_stats,
    _serialize_stats,
    _table_from_columns,
    aggregate_stats,
)

CODEBASE_VERSION = "v2.1"
DATA_PATH = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
VIDEO_PATH = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
DEFAULT_CHUNK_SIZE = 1000


class EpisodeRecorderV21:
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
        if self._finished:
            raise RuntimeError("episode already finished")
        for key in self.writer.numeric_keys:
            self.rows[key].append(values[key])
        self.qc.append(qc or {})
        self.task_votes[task] = self.task_votes.get(task, 0) + 1
        cv2.imwrite(str(self._frame_dir / f"frame-{self.length:06d}.jpg"), frame_bgr,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        sample = _resize_for_stats(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        self._seen += 1
        if len(self._reservoir) < MAX_STAT_SAMPLES:
            self._reservoir.append(sample)
        else:
            index = random.randrange(self._seen)
            if index < MAX_STAT_SAMPLES:
                self._reservoir[index] = sample
        self.length += 1

    def _primary_task(self):
        return max(self.task_votes, key=self.task_votes.get) if self.task_votes else ""

    def abort(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def finish(self):
        if self._finished:
            raise RuntimeError("episode already finished")
        self._finished = True
        if self.length == 0:
            shutil.rmtree(self._tmp, ignore_errors=True)
            return None
        writer = self.writer
        chunk = self.episode_index // writer.chunks_size
        from_index = writer.total_frames
        to_index = from_index + self.length

        columns = {
            "timestamp": [i / writer.fps for i in range(self.length)],
            "frame_index": list(range(self.length)),
            "episode_index": [self.episode_index] * self.length,
            "index": list(range(from_index, to_index)),
            "task_index": [writer.task_index(self._primary_task())] * self.length,
        }
        for key in writer.numeric_keys:
            columns[key] = self.rows[key]
        data_file = writer.root / DATA_PATH.format(
            episode_chunk=chunk, episode_index=self.episode_index)
        data_file.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(_table_from_columns(columns), data_file,
                       compression="snappy", use_dictionary=True)

        for video_key in writer.video_keys:
            video_file = writer.root / VIDEO_PATH.format(
                episode_chunk=chunk, episode_index=self.episode_index, video_key=video_key)
            video_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self._tmp / f"{video_key.replace('.', '_')}.mp4"
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", str(writer.fps),
                "-i", str(self._frame_dir / "frame-%06d.jpg"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-an",
                str(temp),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:400]}")
            shutil.move(str(temp), str(video_file))

        episode_stats = {}
        for key, data in columns.items():
            episode_stats[key] = _numeric_stats(np.asarray(data))
        for video_key in writer.video_keys:
            if self._reservoir:
                episode_stats[video_key] = _image_stats(self._reservoir)

        meta_dir = writer.root / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        with open(meta_dir / "episodes.jsonl", "a") as handle:
            handle.write(json.dumps({
                "episode_index": self.episode_index,
                "tasks": [self._primary_task()],
                "length": self.length,
            }) + "\n")
        with open(meta_dir / "episodes_stats.jsonl", "a") as handle:
            handle.write(json.dumps({
                "episode_index": self.episode_index,
                "stats": _serialize_stats(episode_stats),
            }) + "\n")

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


class LeRobotV21Writer:
    def __init__(self, root, fps, features, robot_type=None,
                 chunks_size=DEFAULT_CHUNK_SIZE):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fps = int(fps)
        self.robot_type = robot_type
        self.chunks_size = int(chunks_size)
        self.features = {**{k: dict(v) for k, v in DEFAULT_FEATURES.items()},
                         **features}
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
        (self.root / "meta").mkdir(parents=True, exist_ok=True)
        (self.root / "meta" / "episodes.jsonl").touch()
        (self.root / "meta" / "episodes_stats.jsonl").touch()
        self.write_tasks()
        self.write_info()

    def task_index(self, task):
        if task not in self.tasks:
            self.tasks[task] = len(self.tasks)
            self.write_tasks()
        return self.tasks[task]

    def write_tasks(self):
        path = self.root / "meta" / "tasks.jsonl"
        with open(path, "w") as handle:
            for task, index in self.tasks.items():
                handle.write(json.dumps({"task_index": index, "task": task}) + "\n")

    def write_info(self):
        features = {}
        for key, value in self.features.items():
            feature = dict(value)
            feature["shape"] = list(value["shape"])
            if feature["dtype"] != "video":
                feature["fps"] = self.fps
            else:
                height, width, channels = feature["shape"]
                feature["info"] = {
                    "video.height": int(height),
                    "video.width": int(width),
                    "video.codec": "h264",
                    "video.pix_fmt": "yuv420p",
                    "video.is_depth_map": False,
                    "video.fps": self.fps,
                    "video.channels": int(channels),
                    "has_audio": False,
                }
            features[key] = feature
        info = {
            "codebase_version": CODEBASE_VERSION,
            "robot_type": self.robot_type,
            "total_episodes": self.total_episodes,
            "total_frames": self.total_frames,
            "total_tasks": len(self.tasks),
            "total_videos": self.total_episodes * len(self.video_keys),
            "total_chunks": math.ceil(self.total_episodes / self.chunks_size),
            "chunks_size": self.chunks_size,
            "fps": self.fps,
            "splits": {"train": f"0:{self.total_episodes}"},
            "data_path": DATA_PATH,
            "video_path": VIDEO_PATH if self.video_keys else None,
            "features": features,
        }
        with open(self.root / "meta" / "info.json", "w") as handle:
            json.dump(info, handle, indent=4, ensure_ascii=False)

    def write_stats(self):
        if self.stats is None:
            return
        with open(self.root / "meta" / "stats.json", "w") as handle:
            json.dump(_serialize_stats(self.stats), handle, indent=4, ensure_ascii=False)

    def begin_episode(self):
        return EpisodeRecorderV21(self)

    def validate(self):
        return validate_dataset_v21(self.root)


def _read_jsonl(path):
    if not path.exists():
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def validate_dataset_v21(root):
    root = Path(root)
    info_path = root / "meta" / "info.json"
    if not info_path.exists():
        return [f"{info_path} missing"]
    info = json.loads(info_path.read_text())
    problems = []
    if not str(info.get("codebase_version", "")).startswith("v2"):
        problems.append(f"unexpected codebase_version {info.get('codebase_version')}")
    for relative in ("meta/tasks.jsonl", "meta/episodes.jsonl",
                     "meta/episodes_stats.jsonl", "meta/stats.json"):
        if not (root / relative).exists():
            problems.append(f"missing {relative}")
    episodes = _read_jsonl(root / "meta" / "episodes.jsonl")
    stats = _read_jsonl(root / "meta" / "episodes_stats.jsonl")
    if len(episodes) != info.get("total_episodes", 0):
        problems.append("episodes.jsonl count does not match total_episodes")
    if len(stats) != len(episodes):
        problems.append("episodes_stats.jsonl count does not match episodes.jsonl")
    chunks = info.get("chunks_size", DEFAULT_CHUNK_SIZE)
    video_keys = [k for k, v in info.get("features", {}).items() if v["dtype"] == "video"]
    for episode in episodes:
        index = episode["episode_index"]
        chunk = index // chunks
        data = root / DATA_PATH.format(episode_chunk=chunk, episode_index=index)
        if not data.exists():
            problems.append(f"missing data file for episode {index}")
        for key in video_keys:
            video = root / VIDEO_PATH.format(
                episode_chunk=chunk, episode_index=index, video_key=key)
            if not video.exists():
                problems.append(f"missing video {key} for episode {index}")
    return problems
