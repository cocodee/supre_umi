#!/usr/bin/env python3
"""Calibrate and analyze the AS5600 gripper encoder.

Subcommands:
  record   sweep known widths in both directions and save raw samples to CSV
  fit      fit the piecewise-quadratic hysteresis model and write a JSON model
  analyze  summarize the static jitter (span, std, dominant frequency)
  selftest fit the solver on synthetic data (no hardware)

`record` needs a TTY and i2c access (run with `sg i2c -c`).
"""

import argparse
import csv
import json
import math
import sys
import time

import gripper_as5600
import gripper_calib


def circular_mean(values):
    return gripper_calib.circular_mean(values)


def record(args):
    if not sys.stdin.isatty():
        print("record needs a TTY", file=sys.stderr)
        return 1
    levels = [float(v) for v in args.levels.split(",")]
    try:
        encoder = gripper_as5600.As5600()
    except (RuntimeError, PermissionError, OSError) as error:
        print(f"cannot open AS5600: {error}", file=sys.stderr)
        return 1

    rows = []
    with encoder:
        for trip in range(args.trips):
            # opening sweep: ascending levels, approach from closed side
            for index, width in enumerate(levels):
                direction = gripper_calib.OPEN
                prompt = (f"trip {trip + 1}/{args.trips} opening -> set {width:g} mm, "
                          "then press Enter")
                input(prompt)
                angles = sample_block(encoder, args.samples, args.hz)
                rows.append((width, direction, circular_mean(angles), time.time()))
            # closing sweep: descending levels, approach from open side
            for width in reversed(levels):
                direction = gripper_calib.CLOSE
                prompt = (f"trip {trip + 1}/{args.trips} closing -> set {width:g} mm, "
                          "then press Enter")
                input(prompt)
                angles = sample_block(encoder, args.samples, args.hz)
                rows.append((width, direction, circular_mean(angles), time.time()))

    with open(args.out, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["width_mm", "direction", "raw_angle", "timestamp"])
        for width, direction, angle, stamp in rows:
            writer.writerow([f"{width:.4f}", direction, f"{angle:.3f}", f"{stamp:.6f}"])
    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


def sample_block(encoder, count, hz, settle=0.15):
    time.sleep(settle)  # let the mechanism settle after the user moves it
    angles = []
    for _ in range(count):
        angles.append(encoder.sample()["raw_angle"])
        if hz > 0:
            time.sleep(1.0 / hz)
    return angles


def load_csv(path):
    samples = []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            samples.append((float(row["raw_angle"]), float(row["width_mm"]),
                            int(row["direction"])))
    return samples


def fit(args):
    try:
        samples = load_csv(args.input)
    except OSError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        model = gripper_calib.fit(samples, direction_threshold=args.threshold)
    except ValueError as error:
        print(f"fit failed: {error}", file=sys.stderr)
        return 1
    gripper_calib.save(model, args.out)
    metrics = model["metrics"]
    print(json.dumps(metrics, indent=2))
    print(f"knots: {[round(k, 1) for k in model['knots']]}")
    print(f"hysteresis offset (close): {model['offsets']['close']:.3f} mm")
    print(f"open_sign: {model['open_sign']}  -> wrote {args.out}")
    return 0


def analyze(args):
    try:
        encoder = gripper_as5600.As5600()
    except (RuntimeError, PermissionError, OSError) as error:
        print(f"cannot open AS5600: {error}", file=sys.stderr)
        return 1
    angles = []
    stamps = []
    with encoder:
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            angles.append(encoder.sample()["raw_angle"])
            stamps.append(time.monotonic())
    actual_rate = ((len(stamps) - 1) / (stamps[-1] - stamps[0])
                   if len(stamps) > 1 else args.hz)
    result = gripper_calib.analyze(angles, rate_hz=actual_rate)
    print(json.dumps(result, indent=2))
    return 0


def selftest(args):
    """Fit synthetic data with a known hysteresis offset and check recovery."""
    import random
    random.seed(0)
    levels = [0, 10, 20, 30, 40, 50, 60, 70, 80]
    sequence = ([(width, gripper_calib.OPEN) for width in levels]
                + [(width, gripper_calib.CLOSE) for width in reversed(levels)])
    samples = []
    for _ in range(3):
        for width, direction in sequence:
            x = 1200 + width * 18 + (2.5 if direction == gripper_calib.CLOSE else -2.5)
            x += random.gauss(0, 0.3)
            samples.append((x % 4096, width, direction))
    model = gripper_calib.fit(samples)
    metrics = model["metrics"]
    print(json.dumps(metrics, indent=2))
    ok = metrics["test_rmse_mm"] < metrics["linear_test_rmse_mm"] and metrics["test_rmse_mm"] < 1.0
    print("selftest:", "OK" if ok else "FAIL")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="sweep widths and save raw samples")
    rec.add_argument("--out", required=True)
    rec.add_argument("--levels", default="0,10,20,30,40,50,60,70,80",
                     help="known widths in mm, comma separated")
    rec.add_argument("--trips", type=int, default=3)
    rec.add_argument("--samples", type=int, default=100,
                     help="readings averaged per point (default: 100)")
    rec.add_argument("--hz", type=float, default=0.0,
                     help="readings per second; 0 = as fast as the I2C bus allows")
    rec.set_defaults(func=record)

    f = sub.add_parser("fit", help="fit the model from CSV")
    f.add_argument("--input", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--threshold", type=float, default=gripper_calib.DIRECTION_THRESHOLD)
    f.set_defaults(func=fit)

    a = sub.add_parser("analyze", help="summarize static jitter")
    a.add_argument("--seconds", type=float, default=3.0)
    a.add_argument("--hz", type=float, default=200.0)
    a.set_defaults(func=analyze)

    s = sub.add_parser("selftest", help="fit synthetic data")
    s.set_defaults(func=selftest)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
