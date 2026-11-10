#!/usr/bin/env python3
"""Modern web console for the UMI synchronized data collector.

The server owns the camera, tracker and gripper streams so that the live
preview, telemetry and recording all share the same devices. Episodes are
written with the same LeRobot writers used by ``umi_collect.py``, and the same
nearest-sample alignment / QC logic is applied.

Endpoints (JSON unless noted)
    GET  /                     dashboard
    GET  /api/state            devices, telemetry, recording and QC state
    GET  /api/preview.mjpg     live MJPEG preview
    GET  /api/snapshot.jpg     one JPEG frame
    GET  /api/datasets         datasets under the output root
    GET  /api/dataset?name=... dataset detail (info.json + QC)
    GET  /api/dataset/episodes?name=...     episode list for the viewer
    GET  /api/dataset/episode?name=...&episode=N&max_points=...
                                            numeric series + current-frame data
    GET  /api/dataset/video?name=...&episode=N   MP4 with HTTP Range support
    GET  /api/dataset/poster?name=...&episode=N  first-frame JPEG
    GET  /api/calibrations     calibration files offered in the UI
    POST /api/settings         update persisted collector settings
    POST /api/session/new      create a dataset session
    POST /api/session/close    validate and close the session
    POST /api/record/start     start an episode
    POST /api/record/stop      stop and finalize the episode
    POST /api/devices/reconnect
    POST /api/dataset/validate
    POST /api/gripper/analyze  FFT jitter summary

Run:
    cd /home/zy/workspace/supre_umi/umi_devices
    python3 umi_web.py --host 127.0.0.1 --port 8770
"""

import argparse
import base64
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import cv2
import numpy as np
import pyarrow.parquet as pq

import camera_v4l2
import gripper_as5600
import gripper_calib
import tracker_openvr
from lerobot_v21 import LeRobotV21Writer
from lerobot_v30 import LeRobotV30Writer, validate_dataset
from umi_collect import (GRIPPER_SMOOTH_WINDOW_S, CameraStream, build_features,
                         matrix_to_quaternion, sample_tick, summarize_qc, write_qc)

VERSION = "1.1.0"
WEB_DIR = Path(__file__).resolve().with_name("web")
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("UMI_WEB_CONFIG", str(Path.home() / ".config" / "umi-web" / "config.json"))
)

DEFAULT_CONFIG = {
    "output_root": "~/datasets",
    "camera": "/dev/video0",
    "width": 640,
    "height": 480,
    "camera_key": "observation.images.camera0",
    "fps": 30,
    "format": "v3.0",
    "task": "pick",
    "enable_tracker": True,
    "auto_start_steamvr": True,
    "enable_gripper": True,
    "calibration_file": "",
    "gripper_bus2": None,
    "max_sample_age": 0.05,
    "drop_bad_episodes": False,
    "max_missing_ratio": 0.05,
    "max_invalid_pose_ratio": 1.0,
    "qc_per_frame": False,
}

RESOLUTIONS = [(320, 240), (640, 480), (1280, 720), (1920, 1080)]
DISABLE_HARDWARE = False


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        stored = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        stored = {}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in config:
                config[key] = value
    return config


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    temp.replace(CONFIG_PATH)


def _as_bool(value, name):
    if not isinstance(value, bool):
        raise ApiError(f"{name} 必须是布尔值")
    return value


def validate_patch(patch):
    """Validate a settings patch and return normalized values."""
    if not isinstance(patch, dict):
        raise ApiError("请求体必须是 JSON 对象")
    allowed = set(DEFAULT_CONFIG)
    unknown = set(patch) - allowed
    if unknown:
        raise ApiError("未知设置项: " + ", ".join(sorted(unknown)))
    clean = {}
    for key, value in patch.items():
        if key in ("output_root", "camera", "camera_key", "task", "calibration_file"):
            if not isinstance(value, str):
                raise ApiError(f"{key} 必须是字符串")
            if key == "output_root" and not value.strip():
                raise ApiError("输出目录不能为空")
            if key == "camera" and not value.startswith("/dev/video"):
                raise ApiError("相机设备必须是 /dev/video* 节点")
            clean[key] = value.strip() if key != "output_root" else value.strip()
        elif key in ("width", "height"):
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ApiError(f"{key} 必须是整数")
            if not 120 <= number <= 3840:
                raise ApiError(f"{key} 超出范围 (120..3840)")
            clean[key] = number
        elif key == "fps":
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ApiError("fps 必须是整数")
            if not 1 <= number <= 120:
                raise ApiError("fps 超出范围 (1..120)")
            clean[key] = number
        elif key == "format":
            if value not in ("v3.0", "v2.1"):
                raise ApiError("format 只能是 v3.0 或 v2.1")
            clean[key] = value
        elif key in ("enable_tracker", "auto_start_steamvr", "enable_gripper",
                     "drop_bad_episodes", "qc_per_frame"):
            clean[key] = _as_bool(value, key)
        elif key == "max_sample_age":
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ApiError("max_sample_age 必须是数字")
            if not 0.005 <= number <= 1.0:
                raise ApiError("max_sample_age 超出范围 (0.005..1.0)")
            clean[key] = round(number, 4)
        elif key in ("max_missing_ratio", "max_invalid_pose_ratio"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ApiError(f"{key} 必须是数字")
            if not 0.0 <= number <= 1.0:
                raise ApiError(f"{key} 超出范围 (0..1)")
            clean[key] = round(number, 4)
        elif key == "gripper_bus2":
            if value in (None, ""):
                clean[key] = None
            else:
                try:
                    clean[key] = int(value)
                except (TypeError, ValueError):
                    raise ApiError("gripper_bus2 必须是 I2C 总线号")
    return clean


# ---------------------------------------------------------------------------
# device monitors
# ---------------------------------------------------------------------------

class CameraMonitor:
    """Owns the V4L2 capture stream shared by preview and recording."""

    def __init__(self, device, width, height):
        self.device = device
        self.stream = CameraStream(device, width, height)
        self.first = self.stream.read_first()
        self.stream.start()

    @property
    def shape(self):
        return self.first.shape[:2]

    def snapshot(self):
        return self.stream.latest()

    def close(self):
        try:
            self.stream.stop()
            self.stream.join(timeout=2.0)
        except Exception:
            pass


class TrackerMonitor(threading.Thread):
    """OpenVR tracker sampler with device metadata for the status card."""

    def __init__(self, hz=30.0):
        super().__init__(daemon=True)
        self.hz = hz
        self.vr = tracker_openvr.init()
        self.openvr = tracker_openvr.openvr
        self.lock = threading.Lock()
        self.buffer = deque(maxlen=256)
        self.pose = (0.0,) * 7
        self.valid = 0.0
        self.device = {}
        self.count = 0
        self.error = None
        self._running = True
        self._serial = None
        self._model = None
        self._last_props = 0.0
        self._battery = None
        self._charging = None

    def _string_property(self, index, prop):
        try:
            return self.vr.getStringTrackedDeviceProperty(index, prop)
        except Exception:
            return None

    def _refresh_battery(self, index):
        now = time.monotonic()
        if now - self._last_props < 2.0:
            return
        self._last_props = now
        try:
            if self.vr.getBoolTrackedDeviceProperty(
                    index, self.openvr.Prop_DeviceProvidesBatteryStatus_Bool):
                self._battery = float(self.vr.getFloatTrackedDeviceProperty(
                    index, self.openvr.Prop_DeviceBatteryPercentage_Float))
                self._charging = bool(self.vr.getBoolTrackedDeviceProperty(
                    index, self.openvr.Prop_DeviceIsCharging_Bool))
        except Exception:
            pass

    def run(self):
        while self._running:
            try:
                poses = self.vr.getDeviceToAbsoluteTrackingPose(
                    self.openvr.TrackingUniverseStanding, 0,
                    self.openvr.k_unMaxTrackedDeviceCount)
                info = None
                for index, tracked in enumerate(poses):
                    if self.vr.getTrackedDeviceClass(index) != \
                            self.openvr.TrackedDeviceClass_GenericTracker:
                        continue
                    connected = bool(tracked.bDeviceIsConnected)
                    valid = bool(tracked.bPoseIsValid)
                    position = None
                    if valid:
                        matrix = tracked.mDeviceToAbsoluteTracking.m
                        position = [float(matrix[row][3]) for row in range(3)]
                        rotation = [[float(matrix[row][col]) for col in range(3)]
                                    for row in range(3)]
                        pose = tuple(position + matrix_to_quaternion(rotation))
                    else:
                        pose = self.pose
                    if self._serial is None:
                        self._serial = self._string_property(
                            index, self.openvr.Prop_SerialNumber_String)
                        self._model = self._string_property(
                            index, self.openvr.Prop_ModelNumber_String)
                    self._refresh_battery(index)
                    info = {
                        "connected": connected,
                        "pose_valid": valid,
                        "tracking_result": tracker_openvr.TRACKING_RESULTS.get(
                            tracked.eTrackingResult, str(tracked.eTrackingResult)),
                        "position": position,
                        "battery": self._battery,
                        "charging": self._charging,
                        "serial": self._serial,
                        "model": self._model,
                    }
                    with self.lock:
                        self.pose = pose
                        self.valid = 1.0 if valid else 0.0
                        self.device = info
                        self.buffer.append((time.monotonic(), pose, self.valid, info))
                    break
                if info is None:
                    with self.lock:
                        self.device = {}
                        self.buffer.append((time.monotonic(), self.pose, 0.0, None))
                self.count += 1
                self.error = None
            except Exception as error:  # openvr can raise during runtime restarts
                self.error = str(error)
                with self.lock:
                    self.buffer.append((time.monotonic(), self.pose, 0.0, None))
            time.sleep(1.0 / self.hz)

    def sample_at(self, tick, max_age):
        with self.lock:
            item = _nearest(self.buffer, tick)
        if item is None:
            return (0.0,) * 7, 0.0, 0.0, False
        stamp, pose, valid, _ = item
        return pose, valid, stamp, abs(stamp - tick) <= max_age

    def latest(self):
        with self.lock:
            return self.buffer[-1] if self.buffer else None

    def close(self):
        self._running = False


class GripperMonitor(threading.Thread):
    """AS5600 sampler with the circular-mean smoothing used by the collector."""

    def __init__(self, hz=0.0, calibration=None, calib_model=None,
                 secondary_bus=None, calibration_label="理论值",
                 smooth_window_s=GRIPPER_SMOOTH_WINDOW_S):
        super().__init__(daemon=True)
        self.hz = hz
        self.smooth_window_s = float(smooth_window_s)
        self.calibration = calibration
        self.calib_model = calib_model
        self.calibration_label = calibration_label
        self.differential = secondary_bus is not None
        if self.differential:
            self.encoder = gripper_as5600.DualAs5600(secondary_bus=secondary_bus)
        else:
            self.encoder = gripper_as5600.As5600()
        self.lock = threading.Lock()
        self.samples = deque(maxlen=512)
        self.buffer = deque(maxlen=512)
        self.raw = None
        self.width = 0.0
        self.count = 0
        self.error = None
        self._running = True

    def _smooth(self, now):
        cutoff = now - self.smooth_window_s
        values = [angle for stamp, angle in self.samples if stamp >= cutoff]
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
        return angle * (80.0 / 4096.0)

    def set_calibration(self, calibration, calib_model, label):
        with self.lock:
            self.calibration = calibration
            self.calib_model = calib_model
            self.calibration_label = label

    def run(self):
        while self._running:
            try:
                sample = self.encoder.sample()
                angle = sample["raw_angle"]
                stamp = float(sample.get("time_monotonic_s") or time.monotonic())
                with self.lock:
                    self.samples.append((stamp, angle))
                    smoothed = self._smooth(stamp)
                    self.width = self._to_width(smoothed)
                    self.raw = sample
                    self.buffer.append((stamp, self.width, smoothed))
                self.count += 1
                self.error = None
            except Exception as error:  # I2C errors and closed-fd races
                self.error = str(error)
                time.sleep(0.05)
            if self.hz > 0:
                time.sleep(1.0 / self.hz)

    def sample_at(self, tick, max_age):
        with self.lock:
            item = _nearest(self.buffer, tick)
        if item is None:
            return 0.0, 0.0, False
        stamp, width, _ = item
        return width, stamp, abs(stamp - tick) <= max_age

    def latest(self):
        with self.lock:
            if not self.buffer:
                return None
            stamp = self.buffer[-1][0]
            cutoff = stamp - self.smooth_window_s
            angles = [angle for sample_stamp, angle in self.samples
                      if sample_stamp >= cutoff]
            return self.buffer[-1], self.raw, angles

    def close(self):
        self._running = False
        try:
            self.encoder.close()
        except Exception:
            pass


def _nearest(buffer, tick):
    if not buffer:
        return None
    return min(buffer, key=lambda item: abs(item[0] - tick))


# ---------------------------------------------------------------------------
# hardware discovery / SteamVR startup
# ---------------------------------------------------------------------------

STEAM_BIN = Path("/usr/bin/steam")
STEAM_LOG = Path.home() / ".cache" / "umi-web" / "steam.log"
STEAM_TIMEOUT_S = float(os.environ.get("UMI_STEAM_TIMEOUT", "45"))
STEAMVR_LAUNCH = (Path.home() / ".local/share/Steam/steamapps/common/SteamVR"
                  / "bin" / "vrmonitor.sh")
STEAMVR_LOG = Path.home() / ".cache" / "umi-web" / "steamvr.log"
STEAMVR_TIMEOUT_S = float(os.environ.get("UMI_STEAMVR_TIMEOUT", "45"))


def resolve_camera(configured, cameras=None):
    """Return a usable capture node.

    V4L2 numbering is not stable across reboots/replugs, so prefer the
    configured node only if it is still a real capture device; otherwise fall
    back to the first discovered capture node.
    """
    try:
        found = camera_v4l2.cameras() if cameras is None else cameras
    except Exception:
        found = []
    devices = [item["device"] for item in found]
    if configured in devices:
        return configured
    return devices[0] if devices else configured


def steamvr_running():
    try:
        result = subprocess.run(["pgrep", "-x", "vrserver"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except OSError:
        return False


def steam_client_running():
    try:
        result = subprocess.run(["pgrep", "-f", "ubuntu12_32/steam"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except OSError:
        return False


def _gui_env():
    env = dict(os.environ)
    uid = os.getuid()
    env["DISPLAY"] = env.get("DISPLAY") or ":0"
    env["XAUTHORITY"] = env.get("XAUTHORITY") or str(Path.home() / ".Xauthority")
    env["XDG_RUNTIME_DIR"] = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env["DBUS_SESSION_BUS_ADDRESS"] = (
        env.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path=/run/user/{uid}/bus")
    return env


def _launch_detached(command, log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        subprocess.Popen([str(part) for part in command], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, env=_gui_env(),
                         start_new_session=True)


def launch_steam():
    if not STEAM_BIN.exists():
        raise RuntimeError(f"未找到 Steam: {STEAM_BIN}")
    _launch_detached([str(STEAM_BIN), "-silent"], STEAM_LOG)


def launch_steamvr():
    if not STEAMVR_LAUNCH.exists():
        raise RuntimeError(f"未找到 SteamVR 启动脚本: {STEAMVR_LAUNCH}")
    _launch_detached([str(STEAMVR_LAUNCH)], STEAMVR_LOG)


def ensure_steamvr(timeout=STEAMVR_TIMEOUT_S):
    """Ensure the Steam client and SteamVR runtime are up (blocking).

    The wireless/USB tracker link (vrlink) is serviced by the Steam client, so
    starting only vrmonitor.sh is not enough: SteamVR enumerates the tracker
    only once Steam is running.
    """
    if not steam_client_running():
        launch_steam()
        deadline = time.monotonic() + STEAM_TIMEOUT_S
        while time.monotonic() < deadline and not steam_client_running():
            time.sleep(0.5)
        time.sleep(2.0)
    if steamvr_running():
        return True
    launch_steamvr()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if steamvr_running():
            time.sleep(1.0)  # let the runtime accept OpenVR clients
            return True
        time.sleep(0.5)
    raise RuntimeError(f"SteamVR 未能在 {timeout:.0f}s 内就绪；日志: {STEAMVR_LOG}")


# ---------------------------------------------------------------------------
# collector service
# ---------------------------------------------------------------------------

class Service:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.rec_lock = threading.Lock()
        self.device_lock = threading.Lock()
        self.jpeg_lock = threading.Lock()
        self._jpeg_key = None
        self._jpeg_bytes = None
        self._catalog_cache = {}
        self._poster_cache = OrderedDict()
        self._poster_lock = threading.Lock()
        self.started_at = time.time()
        self.messages = deque(maxlen=60)
        self.history = deque(maxlen=400)
        self._rates = {}
        self._counts = {}
        self._last_tick = time.monotonic()
        self._last_try = {}
        self.device_errors = {}
        self.cameras = []
        self.camera = None
        self.tracker = None
        self.gripper = None
        self.session = None
        self.calibration_session = None
        self.writer = None
        self.recorder = None
        self.recording = False
        self.finalizing = False
        self.finalize_stage = ""
        self.recording_thread = None
        self._rec_stop = None
        self.episode_task = ""
        self.episode_started_at = 0.0
        self.rec_use_tracker = False
        self.rec_use_gripper = False
        self.last_finalize = None
        self._stop = threading.Event()
        self._open_devices(initial=True)
        self.telemetry = threading.Thread(target=self._telemetry_loop, daemon=True)
        self.telemetry.start()
        self._message("info", f"服务已启动 · LeRobot 采集控制台 v{VERSION}")

    # -- devices ----------------------------------------------------------

    def _message(self, kind, text):
        stamp = time.time()
        self.messages.append({"t": stamp, "kind": kind, "text": text})
        clock = datetime.fromtimestamp(stamp).strftime("%H:%M:%S")
        print(f"[{clock}] {kind.upper():5s} {text}", flush=True)

    def _calibration(self):
        path = str(self.config.get("calibration_file") or "").strip()
        if not path:
            return None, None, "理论值"
        return self._load_calibration_path(path)

    @staticmethod
    def _load_calibration_path(path):
        expanded = Path(os.path.expanduser(str(path).strip()))
        try:
            model = gripper_calib.GripperCalibration(gripper_calib.load(str(expanded)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ApiError(f"无法加载标定文件 {expanded}: {error}")
        return None, model, expanded.name

    def _new_gripper(self):
        calibration, model, label = self._calibration()
        monitor = GripperMonitor(calibration=calibration, calib_model=model,
                                 secondary_bus=self.config.get("gripper_bus2"),
                                 calibration_label=label)
        monitor.start()
        return monitor

    def _open_devices(self, initial=False):
        """(Re)open all device streams. Never call while recording."""
        with self.device_lock:
            if self.recording or self.finalizing:
                raise ApiError("录制过程中不能重新连接设备")
            previous = [self.camera, self.tracker, self.gripper]
            self.camera = self.tracker = self.gripper = None
            for monitor in previous:
                try:
                    if monitor is not None:
                        monitor.close()
                except Exception:
                    pass
            try:
                self.cameras = camera_v4l2.cameras()
            except Exception:
                self.cameras = []
            if DISABLE_HARDWARE:
                for name in ("camera", "tracker", "gripper"):
                    self.device_errors[name] = "已用 --no-devices 启动，硬件未打开"
                return
            cfg = self.config
            camera_device = resolve_camera(cfg["camera"], self.cameras)
            if camera_device != cfg["camera"]:
                self._message("warn",
                              f"相机节点 {cfg['camera']} 不可用，改用 {camera_device}")
                cfg["camera"] = camera_device
                try:
                    save_config(cfg)
                except Exception:
                    pass
            try:
                self.camera = CameraMonitor(camera_device, cfg["width"], cfg["height"])
                self.device_errors.pop("camera", None)
                height, width = self.camera.shape
                self._message("ok", f"相机 {camera_device} 已连接 ({width}x{height})")
            except Exception as error:
                self.device_errors["camera"] = str(error)
                self._message("error", f"相机不可用: {error}")
            try:
                self._drive_steam_startup(time.monotonic())
                if not steamvr_running():
                    raise RuntimeError("SteamVR 正在启动")
                self.tracker = TrackerMonitor()
                self.tracker.start()
                self.device_errors.pop("tracker", None)
            except Exception as error:
                self.device_errors["tracker"] = str(error)
                if not initial:
                    self._message("error", f"Tracker 不可用: {error}")
            try:
                self.gripper = self._new_gripper()
                self.device_errors.pop("gripper", None)
            except Exception as error:
                self.device_errors["gripper"] = str(error)
                if not initial:
                    self._message("error", f"夹爪不可用: {error}")
            self._last_try["camera"] = time.monotonic()
            self._last_try["tracker"] = time.monotonic()
            self._last_try["gripper"] = time.monotonic()

    def _drive_steam_startup(self, now):
        """Advance Steam -> SteamVR startup without blocking the server.

        vrmonitor.sh alone is not enough: the tracker link (vrlink) is serviced
        by the Steam client, so Steam must be running first.
        """
        if not self.config.get("enable_tracker", True) or \
                not self.config.get("auto_start_steamvr", True):
            return
        if not steam_client_running():
            if now - self._last_try.get("steam", 0) > 30:
                self._last_try["steam"] = now
                try:
                    launch_steam()
                    self._message("warn", "Steam 未运行，正在启动")
                except Exception as error:
                    self._message("error", f"无法启动 Steam: {error}")
            return
        if not steamvr_running() and now - self._last_try.get("steamvr", 0) > 30:
            self._last_try["steamvr"] = now
            try:
                launch_steamvr()
                self._message("warn", "SteamVR 未运行，正在启动")
            except Exception as error:
                self._message("error", f"无法启动 SteamVR: {error}")

    def reconnect(self):
        if self.session:
            raise ApiError("当前数据集已打开，结束数据集后才能重新连接相机")
        self._open_devices()
        return {"ok": True}

    @staticmethod
    def _monitor_age(monitor, kind):
        latest = monitor.latest() if monitor is not None else None
        if not latest:
            return None
        stamp = latest[0][0] if kind == "gripper" else latest[0]
        return max(0.0, time.monotonic() - stamp)

    def _maybe_reconnect(self, now):
        if DISABLE_HARDWARE or self.recording or self.finalizing:
            return
        if not self.device_lock.acquire(blocking=False):
            return
        try:
            # Monitors can stay alive while their device disappears (CH341
            # re-enumeration, camera unplug). Replace them once stale.
            for name in ("tracker", "gripper"):
                monitor = getattr(self, name)
                if monitor is None:
                    continue
                age = self._monitor_age(monitor, name)
                reference = age if age is not None else now - self._last_try.get(name, now)
                if reference > 3.0:
                    self._message("warn", f"{name} 数据中断，正在尝试重连")
                    try:
                        monitor.close()
                    except Exception:
                        pass
                    setattr(self, name, None)
                    self._last_try[name] = 0.0
            if self.camera is not None and not self.session:
                age = self._monitor_age(self.camera.stream, "camera")
                reference = age if age is not None else now - self._last_try.get("camera", now)
                if reference > 3.0:
                    self._message("warn", "相机画面中断，正在尝试重连")
                    try:
                        self.camera.close()
                    except Exception:
                        pass
                    self.camera = None
                    self._last_try["camera"] = 0.0
            if self.camera is None and now - self._last_try.get("camera", 0) > 8:
                self._last_try["camera"] = now
                device = resolve_camera(self.config["camera"])
                if device != self.config["camera"]:
                    self.config["camera"] = device
                    try:
                        save_config(self.config)
                    except Exception:
                        pass
                    self._message("warn", f"相机节点已切换为 {device}")
                try:
                    self.camera = CameraMonitor(device, self.config["width"],
                                                self.config["height"])
                    self.device_errors.pop("camera", None)
                    self._message("ok", "相机已自动重连")
                except Exception as error:
                    self.device_errors["camera"] = str(error)
            if self.tracker is None and now - self._last_try.get("tracker", 0) > 15:
                self._last_try["tracker"] = now
                self._drive_steam_startup(now)
                if steamvr_running():
                    try:
                        self.tracker = TrackerMonitor()
                        self.tracker.start()
                        self.device_errors.pop("tracker", None)
                        self._message("ok", "Tracker 已自动重连")
                    except Exception as error:
                        self.device_errors["tracker"] = str(error)
            if self.gripper is None and now - self._last_try.get("gripper", 0) > 6:
                self._last_try["gripper"] = now
                try:
                    self.gripper = self._new_gripper()
                    self.device_errors.pop("gripper", None)
                    self._message("ok", "夹爪已自动重连")
                except Exception as error:
                    self.device_errors["gripper"] = str(error)
        finally:
            self.device_lock.release()

    # -- telemetry --------------------------------------------------------

    def _count_of(self, name):
        if name == "camera":
            return self.camera.stream._count if self.camera else None
        monitor = getattr(self, name, None)
        return monitor.count if monitor is not None else None

    def _telemetry_loop(self):
        while not self._stop.wait(0.15):
            now = time.monotonic()
            dt = max(1e-3, now - self._last_tick)
            for name in ("camera", "tracker", "gripper"):
                count = self._count_of(name)
                self._counts[name] = count
                previous = self._counts.get(name + "_prev", count)
                self._counts[name + "_prev"] = count
                if count is not None and previous is not None:
                    rate = max(0.0, (count - previous) / dt)
                    self._rates[name] = 0.75 * self._rates.get(name, rate) + 0.25 * rate
                elif count is None:
                    self._rates[name] = 0.0
            self._last_tick = now
            width = None
            if self.gripper is not None:
                latest = self.gripper.latest()
                if latest is not None:
                    width = latest[0][1]
            self.history.append({"t": time.time(), "width": width,
                                 "cam": round(self._rates.get("camera", 0.0), 1)})
            self._maybe_reconnect(now)

    # -- state ------------------------------------------------------------

    def _camera_state(self):
        cfg = self.config
        name = next((camera["name"] for camera in self.cameras
                     if camera["device"] == cfg["camera"]), Path(cfg["camera"]).name)
        if self.camera is None:
            return {"ok": False, "device": cfg["camera"], "name": name,
                    "error": self.device_errors.get("camera", "相机不可用")}
        stamp, frame = self.camera.snapshot()
        age = max(0.0, time.monotonic() - (stamp or 0.0))
        state = {
            "ok": frame is not None and age < 2.0,
            "device": cfg["camera"],
            "name": name,
            "width": int(frame.shape[1]) if frame is not None else None,
            "height": int(frame.shape[0]) if frame is not None else None,
            "fps": round(self._rates.get("camera", 0.0), 1),
            "frames": self.camera.stream._count,
            "age_s": round(age, 2),
        }
        if self.device_errors.get("camera"):
            state["error"] = self.device_errors["camera"]
        return state

    def _tracker_state(self):
        if self.tracker is None:
            return {"ok": False, "error": self.device_errors.get("tracker", "Tracker 不可用")}
        latest = self.tracker.latest()
        with self.tracker.lock:
            device = dict(self.tracker.device)
        age = round(max(0.0, time.monotonic() - latest[0]), 2) if latest else None
        fresh = age is not None and age < 2.0
        state = {
            "ok": bool(device.get("pose_valid")) and fresh,
            "stale": not fresh,
            "connected": bool(device.get("connected")),
            "pose_valid": bool(device.get("pose_valid")),
            "tracking_result": device.get("tracking_result", "—"),
            "serial": device.get("serial"),
            "model": device.get("model"),
            "battery": device.get("battery"),
            "charging": device.get("charging"),
            "position": device.get("position"),
            "hz": round(self._rates.get("tracker", 0.0), 1),
            "age_s": age,
        }
        if latest:
            state["pose"] = [round(value, 4) for value in latest[1]]
            state["pose_valid"] = bool(latest[2])
        if self.tracker.error:
            state["error"] = self.tracker.error
        return state

    def _gripper_state(self):
        if self.gripper is None:
            return {"ok": False, "error": self.device_errors.get("gripper", "夹爪不可用")}
        latest = self.gripper.latest()
        if latest is None:
            return {"ok": False, "error": self.gripper.error or "暂无读数"}
        (stamp, width, smoothed), raw, angles = latest
        raw = raw or {}
        span = (max(angles) - min(angles)) if angles else 0
        age = round(max(0.0, time.monotonic() - stamp), 2)
        fresh = age < 2.0
        state = {
            "ok": bool(raw.get("magnet_detected")) and fresh,
            "stale": not fresh,
            "width_mm": round(width, 3),
            "smoothed_angle": round(smoothed, 1),
            "raw_angle": raw.get("raw_angle"),
            "raw_angle_deg": round(raw.get("raw_angle_deg", 0.0), 2),
            "span": span,
            "agc": raw.get("agc"),
            "magnitude": raw.get("magnitude"),
            "magnet_detected": bool(raw.get("magnet_detected")),
            "magnet_too_weak": bool(raw.get("magnet_too_weak")),
            "magnet_too_strong": bool(raw.get("magnet_too_strong")),
            "mode": raw.get("mode", "single"),
            "calibration": self.gripper.calibration_label,
            "hz": round(self._rates.get("gripper", 0.0), 1),
            "age_s": age,
            "bus": raw.get("bus"),
        }
        if self.gripper.error:
            state["error"] = self.gripper.error
        return state

    def _recording_state(self, now):
        with self.lock:
            recorder = self.recorder
            use_tracker = self.rec_use_tracker
            use_gripper = self.rec_use_gripper
            task = self.episode_task
            started = self.episode_started_at
            active = self.recording
            finalizing = self.finalizing
            stage = self.finalize_stage
            last = self.last_finalize
        if active and recorder is not None:
            with self.rec_lock:
                frames = len(recorder.qc)
                summary = (summarize_qc(recorder, use_tracker, use_gripper)
                           if frames else {"frames": 0})
            return {
                "active": True,
                "finalizing": False,
                "episode_index": recorder.episode_index,
                "task": task,
                "elapsed_s": round(now - started, 2),
                "frames": frames,
                "target_fps": self.writer.fps if self.writer else None,
                "summary": summary,
                "use_tracker": use_tracker,
                "use_gripper": use_gripper,
            }
        return {
            "active": False,
            "finalizing": finalizing,
            "stage": stage,
            "last": last,
        }

    def _disk(self):
        path = Path(os.path.expanduser(self.config["output_root"])).resolve()
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
            return {"free_gb": round(usage.free / 1e9, 1),
                    "total_gb": round(usage.total / 1e9, 1)}
        except OSError:
            return None

    def _dataset_episodes(self, session):
        if not session:
            return []
        return read_qc(session["path"], session.get("fps") or self.config["fps"])

    def state(self):
        now = time.monotonic()
        with self.lock:
            config = dict(self.config)
            session = dict(self.session) if self.session else None
            history = list(self.history)
            messages = list(self.messages)[-18:]
        now_wall = time.time()
        history_payload = [
            {"t": round(item["t"] - now_wall, 2),
             "width": round(item["width"], 3) if item["width"] is not None else None,
             "cam": item.get("cam")}
            for item in history if item["width"] is not None
        ]
        return {
            "version": VERSION,
            "now": now_wall,
            "uptime_s": round(time.time() - self.started_at, 1),
            "config": config,
            "session": session,
            "episodes": self._dataset_episodes(session),
            "recording": self._recording_state(now),
            "devices": {
                "camera": self._camera_state(),
                "tracker": self._tracker_state(),
                "gripper": self._gripper_state(),
            },
            "history": history_payload[-400:],
            "messages": messages,
            "cameras": self.cameras,
            "resolutions": [f"{w}x{h}" for w, h in RESOLUTIONS],
            "disk": self._disk(),
        }

    # -- preview ----------------------------------------------------------

    def latest_jpeg(self):
        camera = self.camera
        if camera is None:
            return None
        stamp, frame = camera.snapshot()
        if frame is None:
            return None
        with self.jpeg_lock:
            if stamp == self._jpeg_key and self._jpeg_bytes is not None:
                return self._jpeg_bytes
            ok, encoded = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not ok:
                return None
            self._jpeg_key = stamp
            self._jpeg_bytes = encoded.tobytes()
            return self._jpeg_bytes

    # -- sessions ---------------------------------------------------------

    def _resolve_dataset(self, name):
        root = Path(os.path.expanduser(self.config["output_root"])).resolve()
        target = (root / str(name)).resolve()
        if target != root and root not in target.parents:
            raise ApiError("非法数据集路径")
        return target

    def new_session(self, name=None):
        with self.lock:
            if self.recording:
                raise ApiError("正在录制，请先结束当前 episode")
            if self.finalizing:
                raise ApiError("上一段视频仍在编码，请稍候")
            if self.session and self.session.get("episodes", 0) > 0:
                raise ApiError("当前数据集已有 episode，请先结束数据集并校验")
            if self.camera is None:
                raise ApiError("相机不可用，无法创建数据集")
            _, frame = self.camera.snapshot()
            if frame is None:
                raise ApiError("相机暂无画面，无法创建数据集")
            config = dict(self.config)
            if name is None or not str(name).strip():
                name = f"umi_{datetime.now():%Y%m%d_%H%M%S}"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name).strip()).strip(".-_")
            if not safe:
                raise ApiError("数据集名称无效")
            path = (Path(os.path.expanduser(config["output_root"])).resolve() / safe)
            if path.exists() and any(path.iterdir()):
                raise ApiError(f"目录已存在且非空：{path}")
            path.mkdir(parents=True, exist_ok=True)
            height, width = frame.shape[:2]
            features = build_features(config["camera_key"], width, height)
            writer_class = (LeRobotV21Writer if config["format"] == "v2.1"
                            else LeRobotV30Writer)
            try:
                writer = writer_class(str(path), int(config["fps"]), features,
                                      robot_type="umi_gripper")
            except Exception as error:
                raise ApiError(f"无法创建数据集: {error}")
            self.writer = writer
            self.session = {
                "name": safe,
                "path": str(path),
                "format": config["format"],
                "fps": int(config["fps"]),
                "width": int(width),
                "height": int(height),
                "episodes": 0,
                "frames": 0,
                "created_at": time.time(),
                "validate": None,
            }
            self._message("ok", f"新建数据集 {safe} · {config['format']} · {config['fps']} fps")
            return dict(self.session)

    def start_recording(self, name=None):
        with self.lock:
            if self.recording:
                raise ApiError("已经在录制中")
            if self.finalizing:
                raise ApiError("上一段视频仍在编码，请稍候")
            config = dict(self.config)
            if self.camera is None:
                raise ApiError("相机不可用")
            frame = self.camera.snapshot()[1]
            if frame is None:
                raise ApiError("相机暂无画面")
            height, width = frame.shape[:2]
            use_tracker = bool(config["enable_tracker"])
            if use_tracker and self.tracker is None:
                raise ApiError("Tracker 不可用：启动 SteamVR 后重连，或在设置中关闭 Tracker")
            use_gripper = bool(config["enable_gripper"])
            if use_gripper and self.gripper is None:
                raise ApiError("夹爪不可用：检查 I2C 连接，或在设置中关闭夹爪")
            # Create the dataset only after the readiness checks pass so a failed
            # start does not leave an empty dataset behind.
            if self.session is None or self.writer is None:
                self.new_session(name)
            session = self.session
            if (width, height) != (session["width"], session["height"]):
                raise ApiError(
                    f"相机分辨率 {width}x{height} 与数据集 {session['width']}x"
                    f"{session['height']} 不一致，请结束数据集后重连设备")
            task = (config.get("task") or "umi_demo").strip() or "umi_demo"
            recorder = self.writer.begin_episode()
            self.recorder = recorder
            self.episode_task = task
            self.episode_started_at = time.monotonic()
            self.rec_use_tracker = use_tracker
            self.rec_use_gripper = use_gripper
            self.recording = True
            self.last_finalize = None
            self._rec_stop = threading.Event()
            self.recording_thread = threading.Thread(target=self._record_loop,
                                                     daemon=True)
            self.recording_thread.start()
            self._message("rec", f"开始录制 episode {recorder.episode_index} · 任务 {task}")
            return {"ok": True, "episode_index": recorder.episode_index,
                    "task": task, "session": dict(session)}

    def _record_loop(self):
        period = 1.0 / max(1, int(self.config["fps"]))
        camera_key = self.config["camera_key"]
        max_age = float(self.config["max_sample_age"])
        task = self.episode_task
        use_tracker = self.rec_use_tracker
        use_gripper = self.rec_use_gripper
        recorder = self.recorder
        stop = self._rec_stop
        next_tick = time.monotonic()
        reported = False
        while not stop.is_set():
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(next_tick - now, 0.005))
                continue
            try:
                with self.rec_lock:
                    sample_tick(recorder, self.camera.stream,
                                self.tracker if use_tracker else None,
                                self.gripper if use_gripper else None,
                                camera_key, task, next_tick, max_age)
            except Exception as error:
                if not reported:
                    self._message("error", f"录制采样异常: {error}")
                    reported = True
            next_tick += period
            if time.monotonic() - next_tick > period:
                next_tick = time.monotonic()

    def stop_recording(self):
        with self.lock:
            if not self.recording:
                raise ApiError("当前没有正在进行的录制")
            self.recording = False
            stop = self._rec_stop
            thread = self.recording_thread
            recorder = self.recorder
            use_tracker = self.rec_use_tracker
            use_gripper = self.rec_use_gripper
            if recorder is None:
                self.recording = False
                raise ApiError("没有可保存的 episode")
            if stop is not None:
                stop.set()
        if thread is not None:
            thread.join(timeout=5.0)
        with self.lock:
            self.recorder = None
            self.finalizing = True
            self.finalize_stage = "正在编码视频并写入数据集…"
        threading.Thread(target=self._finalize_worker,
                         args=(recorder, use_tracker, use_gripper),
                         daemon=True).start()
        return {"ok": True, "episode_index": recorder.episode_index}

    def _finalize_worker(self, recorder, use_tracker, use_gripper):
        config = dict(self.config)
        writer = self.writer
        try:
            with self.rec_lock:
                frames = len(recorder.qc)
            if frames == 0:
                recorder.abort()
                write_qc(writer, recorder, {"frames": 0}, False,
                         config.get("qc_per_frame", False))
                result = None
                summary = {"frames": 0}
                accepted = False
                dropped = True
                self._message("warn", "该 episode 没有采集到任何帧，已丢弃")
            else:
                summary = summarize_qc(recorder, use_tracker, use_gripper)
                ratios = [summary.get("camera_missing_ratio", 0.0)]
                if use_tracker:
                    ratios.append(summary.get("tracker_missing_ratio", 0.0))
                if use_gripper:
                    ratios.append(summary.get("gripper_missing_ratio", 0.0))
                accepted = (
                    max(ratios) <= config["max_missing_ratio"]
                    and (not use_tracker
                         or summary.get("pose_invalid_ratio", 0.0)
                         <= config["max_invalid_pose_ratio"]))
                write_qc(writer, recorder, summary, accepted,
                         bool(config.get("qc_per_frame")))
                dropped = bool(config.get("drop_bad_episodes")) and not accepted
                if dropped:
                    recorder.abort()
                    result = None
                    self._message(
                        "warn",
                        f"episode {recorder.episode_index} 未通过 QC，已按设置丢弃 "
                        f"(相机缺失 {summary.get('camera_missing_ratio', 0) * 100:.1f}%)")
                else:
                    result = recorder.finish()
                    self._message(
                        "ok",
                        f"episode {recorder.episode_index} 已保存 · "
                        f"{summary.get('frames', 0)} 帧 · "
                        f"相机缺失 {summary.get('camera_missing_ratio', 0) * 100:.1f}% · "
                        f"位姿无效 {summary.get('pose_invalid_ratio', 0) * 100:.1f}%")
            with self.lock:
                if self.session is not None:
                    self.session["episodes"] = writer.total_episodes
                    self.session["frames"] = writer.total_frames
                self.last_finalize = {
                    "episode_index": recorder.episode_index,
                    "frames": summary.get("frames", 0),
                    "accepted": accepted,
                    "dropped": dropped,
                    "summary": summary,
                    "saved": result is not None,
                }
        except Exception as error:
            traceback.print_exc()
            try:
                recorder.abort()
            except Exception:
                pass
            self._message("error", f"保存 episode 失败: {error}")
            with self.lock:
                self.last_finalize = {"episode_index": recorder.episode_index,
                                      "error": str(error), "saved": False}
        finally:
            with self.lock:
                self.finalizing = False
                self.finalize_stage = ""

    def close_session(self):
        with self.lock:
            if self.recording:
                raise ApiError("正在录制，无法结束数据集")
            if self.finalizing:
                raise ApiError("视频仍在编码，请稍候")
            if self.session is None or self.writer is None:
                raise ApiError("当前没有打开的数据集")
            writer = self.writer
            session = dict(self.session)
            self.writer = None
            self.session = None
        problems = writer.validate()
        session["validate"] = problems
        self._message("ok" if not problems else "warn",
                      f"数据集 {session['name']} 已关闭 · " +
                      ("校验通过" if not problems else f"校验发现 {len(problems)} 个问题"))
        return {"session": session, "problems": problems}

    # -- settings ---------------------------------------------------------

    def update_config(self, patch):
        clean = validate_patch(patch)
        with self.lock:
            if self.recording or self.finalizing:
                allowed = {"task"}
                rejected = set(clean) - allowed
                if rejected:
                    raise ApiError("录制过程中只能修改任务名称")
            config = dict(self.config)
            session_keys = {"fps", "format", "camera", "width", "height", "camera_key"}
            if self.session and session_keys & set(clean):
                for key in session_keys & set(clean):
                    if clean[key] != config.get(key):
                        raise ApiError("数据集打开期间不能修改帧率、格式或相机设置")
            camera_changed = any(clean.get(key) != config.get(key)
                                 for key in ("camera", "width", "height"))
            calibration_changed = ("calibration_file" in clean
                                   and clean["calibration_file"] != config.get("calibration_file"))
            if calibration_changed and str(clean["calibration_file"]).strip():
                # Validate the model before persisting a broken setting.
                self._load_calibration_path(clean["calibration_file"])
            config.update(clean)
            self.config = config
            save_config(config)
            if calibration_changed:
                with self.device_lock:
                    try:
                        new_monitor = self._new_gripper()
                        old = self.gripper
                        self.gripper = new_monitor
                        if old is not None:
                            old.close()
                        self.device_errors.pop("gripper", None)
                    except Exception as error:
                        self.device_errors["gripper"] = str(error)
                        self._message("error", f"标定已保存，但夹爪重连失败: {error}")
            if camera_changed and not self.session:
                self._open_devices()
            self._message("info", "设置已保存")
            return dict(self.config)

    # -- datasets ---------------------------------------------------------

    def list_datasets(self, limit=60):
        root = Path(os.path.expanduser(self.config["output_root"])).resolve()
        if not root.exists():
            return []
        entries = []
        try:
            children = list(root.iterdir())
        except OSError as error:
            raise ApiError(f"无法读取输出目录: {error}")
        for child in children:
            if not child.is_dir():
                continue
            info_path = child / "meta" / "info.json"
            if not info_path.exists():
                continue
            try:
                info = json.loads(info_path.read_text())
            except (OSError, ValueError):
                continue
            qc = read_qc(child, info.get("fps") or 30)
            accepted = sum(1 for record in qc if record.get("accepted"))
            try:
                modified = child.stat().st_mtime
            except OSError:
                modified = 0.0
            entries.append({
                "name": child.name,
                "path": str(child),
                "format": info.get("codebase_version"),
                "episodes": info.get("total_episodes", 0),
                "frames": info.get("total_frames", 0),
                "fps": info.get("fps"),
                "tasks": info.get("total_tasks", 0),
                "robot_type": info.get("robot_type"),
                "qc_total": len(qc),
                "qc_accepted": accepted,
                "modified": modified,
            })
        entries.sort(key=lambda item: item["modified"], reverse=True)
        return entries[:limit]

    def dataset_detail(self, name):
        if not name:
            raise ApiError("缺少数据集名称")
        path = self._resolve_dataset(name)
        info_path = path / "meta" / "info.json"
        if not info_path.exists():
            raise ApiError(f"数据集不存在: {path}")
        try:
            info = json.loads(info_path.read_text())
        except (OSError, ValueError) as error:
            raise ApiError(f"无法读取 info.json: {error}")
        qc = read_qc(path, info.get("fps") or 30)
        features = []
        for key, feature in (info.get("features") or {}).items():
            features.append({
                "key": key,
                "dtype": feature.get("dtype"),
                "shape": feature.get("shape"),
                "names": feature.get("names"),
            })
        size = 0
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    size += item.stat().st_size
        except OSError:
            pass
        return {
            "name": path.name,
            "path": str(path),
            "info": info,
            "features": features,
            "episodes": qc,
            "size_bytes": size,
        }

    # -- dataset viewer ---------------------------------------------------

    @staticmethod
    def _round_number(value, digits=5):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(number, digits) if math.isfinite(number) else None

    @staticmethod
    def _series_stats(values):
        clean = [float(value) for value in values
                 if value is not None and math.isfinite(float(value))]
        if not clean:
            return {"min": None, "max": None, "mean": None}
        array = np.asarray(clean, dtype=np.float64)
        return {"min": round(float(array.min()), 5),
                "max": round(float(array.max()), 5),
                "mean": round(float(array.mean()), 5)}

    @staticmethod
    def _video_key(info):
        for key, feature in (info.get("features") or {}).items():
            if feature.get("dtype") == "video":
                return key
        return "observation.images.camera0"

    def _dataset_info(self, name):
        if not name:
            raise ApiError("缺少数据集名称")
        path = self._resolve_dataset(name)
        info_path = path / "meta" / "info.json"
        if not info_path.exists():
            raise ApiError(f"数据集不存在: {path}")
        try:
            info = json.loads(info_path.read_text())
        except (OSError, ValueError) as error:
            raise ApiError(f"无法读取 info.json: {error}")
        return path, info

    @staticmethod
    def _episode_entry(index, length, tasks, data_path, video_path, fps, qc):
        if isinstance(tasks, str):
            tasks = [tasks]
        if not isinstance(tasks, (list, tuple)):
            tasks = []
        task = ", ".join(str(item) for item in tasks if item)
        return {
            "index": int(index),
            "length": int(length),
            "duration_s": round(int(length) / fps, 3) if fps else 0.0,
            "task": task,
            "has_data": data_path.is_file(),
            "has_video": video_path.is_file(),
            "data_path": str(data_path),
            "video_path": str(video_path),
            "accepted": (qc or {}).get("accepted"),
            "qc": qc or {},
        }

    def _build_catalog(self, path, info):
        fmt = str(info.get("codebase_version") or "v3.0")
        fps = float(info.get("fps") or 30)
        video_key = self._video_key(info)
        qc = {}
        for record in read_qc(path, fps):
            try:
                qc[int(record["episode_index"])] = record
            except (KeyError, TypeError, ValueError):
                continue
        episodes = []
        if fmt.startswith("v2"):
            meta_path = path / "meta" / "episodes.jsonl"
            if not meta_path.exists():
                raise ApiError("数据集缺少 meta/episodes.jsonl，无法浏览")
            for line in meta_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                index = int(row.get("episode_index", len(episodes)))
                length = int(row.get("length") or 0)
                tasks = row.get("tasks") or ([row["task"]] if row.get("task") else [])
                chunk = index // 1000
                data_path = path / f"data/chunk-{chunk:03d}/episode_{index:06d}.parquet"
                video_path = (path / f"videos/chunk-{chunk:03d}/{video_key}/"
                                    f"episode_{index:06d}.mp4")
                episodes.append(self._episode_entry(
                    index, length, tasks, data_path, video_path, fps, qc.get(index)))
        else:
            meta_files = sorted((path / "meta" / "episodes").glob("**/*.parquet"))
            if not meta_files:
                raise ApiError("数据集缺少 meta/episodes 元数据，无法浏览")
            for meta_file in meta_files:
                schema = pq.read_schema(meta_file)
                columns = [name for name in
                           ("episode_index", "length", "tasks", "task",
                            "data/chunk_index", "data/file_index")
                           if name in schema.names]
                for name in schema.names:
                    if name.startswith("videos/") and name.endswith(
                            ("/chunk_index", "/file_index")):
                        columns.append(name)
                table = pq.read_table(meta_file, columns=columns)
                for row in table.to_pylist():
                    index = int(row.get("episode_index", len(episodes)))
                    length = int(row.get("length") or 0)
                    tasks = row.get("tasks") or ([row["task"]] if row.get("task") else [])
                    chunk = int(row.get("data/chunk_index", index // 1000))
                    file_index = int(row.get("data/file_index", index % 1000))
                    video_chunk = int(row.get(f"videos/{video_key}/chunk_index",
                                              index // 1000))
                    video_file = int(row.get(f"videos/{video_key}/file_index",
                                             index % 1000))
                    data_path = (path / f"data/chunk-{chunk:03d}/"
                                        f"file-{file_index:03d}.parquet")
                    video_path = (path / f"videos/{video_key}/"
                                         f"chunk-{video_chunk:03d}/"
                                         f"file-{video_file:03d}.mp4")
                    episodes.append(self._episode_entry(
                        index, length, tasks, data_path, video_path, fps, qc.get(index)))
        episodes.sort(key=lambda item: item["index"])
        return {"name": path.name, "path": str(path), "format": fmt, "fps": fps,
                "video_key": video_key, "episodes": episodes}

    def _catalog(self, name):
        path, info = self._dataset_info(name)
        info_path = path / "meta" / "info.json"
        try:
            stamp = info_path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        key = str(path)
        cached = self._catalog_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        catalog = self._build_catalog(path, info)
        self._catalog_cache[key] = (stamp, catalog)
        return catalog

    @staticmethod
    def _find_episode(catalog, index):
        for episode in catalog["episodes"]:
            if episode["index"] == index:
                return episode
        raise ApiError(f"episode {index} 不存在", 404)

    def dataset_episodes(self, name):
        return self._catalog(name)

    def dataset_episode_data(self, name, episode_index, max_points=4000):
        catalog = self._catalog(name)
        episode = self._find_episode(catalog, int(episode_index))
        try:
            max_points = int(max_points)
        except (TypeError, ValueError):
            max_points = 4000
        max_points = max(100, min(20000, max_points))
        data_path = Path(episode["data_path"])
        if not data_path.is_file():
            raise ApiError("该 episode 没有 parquet 数据文件", 404)
        schema = pq.read_schema(data_path)
        wanted = [name for name in
                  ("episode_index", "timestamp", "frame_index",
                   "observation.state", "observation.pose",
                   "observation.valid", "action")
                  if name in schema.names]
        table = pq.read_table(data_path, columns=wanted)
        row_indices = None
        if "episode_index" in table.column_names:
            values = table.column("episode_index").to_pylist()
            indices = np.asarray(values, dtype=np.int64)
            row_indices = np.nonzero(indices == episode["index"])[0]
            if row_indices.size == 0:
                if len(indices) == episode["length"]:
                    row_indices = np.arange(len(indices))
                else:
                    raise ApiError("数据文件中找不到该 episode", 404)

        def column_values(column):
            if column not in table.column_names:
                return None
            values = table.column(column).to_pylist()
            if row_indices is not None:
                values = [values[int(i)] for i in row_indices]
            return values

        timestamps = column_values("timestamp")
        frame_numbers = column_values("frame_index")
        states = column_values("observation.state")
        poses = column_values("observation.pose")
        valids = column_values("observation.valid")
        actions = column_values("action")
        fps = float(catalog["fps"] or 30) or 30.0
        count = len(timestamps or frame_numbers or poses or states or actions or [])
        if count == 0:
            raise ApiError("episode 数据为空", 404)

        def scalar(value):
            if isinstance(value, (list, tuple)):
                return value[0] if value else None
            return value

        def vector(value, size):
            if isinstance(value, (list, tuple)):
                result = list(value)[:size]
                result.extend([None] * (size - len(result)))
                return result
            if value is None:
                return [None] * size
            return [value] + [None] * (size - 1)

        width, valid = [], []
        position_x, position_y, position_z = [], [], []
        quat = [[], [], [], []]
        times = []
        for index in range(count):
            action = vector(actions[index], 8) if actions else [None] * 8
            pose = (vector(poses[index], 7) if poses and index < len(poses)
                    else action[:7])
            if all(value is None for value in pose):
                pose = [None] * 7
            width_value = scalar(states[index]) if states and index < len(states) else None
            if width_value is None:
                width_value = action[7]
            valid_value = scalar(valids[index]) if valids and index < len(valids) else 1.0
            width.append(width_value)
            valid.append(valid_value)
            position_x.append(pose[0])
            position_y.append(pose[1])
            position_z.append(pose[2])
            for channel in range(4):
                quat[channel].append(pose[3 + channel])
            timestamp = timestamps[index] if timestamps and index < len(timestamps) else None
            if timestamp is None:
                frame = frame_numbers[index] if frame_numbers and index < len(frame_numbers) else None
                timestamp = (float(frame) / fps) if frame is not None else index / fps
            times.append(float(timestamp))

        stride = max(1, math.ceil(count / max_points))
        sampled = list(range(0, count, stride))
        if sampled[-1] != count - 1:
            sampled.append(count - 1)

        def packet(label, unit, values):
            return {"label": label, "unit": unit,
                    "stats": self._series_stats(values),
                    "values": [self._round_number(values[i], 5) for i in sampled]}

        series = {
            "width": packet("夹爪宽度", "mm", width),
            "x": packet("位置 X", "m", position_x),
            "y": packet("位置 Y", "m", position_y),
            "z": packet("位置 Z", "m", position_z),
            "qx": packet("四元数 QX", "", quat[0]),
            "qy": packet("四元数 QY", "", quat[1]),
            "qz": packet("四元数 QZ", "", quat[2]),
            "qw": packet("四元数 QW", "", quat[3]),
            "valid": packet("位姿有效", "", valid),
        }
        sampled_frames = []
        for source_index in sampled:
            if frame_numbers and source_index < len(frame_numbers):
                sampled_frames.append(self._round_number(frame_numbers[source_index], 0))
            else:
                sampled_frames.append(round(times[source_index] * fps))
        name_quoted = quote(str(catalog["name"]), safe="")
        video_url = f"/api/dataset/video?name={name_quoted}&episode={episode['index']}"
        poster_url = f"/api/dataset/poster?name={name_quoted}&episode={episode['index']}"
        duration = float(episode["duration_s"] or 0.0)
        if duration <= 0 and times:
            duration = max(times) + 1.0 / fps
        video_size = 0
        if episode["has_video"]:
            try:
                video_size = Path(episode["video_path"]).stat().st_size
            except OSError:
                video_size = 0
        return {
            "dataset": catalog["name"],
            "episode_index": episode["index"],
            "task": episode["task"],
            "fps": catalog["fps"],
            "length": episode["length"] or count,
            "total_frames": count,
            "duration_s": round(duration, 4),
            "sampled": stride > 1,
            "stride": stride,
            "points": len(sampled),
            "time": [self._round_number(value, 4) for value in
                     (times[i] for i in sampled)],
            "frame": sampled_frames,
            "series": series,
            "video": {"available": episode["has_video"],
                      "url": video_url, "poster": poster_url,
                      "size": video_size},
            "qc": episode.get("qc") or {},
        }

    def dataset_video_path(self, name, episode_index):
        catalog = self._catalog(name)
        episode = self._find_episode(catalog, int(episode_index))
        path = Path(episode["video_path"]).resolve()
        root = Path(catalog["path"]).resolve()
        if path != root and root not in path.parents:
            raise ApiError("非法视频路径", 403)
        if not path.is_file() or path.stat().st_size == 0:
            raise ApiError("该 episode 没有视频文件", 404)
        return path

    def dataset_poster(self, name, episode_index):
        path = self.dataset_video_path(name, episode_index)
        key = (str(path), path.stat().st_mtime_ns)
        with self._poster_lock:
            cached = self._poster_cache.get(key)
            if cached is not None:
                self._poster_cache.move_to_end(key)
                return cached
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise ApiError("无法打开 episode 视频", 500)
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok or frame is None:
            raise ApiError("无法读取视频首帧", 500)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ok:
            raise ApiError("视频首帧编码失败", 500)
        data = encoded.tobytes()
        with self._poster_lock:
            self._poster_cache[key] = data
            while len(self._poster_cache) > 12:
                self._poster_cache.popitem(last=False)
        return data

    def validate_dataset_path(self, name):
        path = self._resolve_dataset(name)
        if not (path / "meta" / "info.json").exists():
            raise ApiError(f"不是数据集目录: {path}")
        problems = validate_dataset(str(path))
        self._message("ok" if not problems else "warn",
                      f"数据集 {path.name} 校验: " +
                      ("OK" if not problems else f"{len(problems)} 个问题"))
        return {"path": str(path), "ok": not problems, "problems": problems}

    def calibration_files(self):
        candidates = set()
        for base in (PROJECT_DIR, Path.home(),
                     Path(os.path.expanduser(self.config["output_root"]))):
            try:
                for item in base.glob("*.json"):
                    if item.name in ("stats.json", "info.json", "config.json"):
                        continue
                    candidates.add(item.resolve())
            except OSError:
                continue
        entries = []
        for item in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(item.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and ("knots" in data or "offsets" in data):
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "modified": item.stat().st_mtime,
                    "knots": len(data.get("knots", [])),
                })
        return entries

    def gripper_analyze(self, seconds=2.0):
        if self.recording or self.finalizing:
            raise ApiError("录制中不能运行抖动分析")
        seconds = float(max(0.5, min(10.0, float(seconds))))
        command = [sys.executable, str(PROJECT_DIR / "gripper_calibrate.py"),
                   "analyze", "--seconds", str(seconds), "--hz", "200"]
        with self.device_lock:
            paused = self.gripper
            self.gripper = None
            if paused is not None:
                paused._running = False
                try:
                    paused.join(timeout=1.0)
                except Exception:
                    pass
                paused.close()
            try:
                try:
                    result = subprocess.run(command, cwd=str(PROJECT_DIR),
                                            capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    raise ApiError("抖动分析超时")
                if result.returncode != 0:
                    message = (result.stderr or result.stdout).strip().splitlines()
                    raise ApiError(message[-1] if message else "抖动分析失败")
                try:
                    payload = json.loads(result.stdout)
                except ValueError:
                    raise ApiError("抖动分析输出无法解析")
            finally:
                try:
                    self.gripper = self._new_gripper()
                    self.device_errors.pop("gripper", None)
                except Exception as error:
                    self.device_errors["gripper"] = str(error)
        self._message("info",
                      f"抖动分析: 主频 {payload.get('dominant_hz', 0):.1f} Hz · "
                      f"跨度 {payload.get('span', 0):.0f} 计数")
        return payload

    # -- gripper calibration wizard ---------------------------------------

    def _calibration_angle(self):
        """Stable raw angle for one calibration point (about 0.6 s)."""
        if self.gripper is None:
            raise ApiError("夹爪不可用，无法标定")
        values = []
        for _ in range(3):
            latest = self.gripper.latest()
            if latest is not None:
                _, _, angles = latest
                values.extend(angles or [])
            time.sleep(0.2)
        if not values:
            raise ApiError("夹爪暂无读数")
        if self.gripper.differential:
            return int(round(sum(values) / len(values)))
        return int(round(gripper_calib.circular_mean(values))) % 4096

    def calibration_state(self):
        session = self.calibration_session
        if not session or not session.get("active"):
            return {"active": False}
        index = session["index"]
        steps = session["steps"]
        return {
            "active": True,
            "mode": session["mode"],
            "index": index,
            "total": len(steps),
            "done": index >= len(steps),
            "current": steps[index] if index < len(steps) else None,
            "records": list(session["records"]),
            "levels": session.get("levels"),
            "trips": session.get("trips"),
            "samples": session.get("samples"),
        }

    def calibration_start(self, mode="full", levels=None, trips=3, samples=30,
                          closed_mm=0.0, open_mm=80.0):
        if self.recording or self.finalizing:
            raise ApiError("录制中不能标定")
        if self.session:
            raise ApiError("数据集打开期间不能标定，请先完成并校验")
        if self.gripper is None:
            raise ApiError("夹爪不可用，无法标定")
        mode = str(mode or "full")
        if mode not in ("quick", "full"):
            raise ApiError("标定方式只能是 quick 或 full")
        if mode == "quick":
            try:
                closed_mm = float(closed_mm)
                open_mm = float(open_mm)
            except (TypeError, ValueError):
                raise ApiError("两点标定的宽度必须是数字")
            if not (0.0 <= closed_mm < open_mm <= 300.0):
                raise ApiError("宽度需满足 0 ≤ 全闭 < 全开 ≤ 300 mm")
            steps = [
                {"width_mm": closed_mm, "direction": 1, "label": "全闭"},
                {"width_mm": open_mm, "direction": 0, "label": "全开"},
            ]
            levels = None
            trips = None
            samples = 30
        else:
            if isinstance(levels, str):
                parts = levels.replace("，", ",").split(",")
            elif levels is None:
                parts = []
            else:
                parts = list(levels)
            try:
                levels = sorted({round(float(part), 3) for part in parts
                                 if str(part).strip()})
            except (TypeError, ValueError):
                raise ApiError("宽度级别必须是数字")
            if not 3 <= len(levels) <= 20:
                raise ApiError("宽度级别需要 3–20 个")
            if levels[0] < 0 or levels[-1] > 300:
                raise ApiError("宽度级别需在 0–300 mm")
            try:
                trips = int(trips)
                samples = int(samples)
            except (TypeError, ValueError):
                raise ApiError("重复趟数和采样数必须是整数")
            if not 1 <= trips <= 10:
                raise ApiError("重复趟数需在 1–10")
            if not 10 <= samples <= 200:
                raise ApiError("每点采样数需在 10–200")
            steps = []
            for trip in range(trips):
                for width in levels:
                    steps.append({"width_mm": width, "direction": 0,
                                  "label": "张开", "trip": trip})
                for width in reversed(levels):
                    steps.append({"width_mm": width, "direction": 1,
                                  "label": "夹紧", "trip": trip})
        self.calibration_session = {
            "active": True, "mode": mode, "steps": steps, "records": [],
            "index": 0, "levels": levels, "trips": trips, "samples": samples,
        }
        self._message("info", f"开始夹爪标定（{mode}，共 {len(steps)} 点）")
        return self.calibration_state()

    def calibration_record(self):
        session = self.calibration_session
        if not session or not session.get("active"):
            raise ApiError("没有进行中的标定")
        index = session["index"]
        if index >= len(session["steps"]):
            raise ApiError("所有点已记录，请拟合标定")
        step = session["steps"][index]
        raw = self._calibration_angle()
        session["records"].append({
            "index": index, "width_mm": step["width_mm"],
            "direction": step["direction"], "label": step["label"],
            "raw_angle": raw, "trip": step.get("trip"),
        })
        session["index"] = index + 1
        self._message("info", f"标定点 {index + 1}/{len(session['steps'])}: "
                              f"{step['label']} {step['width_mm']:g} mm → {raw} 计数")
        return self.calibration_state()

    def calibration_undo(self):
        session = self.calibration_session
        if not session or not session.get("active"):
            raise ApiError("没有进行中的标定")
        if not session["records"]:
            raise ApiError("没有可撤销的记录")
        session["records"].pop()
        session["index"] = max(0, session["index"] - 1)
        return self.calibration_state()

    def calibration_cancel(self):
        self.calibration_session = None
        return {"active": False}

    @staticmethod
    def _linear_calibration_model(closed, opened):
        closed_raw = int(closed["raw_angle"])
        open_raw = int(opened["raw_angle"])
        closed_mm = float(closed["width_mm"])
        open_mm = float(opened["width_mm"])
        delta = gripper_calib.wrap_diff(open_raw, closed_raw)
        if delta == 0:
            raise ApiError("全开和全闭的编码器读数相同，请检查夹爪是否真的移动")
        slope = (open_mm - closed_mm) / delta
        intercept = closed_mm - slope * closed_raw
        return {
            "version": 1, "type": "linear_two_point", "units": "mm",
            "counts_per_turn": 4096, "reference": float(closed_raw),
            "knots": [], "segments": [[0.0, slope, intercept]],
            "offsets": {"open": 0.0, "close": 0.0},
            "open_sign": 1 if delta > 0 else -1,
            "direction_window": gripper_calib.DIRECTION_WINDOW,
            "direction_threshold": gripper_calib.DIRECTION_THRESHOLD,
            "levels": [{"width_mm": closed_mm}, {"width_mm": open_mm}],
            "metrics": {"n": 2, "mode": "quick"},
        }

    def calibration_fit(self, name=None):
        session = self.calibration_session
        if not session or not session.get("active"):
            raise ApiError("没有进行中的标定")
        records = session["records"]
        if session["mode"] == "quick":
            closed = next((r for r in records if r["label"] == "全闭"), None)
            opened = next((r for r in records if r["label"] == "全开"), None)
            if closed is None or opened is None:
                raise ApiError("请先记录全闭和全开两个点")
            model = self._linear_calibration_model(closed, opened)
        else:
            if len(records) < 3:
                raise ApiError("记录点太少，至少需要 3 个点")
            try:
                model = gripper_calib.fit(
                    [(r["raw_angle"], r["width_mm"], r["direction"])
                     for r in records])
            except ValueError as error:
                raise ApiError(f"拟合失败: {error}")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip()).strip(".-_")
        if not safe:
            safe = f"gripper_calib_{datetime.now():%Y%m%d_%H%M%S}"
        if not safe.endswith(".json"):
            safe += ".json"
        path = PROJECT_DIR / safe
        gripper_calib.save(model, str(path))
        self._message("ok", f"标定已拟合: {path.name}")
        return {
            "path": str(path), "name": path.name, "mode": session["mode"],
            "metrics": model.get("metrics", {}), "knots": model.get("knots", []),
            "offsets": model.get("offsets"), "open_sign": model.get("open_sign"),
        }

    def calibration_apply(self, path=None):
        if not path:
            raise ApiError("缺少标定文件路径")
        expanded = str(Path(os.path.expanduser(str(path))).resolve())
        self.update_config({"calibration_file": expanded})
        self.calibration_session = None
        self._message("ok", f"已应用夹爪标定: {Path(expanded).name}")
        return {"config": dict(self.config), "calibration_file": expanded}

    # -- lifecycle --------------------------------------------------------

    def close(self):
        self._stop.set()
        with self.lock:
            recording = self.recording
            stop = self._rec_stop
            thread = self.recording_thread
            recorder = self.recorder
            use_tracker = self.rec_use_tracker
            use_gripper = self.rec_use_gripper
            self.recording = False
            if stop is not None:
                stop.set()
        if recording and thread is not None:
            thread.join(timeout=3.0)
        if recording and recorder is not None:
            # Best effort: save the in-flight episode on Ctrl+C.
            self._message("info", "正在保存未完成的 episode…")
            try:
                self._finalize_worker(recorder, use_tracker, use_gripper)
            except Exception:
                pass
        for monitor in (self.camera, self.tracker, self.gripper):
            try:
                if monitor is not None:
                    monitor.close()
            except Exception:
                pass
        try:
            self.telemetry.join(timeout=1.0)
        except Exception:
            pass


def read_qc(root, fps):
    path = Path(root) / "qc" / "episodes.jsonl"
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            frames = record.get("frames") or 0
            if fps:
                record["duration_s"] = round(frames / float(fps), 2)
            records.append(record)
    except OSError:
        return []
    return records


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "UMIWeb/" + VERSION
    protocol_version = "HTTP/1.0"

    # -- helpers ----------------------------------------------------------

    def log_message(self, fmt, *args):
        message = fmt % args
        if ("/api/state" in message or "/api/preview" in message
                or ("/api/datasets" in message and " 200 " in message)
                or (("/api/dataset/video" in message or "/api/dataset/poster" in message)
                    and (" 200 " in message or " 206 " in message))):
            return
        sys.stderr.write(f"{self.address_string()} {message}\n")

    def _authorized(self):
        expected = getattr(self.server, "auth", None)
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        token = "Basic " + base64.b64encode(expected.encode()).decode()
        if header.strip() == token:
            return True
        body = b"Unauthorized\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="UMI Studio"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send(self, status, body, content_type="application/json; charset=utf-8",
              extra_headers=None):
        if not isinstance(body, (bytes, bytearray)):
            body = str(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        extra = extra_headers or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode())

    def _error(self, message, status=400):
        self._json({"error": message}, status)

    def _static(self, relative, content_type=None):
        base = WEB_DIR.resolve()
        target = (base / relative).resolve()
        if base != target and base not in target.parents:
            return self._error("not found", 404)
        if not target.is_file():
            return self._error("not found", 404)
        if content_type is None:
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
                "application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 4_000_000:
            raise ApiError("请求体过大", 413)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode())
        except (UnicodeDecodeError, ValueError):
            raise ApiError("请求体不是有效 JSON")
        if not isinstance(payload, dict):
            raise ApiError("请求体必须是 JSON 对象")
        return payload

    @staticmethod
    def _query_int(query, key, default=None):
        raw = query.get(key, [None])[0]
        if raw is None or raw == "":
            if default is None:
                raise ApiError(f"缺少参数 {key}")
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ApiError(f"参数 {key} 必须是整数")

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        path = url.path
        query = parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/health":
                return self._json({"ok": True, "version": VERSION})
            if path == "/api/state":
                return self._json(self.server.service.state())
            if path == "/api/datasets":
                return self._json({"datasets": self.server.service.list_datasets()})
            if path == "/api/dataset":
                name = query.get("name", [""])[0]
                return self._json(self.server.service.dataset_detail(name))
            if path == "/api/dataset/episodes":
                name = query.get("name", [""])[0]
                return self._json(self.server.service.dataset_episodes(name))
            if path == "/api/dataset/episode":
                name = query.get("name", [""])[0]
                episode = self._query_int(query, "episode")
                max_points = self._query_int(query, "max_points", 4000)
                return self._json(self.server.service.dataset_episode_data(
                    name, episode, max_points))
            if path == "/api/dataset/video":
                name = query.get("name", [""])[0]
                episode = self._query_int(query, "episode")
                return self._video(name, episode)
            if path == "/api/dataset/poster":
                name = query.get("name", [""])[0]
                episode = self._query_int(query, "episode")
                data = self.server.service.dataset_poster(name, episode)
                return self._send(200, data, "image/jpeg", {"Cache-Control": "private, max-age=600"})
            if path == "/api/calibrations":
                return self._json({"files": self.server.service.calibration_files()})
            if path == "/api/calibration":
                return self._json(self.server.service.calibration_state())
            if path == "/api/snapshot.jpg":
                data = self.server.service.latest_jpeg()
                if data is None:
                    return self._error("暂无相机画面", 503)
                return self._send(200, data, "image/jpeg")
            if path == "/api/preview.mjpg":
                return self._mjpeg()
            return self._error("not found", 404)
        except ApiError as error:
            return self._error(str(error), error.status)
        except Exception as error:
            traceback.print_exc()
            return self._error(f"内部错误: {error}", 500)

    def do_POST(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        path = url.path
        service = self.server.service
        try:
            payload = self._body()
            if path == "/api/settings":
                return self._json({"config": service.update_config(payload)})
            if path == "/api/session/new":
                return self._json({"session": service.new_session(payload.get("name"))})
            if path == "/api/session/close":
                return self._json(service.close_session())
            if path == "/api/record/start":
                return self._json(service.start_recording(payload.get("name")))
            if path == "/api/record/stop":
                return self._json(service.stop_recording())
            if path == "/api/devices/reconnect":
                return self._json(service.reconnect())
            if path == "/api/dataset/validate":
                return self._json(service.validate_dataset_path(payload.get("name")))
            if path == "/api/gripper/analyze":
                return self._json(service.gripper_analyze(payload.get("seconds", 2.0)))
            if path == "/api/calibration/start":
                return self._json(service.calibration_start(
                    mode=payload.get("mode", "full"),
                    levels=payload.get("levels"),
                    trips=payload.get("trips", 3),
                    samples=payload.get("samples", 30),
                    closed_mm=payload.get("closed_mm", 0.0),
                    open_mm=payload.get("open_mm", 80.0)))
            if path == "/api/calibration/record":
                return self._json(service.calibration_record())
            if path == "/api/calibration/undo":
                return self._json(service.calibration_undo())
            if path == "/api/calibration/fit":
                return self._json(service.calibration_fit(payload.get("name")))
            if path == "/api/calibration/apply":
                return self._json(service.calibration_apply(payload.get("path")))
            if path == "/api/calibration/cancel":
                return self._json(service.calibration_cancel())
            return self._error("not found", 404)
        except ApiError as error:
            return self._error(str(error), error.status)
        except Exception as error:
            traceback.print_exc()
            return self._error(f"内部错误: {error}", 500)

    def _video(self, name, episode):
        path = self.server.service.dataset_video_path(name, episode)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = (self.headers.get("Range") or "").strip()
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            elif match.group(2):
                start = max(0, size - int(match.group(2)))
                end = size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, max-age=600")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_HEAD(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/api/dataset/video":
                name = query.get("name", [""])[0]
                episode = self._query_int(query, "episode")
                path = self.server.service.dataset_video_path(name, episode)
                size = path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=600")
                self.end_headers()
                return
            if url.path in ("/", "/index.html"):
                target = WEB_DIR / "index.html"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except ApiError as error:
            self.send_response(error.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _mjpeg(self):
        service = self.server.service
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            while True:
                data = service.latest_jpeg()
                if data is None:
                    time.sleep(0.2)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                self.wfile.flush()
                time.sleep(0.06)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class UmiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service, auth=None):
        super().__init__(address, Handler)
        self.service = service
        self.auth = auth


def main():
    parser = argparse.ArgumentParser(
        description="Web console for the UMI synchronized data collector",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; use 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--auth", metavar="USER:PASS",
                        help="optional HTTP Basic auth, e.g. umi:secret")
    parser.add_argument("--no-devices", action="store_true",
                        help="start without opening hardware (UI smoke test)")
    args = parser.parse_args()

    if args.no_devices:
        global DISABLE_HARDWARE
        DISABLE_HARDWARE = True

    config = load_config()
    service = Service(config)
    server = UmiServer((args.host, args.port), service, auth=args.auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"UMI Studio: http://{shown_host}:{args.port}/", flush=True)
    print("若在远端机器，可通过 SSH 隧道访问：", flush=True)
    print(f"  ssh -p 7022 -L {args.port}:127.0.0.1:{args.port} zy@23.254.219.130", flush=True)

    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n正在关闭…", flush=True)
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        try:
            service.close()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
