#!/usr/bin/env python3
"""Synchronized UMI data collection into a LeRobot v3.0 dataset.

Each stream is sampled on its own thread and keeps a short timestamped buffer.
A fixed-rate main loop aligns the streams by taking, for each tick, the sample
whose timestamp is nearest the tick (FOMI-style nearest-sample alignment,
arXiv 2610.19839) and records the alignment error. Frames outside
``--max-sample-age`` are flagged, and per-episode quality control is written to
``<root>/qc/`` so bad episodes can be dropped.

Streams
    camera (V4L2)   -> observation.images.<camera>
    Tracker (OpenVR)-> observation.pose, observation.valid
    AS5600 (I2C)    -> observation.state (gripper width)
    action          -> current pose + width (8D)

Run `umi_status.py` first. The AS5600 needs /dev/i2c-* access (join the i2c
group or run with `sg i2c -c`).
"""

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

import gripper_as5600
import gripper_calib
import tracker_openvr
from lerobot_v21 import LeRobotV21Writer
from lerobot_v30 import LeRobotV30Writer, validate_dataset

DEFAULT_WIDTH_SPAN_COUNTS = 4096.0
DEFAULT_WIDTH_SPAN_MM = 80.0
# The AS5600 sees a strong ~50 Hz modulation when the gripper is at rest.
# A fixed sample count is rate-dependent (the read loop is 120-370 Hz), so
# average over a time window that spans several 50 Hz periods instead.
GRIPPER_SMOOTH_WINDOW_S = 0.25


def matrix_to_quaternion(rotation):
    """3x3 rotation matrix (rows) to [qx, qy, qz, qw]."""
    (m00, m01, m02), (m10, m11, m12), (m20, m21, m22) = rotation
    trace = m00 + m11 + m22
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        qw, qx, qy, qz = 0.25 * s, (m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2
        qw, qx, qy, qz = (m21 - m12) / s, 0.25 * s, (m01 + m10) / s, (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2
        qw, qx, qy, qz = (m02 - m20) / s, (m01 + m10) / s, 0.25 * s, (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2
        qw, qx, qy, qz = (m10 - m01) / s, (m02 + m20) / s, (m12 + m21) / s, 0.25 * s
    return [qx, qy, qz, qw]


def _nearest(buffer, tick):
    if not buffer:
        return None, 0.0
    return min(buffer, key=lambda item: abs(item[0] - tick))


class CameraStream(threading.Thread):
    def __init__(self, device, width, height, buffer_size=16):
        super().__init__(daemon=True)
        self.device = device
        self.capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not self.capture.isOpened():
            raise RuntimeError(f"cannot open camera {device}")
        self.capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._lock = threading.Lock()
        self._buffer = deque(maxlen=buffer_size)
        self._count = 0
        self._running = True

    def read_first(self):
        for _ in range(30):
            ok, frame = self.capture.read()
            if ok and frame is not None:
                with self._lock:
                    self._buffer.append((time.monotonic(), frame))
                return frame
        raise RuntimeError(f"camera {self.device} produced no frames")

    def run(self):
        try:
            while self._running:
                ok, frame = self.capture.read()
                if ok and frame is not None:
                    with self._lock:
                        self._buffer.append((time.monotonic(), frame))
                    self._count += 1
        finally:
            self.capture.release()

    def sample_at(self, tick, max_age):
        with self._lock:
            item = _nearest(self._buffer, tick)
        if item is None:
            return None, 0.0, False
        stamp, frame = item
        return frame, stamp, abs(stamp - tick) <= max_age

    def latest(self):
        with self._lock:
            return self._buffer[-1] if self._buffer else (0.0, None)

    def stop(self):
        self._running = False


class TrackerStream(threading.Thread):
    def __init__(self, hz=30.0, buffer_size=256):
        super().__init__(daemon=True)
        self.hz = hz
        self.vr = tracker_openvr.init()
        self._lock = threading.Lock()
        self._buffer = deque(maxlen=buffer_size)
        self._pose = (0.0,) * 7
        self._valid = 0.0
        self._count = 0
        self._running = True

    def run(self):
        while self._running:
            try:
                poses = self.vr.getDeviceToAbsoluteTrackingPose(
                    tracker_openvr.openvr.TrackingUniverseStanding, 0,
                    tracker_openvr.openvr.k_unMaxTrackedDeviceCount)
                valid = 0.0
                pose = self._pose
                for index, tracked in enumerate(poses):
                    if self.vr.getTrackedDeviceClass(index) != tracker_openvr.openvr.TrackedDeviceClass_GenericTracker:
                        continue
                    if tracked.bPoseIsValid:
                        matrix = tracked.mDeviceToAbsoluteTracking.m
                        position = [matrix[row][3] for row in range(3)]
                        rotation = [[matrix[row][col] for col in range(3)] for row in range(3)]
                        pose = tuple(position + matrix_to_quaternion(rotation))
                        valid = 1.0
                    break
                with self._lock:
                    self._pose = pose
                    self._valid = valid
                    self._buffer.append((time.monotonic(), pose, valid))
            except Exception:
                with self._lock:
                    self._valid = 0.0
                    self._buffer.append((time.monotonic(), self._pose, 0.0))
            self._count += 1
            time.sleep(1.0 / self.hz)

    def sample_at(self, tick, max_age):
        with self._lock:
            item = _nearest(self._buffer, tick)
        if item is None:
            return (0.0,) * 7, 0.0, 0.0, False
        stamp, pose, valid = item
        return pose, valid, stamp, abs(stamp - tick) <= max_age

    def stop(self):
        self._running = False


class GripperStream(threading.Thread):
    def __init__(self, hz=0.0, calibration=None, calib_model=None,
                 secondary_bus=None, smooth_window_s=GRIPPER_SMOOTH_WINDOW_S):
        super().__init__(daemon=True)
        self.hz = hz
        self.smooth_window_s = float(smooth_window_s)
        self.calibration = calibration
        self.calib_model = calib_model
        self.differential = secondary_bus is not None
        if self.differential:
            self.encoder = gripper_as5600.DualAs5600(secondary_bus=secondary_bus)
        else:
            self.encoder = gripper_as5600.As5600()
        self._samples = deque(maxlen=512)
        self._buffer = deque(maxlen=512)
        self._lock = threading.Lock()
        self._width = 0.0
        self._count = 0
        self._running = True

    def run(self):
        try:
            while self._running:
                try:
                    sample = self.encoder.sample()
                    angle = sample["raw_angle"]
                    stamp = float(sample.get("time_monotonic_s") or time.monotonic())
                    with self._lock:
                        self._samples.append((stamp, angle))
                        smoothed = self._smooth(stamp)
                        self._width = self._to_width(smoothed)
                        self._buffer.append((stamp, self._width, smoothed))
                    self._count += 1
                except OSError:
                    # Do not spin when the adapter disappears.
                    time.sleep(0.05)
                if self.hz > 0:
                    time.sleep(1.0 / self.hz)
        finally:
            self.encoder.close()

    def _smooth(self, now):
        cutoff = now - self.smooth_window_s
        values = [angle for stamp, angle in self._samples if stamp >= cutoff]
        if not values:
            return 0.0
        if self.differential:
            return sum(values) / len(values)
        return gripper_calib.circular_mean(values)

    def _to_width(self, angle):
        if self.calib_model is not None:
            return self.calib_model.update(angle)
        if self.calibration:
            closed, opened, span_mm = self.calibration
            return gripper_as5600.calibration_width_mm(angle, closed, opened, span_mm)
        return angle * (DEFAULT_WIDTH_SPAN_MM / DEFAULT_WIDTH_SPAN_COUNTS)

    def sample_at(self, tick, max_age):
        with self._lock:
            item = _nearest(self._buffer, tick)
        if item is None:
            return 0.0, 0.0, False
        stamp, width, _ = item
        return width, stamp, abs(stamp - tick) <= max_age

    def stop(self):
        self._running = False


def build_features(camera_key, width, height):
    return {
        "observation.state": {"dtype": "float32", "shape": [1],
                              "names": ["gripper_width_mm"]},
        "observation.pose": {"dtype": "float32", "shape": [7],
                             "names": ["x", "y", "z", "qx", "qy", "qz", "qw"]},
        "observation.valid": {"dtype": "float32", "shape": [1],
                              "names": ["pose_valid"]},
        "action": {"dtype": "float32", "shape": [8],
                   "names": ["x", "y", "z", "qx", "qy", "qz", "qw", "gripper_width_mm"]},
        camera_key: {"dtype": "video", "shape": [height, width, 3],
                     "names": ["height", "width", "channels"]},
    }


def sample_tick(recorder, camera, tracker, gripper, camera_key, task, tick, max_age):
    frame, cam_ts, cam_ok = camera.sample_at(tick, max_age)
    if frame is None:
        return False
    if tracker is not None:
        pose, valid, tr_ts, tr_ok = tracker.sample_at(tick, max_age)
    else:
        pose, valid, tr_ts, tr_ok = (0.0,) * 7, 0.0, tick, True
    if gripper is not None:
        width, gr_ts, gr_ok = gripper.sample_at(tick, max_age)
    else:
        width, gr_ts, gr_ok = 0.0, tick, True
    values = {
        "observation.state": [width],
        "observation.pose": list(pose),
        "observation.valid": [valid],
        "action": list(pose) + [width],
    }
    qc = {
        "camera_ok": cam_ok,
        "camera_dt": round(cam_ts - tick, 4),
        "tracker_ok": tr_ok,
        "tracker_dt": round(tr_ts - tick, 4),
        "gripper_ok": gr_ok,
        "gripper_dt": round(gr_ts - tick, 4),
        "pose_valid": int(valid),
    }
    recorder.add_frame(values, frame, task, qc)
    return True


def summarize_qc(recorder, use_tracker, use_gripper):
    frames = len(recorder.qc)
    if frames == 0:
        return {"frames": 0}
    def missing(key):
        return sum(1 for q in recorder.qc if not q.get(key, True))
    camera_missing = missing("camera_ok")
    tracker_missing = missing("tracker_ok") if use_tracker else 0
    gripper_missing = missing("gripper_ok") if use_gripper else 0
    invalid_pose = sum(1 for q in recorder.qc if q.get("pose_valid", 1) == 0) if use_tracker else 0
    max_dt = max((abs(q.get("camera_dt", 0)) for q in recorder.qc), default=0.0)
    return {
        "frames": frames,
        "camera_missing": camera_missing,
        "tracker_missing": tracker_missing,
        "gripper_missing": gripper_missing,
        "pose_invalid": invalid_pose,
        "camera_missing_ratio": camera_missing / frames,
        "tracker_missing_ratio": tracker_missing / frames if use_tracker else 0.0,
        "gripper_missing_ratio": gripper_missing / frames if use_gripper else 0.0,
        "pose_invalid_ratio": invalid_pose / frames if use_tracker else 0.0,
        "max_camera_dt_s": max_dt,
    }


def write_qc(writer, recorder, summary, accepted, per_frame):
    directory = writer.root / "qc"
    directory.mkdir(parents=True, exist_ok=True)
    record = {"episode_index": recorder.episode_index, "accepted": accepted, **summary}
    with open(directory / "episodes.jsonl", "a") as handle:
        handle.write(json.dumps(record) + "\n")
    if per_frame:
        with open(directory / f"episode-{recorder.episode_index:06d}.jsonl", "w") as handle:
            for index, qc in enumerate(recorder.qc):
                handle.write(json.dumps({"frame_index": index, **qc}) + "\n")


def finalize_episode(writer, recorder, args, use_tracker, use_gripper):
    summary = summarize_qc(recorder, use_tracker, use_gripper)
    ratios = [summary["camera_missing_ratio"]]
    if use_tracker:
        ratios.append(summary["tracker_missing_ratio"])
    if use_gripper:
        ratios.append(summary["gripper_missing_ratio"])
    accepted = max(ratios) <= args.max_missing_ratio and (
        not use_tracker or summary["pose_invalid_ratio"] <= args.max_invalid_pose_ratio)
    write_qc(writer, recorder, summary, accepted, args.qc_per_frame)
    if not accepted and args.drop_bad_episodes:
        recorder.abort()
        print(f"episode skipped by QC: {summary}", flush=True)
        return None
    result = recorder.finish()
    if result:
        print(f"episode {result['episode_index']} saved: {result['length']} frames "
              f"camera_missing={summary['camera_missing']} "
              f"pose_invalid={summary['pose_invalid']}", flush=True)
    return result


def run_scripted(writer, camera, tracker, gripper, camera_key, task, args,
                 duration, episodes):
    period = 1.0 / writer.fps
    for _ in range(episodes):
        recorder = writer.begin_episode()
        next_tick = time.monotonic()
        start = next_tick
        while time.monotonic() - start < duration:
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
                continue
            sample_tick(recorder, camera, tracker, gripper, camera_key, task,
                        next_tick, args.max_sample_age)
            next_tick += period
            if time.monotonic() - next_tick > period:
                next_tick = time.monotonic()
        finalize_episode(writer, recorder, args, tracker is not None, gripper is not None)


def run_interactive(writer, camera, tracker, gripper, camera_key, task, args):
    state = {"recorder": None, "stop": False}
    period = 1.0 / writer.fps

    def commands():
        for line in sys.stdin:
            command = line.strip().lower()
            if command in ("s", "start") and state["recorder"] is None:
                state["recorder"] = writer.begin_episode()
                print("recording...", flush=True)
            elif command in ("e", "end", "stop") and state["recorder"] is not None:
                recorder = state["recorder"]
                state["recorder"] = None
                finalize_episode(writer, recorder, args, tracker is not None,
                                 gripper is not None)
            elif command in ("q", "quit", "exit"):
                state["stop"] = True
                return
        state["stop"] = True

    thread = threading.Thread(target=commands, daemon=True)
    thread.start()
    next_tick = time.monotonic()
    while not state["stop"]:
        now = time.monotonic()
        if now < next_tick:
            time.sleep(next_tick - now)
            continue
        if state["recorder"] is not None:
            sample_tick(state["recorder"], camera, tracker, gripper, camera_key,
                        task, next_tick, args.max_sample_age)
        next_tick += period
        if time.monotonic() - next_tick > period:
            next_tick = time.monotonic()
    if state["recorder"] is not None:
        finalize_episode(writer, state["recorder"], args, tracker is not None,
                         gripper is not None)


def selftest(writer, frames=90):
    recorder = writer.begin_episode()
    shape = writer.features[writer.video_keys[0]]["shape"]
    height, width = shape[0], shape[1]
    for index in range(frames):
        frame = np.full((height, width, 3), (index * 3) % 256, dtype=np.uint8)
        pose = [0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0]
        values = {"observation.state": [10.0 + index * 0.1],
                  "observation.pose": pose,
                  "observation.valid": [1.0],
                  "action": pose + [10.0 + index * 0.1]}
        recorder.add_frame(values, frame, "selftest")
    result = recorder.finish()
    print(json.dumps({"episode": result["episode_index"], "length": result["length"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="dataset root directory")
    parser.add_argument("--format", choices=("v3.0", "v2.1"), default="v3.0",
                        help="LeRobot dataset format (default v3.0)")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--task", default="umi_demo")
    parser.add_argument("--camera", default="/dev/video0")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--camera-key", default="observation.images.camera0")
    parser.add_argument("--episodes", type=int, default=0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--no-tracker", action="store_true")
    parser.add_argument("--no-gripper", action="store_true")
    parser.add_argument("--gripper-calib", metavar="CLOSED,OPEN,SPAN_MM",
                        help="legacy two-point calibration")
    parser.add_argument("--gripper-calib-file", metavar="JSON",
                        help="piecewise-quadratic calibration model from gripper_calibrate.py")
    parser.add_argument("--gripper-bus2", type=int,
                        help="I2C bus of a second AS5600 for differential width")
    parser.add_argument("--gripper-hz", type=float, default=0.0,
                        help="AS5600 polling target; 0 = as fast as the I2C bus allows "
                             "(recommended: several hundred Hz to beat 50 Hz aliasing)")
    parser.add_argument("--max-sample-age", type=float, default=0.05,
                        help="max seconds between a stream sample and the tick (default 0.05)")
    parser.add_argument("--drop-bad-episodes", action="store_true",
                        help="discard episodes that fail the QC thresholds")
    parser.add_argument("--max-missing-ratio", type=float, default=0.05)
    parser.add_argument("--max-invalid-pose-ratio", type=float, default=1.0)
    parser.add_argument("--qc-per-frame", action="store_true",
                        help="also write per-frame alignment diagnostics")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--validate-only", metavar="DIR")
    args = parser.parse_args()

    if args.validate_only:
        problems = validate_dataset(args.validate_only)
        print("validate:", "OK" if not problems else problems)
        return 0 if not problems else 1
    if not args.out:
        parser.error("--out is required unless --validate-only is used")
    if args.episodes == 0 and not args.selftest and not sys.stdin.isatty():
        parser.error("interactive mode needs a TTY; use --episodes and --duration")

    if args.selftest:
        features = build_features(args.camera_key, args.width, args.height)
        writer = (LeRobotV21Writer if args.format == "v2.1" else LeRobotV30Writer)(
            args.out, args.fps, features, robot_type="umi_gripper")
        selftest(writer)
        problems = writer.validate()
        print("validate:", "OK" if not problems else problems)
        return 0 if not problems else 1

    calibration = None
    if args.gripper_calib:
        try:
            calibration = tuple(int(x) if i < 2 else float(x)
                                for i, x in enumerate(args.gripper_calib.split(",")))
        except ValueError:
            parser.error("--gripper-calib needs CLOSED,OPEN,SPAN_MM")
    calib_model = None
    if args.gripper_calib_file:
        try:
            calib_model = gripper_calib.GripperCalibration(
                gripper_calib.load(args.gripper_calib_file))
            print(f"using calibration {args.gripper_calib_file}", flush=True)
        except (OSError, ValueError, KeyError) as error:
            parser.error(f"cannot load --gripper-calib-file: {error}")
    if args.gripper_bus2 is not None and calib_model is None:
        print("warning: differential width without --gripper-calib-file is not "
              "meaningful; calibrate on the differential signal first", file=sys.stderr)

    camera = CameraStream(args.camera, args.width, args.height)
    first = camera.read_first()
    height, width = first.shape[:2]
    features = build_features(args.camera_key, width, height)
    writer = (LeRobotV21Writer if args.format == "v2.1" else LeRobotV30Writer)(
        args.out, args.fps, features, robot_type="umi_gripper")
    print(f"writing LeRobot {args.format} dataset to {args.out}", flush=True)

    tracker = None
    if not args.no_tracker:
        try:
            tracker = TrackerStream()
        except Exception as error:
            print(f"tracker unavailable: {error}", file=sys.stderr)
            print("start SteamVR first, or pass --no-tracker", file=sys.stderr)
            camera.stop()
            return 1
    try:
        gripper = None if args.no_gripper else GripperStream(
            hz=args.gripper_hz, calibration=calibration, calib_model=calib_model,
            secondary_bus=args.gripper_bus2)
    except (RuntimeError, PermissionError, OSError) as error:
        print(f"gripper unavailable: {error}", file=sys.stderr)
        print("run with 'sg i2c -c' or after joining the i2c group", file=sys.stderr)
        camera.stop()
        return 1

    camera.start()
    if tracker:
        tracker.start()
    if gripper:
        gripper.start()
    time.sleep(0.5)

    try:
        if args.episodes > 0:
            run_scripted(writer, camera, tracker, gripper, args.camera_key,
                         args.task, args, args.duration, args.episodes)
        else:
            print("commands: s=start  e=end  q=quit", flush=True)
            run_interactive(writer, camera, tracker, gripper, args.camera_key,
                            args.task, args)
    finally:
        for stream in (camera, tracker, gripper):
            if stream is not None:
                stream.stop()
        for stream in (camera, tracker, gripper):
            if stream is not None:
                stream.join(timeout=5)

    problems = writer.validate()
    print(f"dataset: {args.out}")
    print(f"episodes: {writer.total_episodes}  frames: {writer.total_frames}")
    print("validate:", "OK" if not problems else problems)
    sys.stdout.flush()
    sys.stderr.flush()
    # Native libraries can leave non-daemon threads behind; exit explicitly.
    os._exit(0 if not problems else 1)


if __name__ == "__main__":
    sys.exit(main())
