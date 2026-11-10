#!/usr/bin/env python3
"""Report the state of the three UMI data-collection devices in one place.

Covers the AS5600 gripper encoder, the Vive Tracker via SteamVR/OpenVR, and the
USB camera. Each device is queried independently, so one failing device still
lets the others report. Run with the system python3; the tracker query is
delegated to the openvr virtualenv automatically.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import camera_v4l2
import gripper_as5600

VENV_PYTHON = '/home/zy/tools/streamvr/.venv/bin/python'
TRACKER_SCRIPT = str(Path(__file__).with_name('tracker_openvr.py'))


def gripper_status(samples=1, settle=0.05):
    """Read the encoder, averaging `samples` readings to damp the observed jitter."""
    state = gripper_as5600.present()
    try:
        with gripper_as5600.As5600() as encoder:
            readings = []
            for _ in range(max(1, samples)):
                readings.append(encoder.sample())
                if samples > 1:
                    time.sleep(settle)
    except (RuntimeError, PermissionError, OSError) as error:
        state['ok'] = False
        state['error'] = str(error)
        return state
    raw = [reading['raw_angle'] for reading in readings]
    latest = readings[-1]
    state.update({
        'ok': latest['magnet_detected'],
        'address': gripper_as5600.ADDRESS,
        'samples': len(readings),
        'raw_angle': latest['raw_angle'],
        'raw_angle_deg': latest['raw_angle_deg'],
        'raw_angle_mean': sum(raw) / len(raw),
        'raw_angle_span': max(raw) - min(raw),
        'magnet_detected': latest['magnet_detected'],
        'magnet_too_weak': latest['magnet_too_weak'],
        'magnet_too_strong': latest['magnet_too_strong'],
        'agc': latest['agc'],
        'magnitude': latest['magnitude'],
    })
    return state


def tracker_status():
    """Query the running SteamVR runtime, via the openvr virtualenv if needed."""
    try:
        import openvr  # noqa: F401
        interpreter = [sys.executable]
    except ImportError:
        interpreter = [VENV_PYTHON]
    try:
        result = subprocess.run(
            interpreter + [TRACKER_SCRIPT, '--json', '--count', '1'],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'error': f'cannot query OpenVR: {error}'}
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        return {'ok': False, 'error': message[-1] if message else 'tracker query failed'}
    try:
        record = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'ok': False, 'error': 'empty tracker output'}
    trackers = [device for device in record['devices']
                if device['class'] == 'GenericTracker']
    connected = [device for device in trackers if device['connected']]
    valid = [device for device in trackers if device['pose_valid']]
    return {
        'ok': bool(valid),
        'vrserver_running': True,
        'tracker_count': len(trackers),
        'connected': len(connected),
        'pose_valid': len(valid),
        'trackers': trackers,
    }


def summarize(gripper, tracker, cameras):
    checks = []
    checks.append(('gripper', bool(gripper.get('ok')),
                   'magnet detected' if gripper.get('ok')
                   else gripper.get('error', 'magnet not detected')))
    checks.append(('tracker', bool(tracker.get('ok')),
                   'valid pose' if tracker.get('ok')
                   else tracker.get('error', 'no valid pose')))
    cameras_ok = bool(cameras) and all(
        c['status'] in ('ok', 'present') for c in cameras)
    checks.append(('camera', cameras_ok,
                   f'{len(cameras)} capture device(s)' if cameras_ok
                   else 'no usable capture device'))
    return [{'device': name, 'ok': ok, 'detail': detail} for name, ok, detail in checks]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    parser.add_argument('--camera-frames', type=int, default=3,
                        help='frames to read per camera; 0 skips the live check (default: 3)')
    parser.add_argument('--avg', type=int, default=8,
                        help='encoder readings to average (default: 8)')
    args = parser.parse_args()

    gripper = gripper_status(samples=args.avg)
    tracker = tracker_status()
    cameras = camera_v4l2.status(frames=args.camera_frames)
    report = {
        'time_unix_s': time.time(),
        'gripper': gripper,
        'tracker': tracker,
        'cameras': cameras,
        'summary': summarize(gripper, tracker, cameras),
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print('== UMI device status ==')
        g = report['gripper']
        if g.get('ok'):
            print(f"gripper : AS5600 on {g['device']} (bus {g['bus']}, addr 0x{g['address']:02x}) "
                  f"raw={g['raw_angle']:4d} mean={g['raw_angle_mean']:6.1f} "
                  f"span={g['raw_angle_span']:4d} agc={g['agc']} mag={g['magnitude']}")
        else:
            print(f"gripper : {g.get('error', 'unavailable')}")
        t = report['tracker']
        if t.get('ok') or t.get('tracker_count') is not None:
            for device in t.get('trackers', []):
                line = (f"tracker : {device['serial']} {device['model'] or ''} "
                        f"conn={'Y' if device['connected'] else 'n'} "
                        f"pose={'Y' if device['pose_valid'] else 'n'} "
                        f"{device['tracking_result']}")
                if device.get('battery_fraction') is not None:
                    line += f" batt={device['battery_fraction'] * 100:.0f}%"
                if 'position_m' in device:
                    x, y, z = device['position_m']
                    line += f" pos=({x:+.3f},{y:+.3f},{z:+.3f})"
                print(line)
            if not t.get('trackers'):
                print('tracker : none enumerated')
        else:
            print(f"tracker : {t.get('error', 'unavailable')}")
        for camera in cameras:
            detail = (f"{camera['width']}x{camera['height']} ~{camera['approx_fps']:.1f} fps"
                      if camera.get('approx_fps') else camera['status'])
            print(f"camera  : {camera['device']} {camera['name']} — {detail}")
        if not cameras:
            print('camera  : none found')
        print('')
        print('== summary ==')
        for check in report['summary']:
            print(f"{check['device']:8s} {'OK ' if check['ok'] else 'FAIL'} {check['detail']}")

    return 0 if all(check['ok'] for check in report['summary']) else 1


if __name__ == '__main__':
    sys.exit(main())
