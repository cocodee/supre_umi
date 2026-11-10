#!/usr/bin/env python3
"""V4L2 USB camera discovery and a quick live-frame check.

Listing uses `v4l2-ctl` so it works without OpenCV; opening a frame needs cv2.
"""

import glob
import re
import subprocess
import time
from pathlib import Path


def cameras():
    """Return capture devices, excluding V4L2 metadata-only nodes."""
    found = []
    for path in sorted(glob.glob('/dev/video*')):
        try:
            result = subprocess.run(
                ['v4l2-ctl', '-d', path, '--info'], capture_output=True,
                text=True, timeout=3, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        info = result.stdout
        caps = re.search(r'Device Caps\s*:\s*0x[0-9a-fA-F]+\n((?:\s+[^\n]+\n)*)', info)
        if not caps or not re.search(r'\bVideo Capture\b', caps.group(1)):
            continue
        name = re.search(r'^\s*Card type\s*:\s*(.+)$', info, re.MULTILINE)
        bus = re.search(r'^\s*Bus info\s*:\s*(.+)$', info, re.MULTILINE)
        found.append({
            'device': path,
            'name': name.group(1).strip() if name else Path(path).name,
            'bus': bus.group(1).strip() if bus else '',
        })
    return found


def frame_check(path, frames=5, timeout=5.0):
    """Open `path` and measure delivered frames over a short window.

    Returns a dict with resolution, delivered fps and mean capture latency. The
    latency is the gap between consecutive dequeue calls, not sensor exposure
    time, so it bounds the pipeline rate rather than the imaging latency.
    """
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError('OpenCV (cv2) is not importable by this interpreter') from error

    capture = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f'Cannot open {path}; it may be in use')
    try:
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        deadline = time.monotonic() + timeout
        first = None
        for _ in range(10):  # discard warm-up frames
            ok, frame = capture.read()
            if ok and frame is not None:
                first = frame
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f'Cannot read a frame from {path}')
        stamps = []
        got = 0
        last = None
        while got < frames:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            stamps.append(time.monotonic())
            got += 1
            last = frame
            if time.monotonic() > deadline:
                raise RuntimeError(f'Frame delivery stalled on {path}')
        if not stamps:
            raise RuntimeError(f'Cannot read a frame from {path}')
        frames_ok = first if last is None else last
        gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
        return {
            'device': path,
            'width': int(frames_ok.shape[1]),
            'height': int(frames_ok.shape[0]),
            'frames_read': len(stamps),
            'mean_frame_interval_s': (sum(gaps) / len(gaps)) if gaps else None,
            'approx_fps': (len(gaps) / sum(gaps)) if gaps else None,
        }
    finally:
        capture.release()


def status(frames=5):
    """List cameras and optionally check each one for a usable frame."""
    listed = cameras()
    for camera in listed:
        camera['status'] = 'present'
        if frames:
            try:
                camera.update(frame_check(camera['device'], frames=frames))
                camera['status'] = 'ok'
            except RuntimeError as error:
                camera['status'] = str(error)
    return listed


if __name__ == '__main__':
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--frames', type=int, default=5,
                        help='frames to read per device for the rate check (0 = skip)')
    args = parser.parse_args()
    result = status(frames=args.frames)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for camera in result:
            detail = (f"{camera['width']}x{camera['height']} "
                      f"~{camera['approx_fps']:.1f} fps"
                      if camera.get('approx_fps') else camera['status'])
            print(f"{camera['device']}: {camera['name']} ({camera['bus']}) — {detail}")
        if not result:
            print('No video capture devices found.')
    sys.exit(0 if result and all(c['status'] == 'ok' for c in result) else 1)
