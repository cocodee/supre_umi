#!/usr/bin/env python3
"""AS5600 gripper width encoder on the CH341 USB-I2C adapter.

The adapter's Linux bus number is not stable across replug, so the bus is
located by adapter name (``ch34x-mphsi-i2c``) rather than hard-coded. Register
semantics follow the AS5600 datasheet; see
docs/devices/umi-gripper-as5600.md.
"""

import ctypes
import fcntl
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ADAPTER_NAME = 'ch34x-mphsi-i2c'
ADDRESS = 0x36
COUNTS = 4096
DEGREES_PER_COUNT = 360.0 / COUNTS


def wrap_diff(value, reference, counts=COUNTS):
    """Signed shortest-arc difference ``value - reference`` in (-counts/2, counts/2]."""
    return ((value - reference + counts // 2) % counts) - counts // 2

REG_STATUS = 0x0B
REG_RAW_ANGLE = 0x0C
REG_ANGLE = 0x0E
REG_AGC = 0x1A
REG_MAGNITUDE = 0x1B

STATUS_MD = 0x20  # magnet detected
STATUS_ML = 0x10  # magnet too weak
STATUS_MH = 0x08  # magnet too strong

I2C_SLAVE = 0x0703
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001


class I2cMsg(ctypes.Structure):
    _fields_ = [('addr', ctypes.c_uint16), ('flags', ctypes.c_uint16),
                ('len', ctypes.c_uint16), ('buf', ctypes.POINTER(ctypes.c_uint8))]


class I2cIoctlData(ctypes.Structure):
    _fields_ = [('msgs', ctypes.POINTER(I2cMsg)), ('nmsgs', ctypes.c_uint32)]


def find_bus(name=ADAPTER_NAME):
    """Return the i2c bus number of the named adapter, or None."""
    for path in sorted(glob.glob('/sys/class/i2c-adapter/i2c-*')):
        try:
            adapter = (Path(path) / 'name').read_text().strip()
        except OSError:
            continue
        if adapter.startswith(name):
            return int(Path(path).name.split('-')[1])
    return None


class As5600:
    """Read-only AS5600 access over /dev/i2c-N using combined transfers."""

    def __init__(self, bus=None, address=ADDRESS):
        self.bus = find_bus() if bus is None else bus
        if self.bus is None:
            raise RuntimeError(
                f'No I2C adapter named {ADAPTER_NAME}; check the CH341 connection '
                'and that the ch34x_mphsi_master module is loaded')
        self.address = address
        self.device = f'/dev/i2c-{self.bus}'
        try:
            self._fd = open(self.device, 'rb+', buffering=0)
        except PermissionError as error:
            raise PermissionError(
                f'Cannot open {self.device}: {error}. Add the user to the "i2c" '
                'group (then re-login) or run this tool with sudo') from error

    def close(self):
        if getattr(self, '_fd', None) is not None:
            self._fd.close()
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, register, length):
        """Read `length` bytes starting at `register` as one combined transfer."""
        write = (ctypes.c_uint8 * 1)(register)
        buffer = (ctypes.c_uint8 * length)()
        messages = (I2cMsg * 2)(
            I2cMsg(self.address, 0, 1, ctypes.cast(write, ctypes.POINTER(ctypes.c_uint8))),
            I2cMsg(self.address, I2C_M_RD, length,
                   ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))),
        )
        fcntl.ioctl(self._fd, I2C_RDWR, I2cIoctlData(messages, 2))
        return bytes(buffer)

    def read12(self, register):
        """Read a 12-bit register pair; the high byte carries only 4 valid bits."""
        high, low = self.read(register, 2)
        return ((high & 0x0F) << 8) | low

    def sample(self):
        """Return one raw observation with a monotonic acquisition timestamp."""
        start = time.monotonic()
        # 0x0B..0x0D = STATUS + RAW ANGLE; 0x1A..0x1C = AGC + MAGNITUDE.
        status_raw = self.read(REG_STATUS, 3)
        status = status_raw[0]
        raw_angle = ((status_raw[1] & 0x0F) << 8) | status_raw[2]
        angle = self.read12(REG_ANGLE)
        agc_mag = self.read(REG_AGC, 3)
        agc = agc_mag[0]
        magnitude = ((agc_mag[1] & 0x0F) << 8) | agc_mag[2]
        end = time.monotonic()
        return {
            'time_monotonic_s': start,
            'time_unix_s': time.time(),
            'read_duration_s': end - start,
            'bus': self.bus,
            'address': self.address,
            'raw_angle': raw_angle,
            'raw_angle_deg': raw_angle * DEGREES_PER_COUNT,
            'angle': angle,
            'status': status,
            'magnet_detected': bool(status & STATUS_MD),
            'magnet_too_weak': bool(status & STATUS_ML),
            'magnet_too_strong': bool(status & STATUS_MH),
            'agc': agc,
            'magnitude': magnitude,
        }


class DualAs5600:
    """Optionally read two AS5600s and expose their wrap-corrected difference.

    The differential signal cancels common-mode disturbance (such as the
    observed ~50 Hz field jitter) and, when the sensors sit on the two gripper
    branches, reduces gear-backlash effects. A second AS5600 cannot share the
    I2C address on one bus, so it must live on a second CH341 adapter
    (``secondary_bus``) or behind an I2C mux. With ``secondary_bus=None`` this
    degrades to a single-sensor reader.
    """

    def __init__(self, bus=None, secondary_bus=None):
        self.primary = As5600(bus=bus)
        self.secondary = As5600(bus=secondary_bus) if secondary_bus is not None else None

    def close(self):
        self.primary.close()
        if self.secondary is not None:
            self.secondary.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def sample(self):
        primary = self.primary.sample()
        if self.secondary is None:
            primary['mode'] = 'single'
            return primary
        secondary = self.secondary.sample()
        difference = wrap_diff(primary['raw_angle'], secondary['raw_angle'])
        sample = dict(primary)
        sample.update({
            'raw_angle_primary': primary['raw_angle'],
            'raw_angle_secondary': secondary['raw_angle'],
            'raw_angle': difference,
            'mode': 'differential',
        })
        return sample


def present():
    """Report CH341/AS5600 presence without opening the device node."""
    bus = find_bus()
    state = {
        'adapter_name': ADAPTER_NAME,
        'bus': bus,
        'device': f'/dev/i2c-{bus}' if bus is not None else None,
        'driver_loaded': Path('/sys/module/ch34x_mphsi_master').exists(),
        'usb_present': _usb_present(),
    }
    state['readable'] = bool(bus is not None and os.access(state['device'], os.R_OK | os.W_OK))
    return state


def _usb_present():
    try:
        result = subprocess.run(['lsusb', '-d', '1a86:5512'], capture_output=True,
                                text=True, timeout=5)
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def calibration_width_mm(raw_angle, closed, opened, span_mm):
    """Map an unwrapped count onto width using two-point endpoint calibration.

    `closed` and `opened` are endpoint counts recorded during calibration and
    `span_mm` the measured width at `opened`. Counts are unwrapped toward the
    shorter arc, so this assumes the travel stays under half a turn.
    """
    if closed == opened:
        raise ValueError('closed and opened counts must differ')
    delta = (raw_angle - closed + COUNTS // 2) % COUNTS - COUNTS // 2
    span = (opened - closed + COUNTS // 2) % COUNTS - COUNTS // 2
    return span_mm * delta / span


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bus', type=int, help='I2C bus number (default: find by adapter name)')
    parser.add_argument('--hz', type=float, default=0.0,
                        help='stream at this rate; 0 reads a single sample (default: 0)')
    parser.add_argument('--count', type=int, default=0, help='stop after N samples (0 = endless)')
    parser.add_argument('--json', action='store_true', help='print JSON lines')
    parser.add_argument('--calib', metavar='CLOSED,OPEN,SPAN_MM',
                        help='add width_mm from endpoint calibration, e.g. 2500,1200,80')
    args = parser.parse_args()

    calibration = None
    if args.calib:
        try:
            closed, opened, span = (float(part) for part in args.calib.split(','))
        except ValueError:
            parser.error('--calib needs CLOSED,OPEN,SPAN_MM')
        calibration = (int(closed), int(opened), span)

    try:
        encoder = As5600(args.bus)
    except (RuntimeError, PermissionError, OSError) as error:
        print(error, file=sys.stderr)
        return 1

    interval = 1.0 / args.hz if args.hz > 0 else 0.0
    emitted = 0
    with encoder:
        try:
            while True:
                try:
                    sample = encoder.sample()
                except OSError as error:
                    print(f'I2C read failed on {encoder.device}: {error}', file=sys.stderr)
                    return 1
                if calibration:
                    sample['width_mm'] = calibration_width_mm(
                        sample['raw_angle'], *calibration)
                if args.json:
                    print(json.dumps(sample), flush=True)
                else:
                    flags = ('magnet' if sample['magnet_detected'] else 'NO MAGNET')
                    if sample['magnet_too_weak']:
                        flags += ' weak'
                    if sample['magnet_too_strong']:
                        flags += ' strong'
                    width = (f" width={sample['width_mm']:7.2f}mm"
                             if 'width_mm' in sample else '')
                    print(f"raw={sample['raw_angle']:4d} "
                          f"({sample['raw_angle_deg']:7.2f}deg){width} "
                          f"agc={sample['agc']:3d} mag={sample['magnitude']:4d} {flags}",
                          flush=True)
                emitted += 1
                if interval == 0.0 or (args.count and emitted >= args.count):
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
