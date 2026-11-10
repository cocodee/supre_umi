#!/usr/bin/env python3
"""Vive Tracker pose and status from the running SteamVR OpenVR runtime.

Requires the `openvr` Python package, which on this machine lives in
/home/zy/tools/streamvr/.venv, and a running SteamVR `vrserver`. See
docs/devices/vive-tracker-steamvr-openvr.md.
"""

import argparse
import json
import subprocess
import sys
import time

try:
    import openvr
except ImportError:  # pragma: no cover - environment dependent
    openvr = None

VENV_PYTHON = '/home/zy/tools/streamvr/.venv/bin/python'

CLASS_NAMES = {}
TRACKING_RESULTS = {}


def _build_tables():
    """Name OpenVR enums once the module is importable."""
    CLASS_NAMES.update({
        openvr.TrackedDeviceClass_Invalid: 'Invalid',
        openvr.TrackedDeviceClass_HMD: 'HMD',
        openvr.TrackedDeviceClass_Controller: 'Controller',
        openvr.TrackedDeviceClass_GenericTracker: 'GenericTracker',
        openvr.TrackedDeviceClass_TrackingReference: 'TrackingReference',
        openvr.TrackedDeviceClass_DisplayRedirect: 'DisplayRedirect',
    })
    TRACKING_RESULTS.update({
        openvr.TrackingResult_Uninitialized: 'Uninitialized',
        openvr.TrackingResult_Calibrating_InProgress: 'Calibrating_InProgress',
        openvr.TrackingResult_Calibrating_OutOfRange: 'Calibrating_OutOfRange',
        openvr.TrackingResult_Running_OK: 'Running_OK',
        openvr.TrackingResult_Running_OutOfRange: 'Running_OutOfRange',
        openvr.TrackingResult_Fallback_RotationOnly: 'Fallback_RotationOnly',
    })


def vrserver_running():
    """True when a SteamVR vrserver process is up; None if pgrep is missing."""
    try:
        result = subprocess.run(['pgrep', '-x', 'vrserver'], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _property(vr, index, prop, kind):
    """Read one device property, returning None when it is not published."""
    getter = {
        'string': vr.getStringTrackedDeviceProperty,
        'float': vr.getFloatTrackedDeviceProperty,
        'bool': vr.getBoolTrackedDeviceProperty,
        'uint64': vr.getUint64TrackedDeviceProperty,
    }[kind]
    try:
        return getter(index, prop)
    except Exception:
        return None


def devices(vr, universe=None):
    """Return every enumerated OpenVR device with its current pose state."""
    if universe is None:
        universe = openvr.TrackingUniverseStanding
    poses = vr.getDeviceToAbsoluteTrackingPose(
        universe, 0, openvr.k_unMaxTrackedDeviceCount)
    found = []
    for index in range(openvr.k_unMaxTrackedDeviceCount):
        device_class = vr.getTrackedDeviceClass(index)
        if device_class == openvr.TrackedDeviceClass_Invalid:
            continue
        pose = poses[index]
        entry = {
            'index': index,
            'class': CLASS_NAMES.get(device_class, str(device_class)),
            'serial': _property(vr, index, openvr.Prop_SerialNumber_String, 'string'),
            'model': _property(vr, index, openvr.Prop_ModelNumber_String, 'string'),
            'connected': bool(pose.bDeviceIsConnected),
            'pose_valid': bool(pose.bPoseIsValid),
            'tracking_result': TRACKING_RESULTS.get(
                pose.eTrackingResult, str(pose.eTrackingResult)),
        }
        if _property(vr, index, openvr.Prop_DeviceProvidesBatteryStatus_Bool, 'bool'):
            battery = _property(vr, index, openvr.Prop_DeviceBatteryPercentage_Float, 'float')
            entry['battery_fraction'] = battery
            entry['charging'] = _property(vr, index, openvr.Prop_DeviceIsCharging_Bool, 'bool')
        if pose.bPoseIsValid:
            matrix = pose.mDeviceToAbsoluteTracking.m
            entry['position_m'] = [matrix[row][3] for row in range(3)]
            entry['rotation_matrix'] = [[matrix[row][col] for col in range(3)]
                                        for row in range(3)]
            entry['velocity_m_s'] = list(pose.vVelocity.v)
            entry['angular_velocity_rad_s'] = list(pose.vAngularVelocity.v)
        found.append(entry)
    return found


def trackers(vr, universe=None):
    """Only the GenericTracker entries."""
    return [device for device in devices(vr, universe) if device['class'] == 'GenericTracker']


def init():
    """Start an OpenVR session as a background application."""
    if openvr is None:
        raise RuntimeError(
            'The openvr Python package is not importable by this interpreter. '
            f'Run this tool with {VENV_PYTHON}')
    _build_tables()
    return openvr.init(openvr.VRApplication_Other)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--universe', choices=('standing', 'raw'), default='standing')
    parser.add_argument('--hz', type=float, default=0.0,
                        help='stream at this rate; 0 reads a single snapshot (default: 0)')
    parser.add_argument('--count', type=int, default=0, help='stop after N samples (0 = endless)')
    parser.add_argument('--trackers-only', action='store_true',
                        help='omit the virtual HMD and base station records')
    parser.add_argument('--json', action='store_true', help='print JSON lines')
    args = parser.parse_args()

    if openvr is None:
        print('The openvr Python package is not importable by this interpreter.\n'
              f'Run: {VENV_PYTHON} {sys.argv[0]} ...', file=sys.stderr)
        return 1
    if vrserver_running() is False:
        print('SteamVR vrserver is not running; start SteamVR before reading poses.',
              file=sys.stderr)
        return 1

    try:
        vr = init()
    except openvr.OpenVRError as error:
        print(f'OpenVR could not start: {error}', file=sys.stderr)
        return 1

    universe = (openvr.TrackingUniverseStanding if args.universe == 'standing'
                else openvr.TrackingUniverseRawAndUncalibrated)
    interval = 1.0 / args.hz if args.hz > 0 else 0.0
    emitted = 0
    try:
        while True:
            listed = (trackers(vr, universe) if args.trackers_only
                      else devices(vr, universe))
            record = {
                'time_unix_s': time.time(),
                'time_monotonic_s': time.monotonic(),
                'universe': args.universe,
                'devices': listed,
            }
            if args.json:
                print(json.dumps(record), flush=True)
            else:
                for device in listed:
                    line = (f"[{device['index']}] {device['class']:17s} "
                            f"{device['serial'] or '?':20s} "
                            f"conn={'Y' if device['connected'] else 'n'} "
                            f"pose={'Y' if device['pose_valid'] else 'n'} "
                            f"{device['tracking_result']}")
                    if 'battery_fraction' in device and device['battery_fraction'] is not None:
                        line += f" batt={device['battery_fraction'] * 100:.0f}%"
                        if device.get('charging'):
                            line += '+chg'
                    if 'position_m' in device:
                        x, y, z = device['position_m']
                        line += f" pos=({x:+.3f},{y:+.3f},{z:+.3f})"
                    print(line, flush=True)
                if not listed:
                    print('No OpenVR devices enumerated.', flush=True)
                print('', flush=True)
            emitted += 1
            if interval == 0.0 or (args.count and emitted >= args.count):
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        openvr.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
