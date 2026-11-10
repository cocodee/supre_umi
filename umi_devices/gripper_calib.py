#!/usr/bin/env python3
"""Gripper width calibration for the AS5600.

Implements the model FOMI (arXiv 2610.19839) uses for gripper calibration:
a piecewise quadratic ``p_k(x)`` with a direction-dependent offset ``c_sigma``
that absorbs hysteresis, continuous at the knots:

    f(x, sigma) = p_k(x) + c_sigma,   x in [knot_k, knot_{k+1})

Knots sit at the median encoder reading of each interior command level. The
fit is a least-squares problem with linear continuity constraints, solved in
the null space of the constraint matrix with numpy only.

Angles are 12-bit counts; all mapping operates on counts unwrapped to the
shortest arc around the calibration reference, so the 4095->0 crossing is
handled as long as the travel stays under half a turn.
"""

import json
import math
from collections import deque

import numpy as np

COUNTS = 4096
DIRECTION_WINDOW = 5
DIRECTION_THRESHOLD = 4.0  # counts over the whole window
OPEN = 0
CLOSE = 1


def wrap_diff(value, reference, counts=COUNTS):
    """Signed shortest-arc difference ``value - reference`` in (-counts/2, counts/2]."""
    return ((value - reference + counts // 2) % counts) - counts // 2


def unwrap_near(value, reference, counts=COUNTS):
    """Return the equivalent of ``value`` closest to ``reference``."""
    return reference + wrap_diff(value, reference, counts)


def circular_mean(angles, counts=COUNTS):
    """Mean of circular angles (counts), robust to the 0/4095 wrap."""
    radians = np.asarray(angles, dtype=np.float64) * (2 * math.pi / counts)
    mean = math.atan2(float(np.sin(radians).mean()), float(np.cos(radians).mean()))
    return (mean % (2 * math.pi)) / (2 * math.pi) * counts % counts


def _constrained_lstsq(design, target, constraints):
    """argmin ||A b - y|| subject to C b = 0, via the null space of C."""
    if constraints.shape[0] == 0:
        return np.linalg.lstsq(design, target, rcond=None)[0]
    _, singular, vt = np.linalg.svd(constraints, full_matrices=True)
    rank = int((singular > 1e-9).sum())
    null = vt[rank:].T
    theta = np.linalg.lstsq(design @ null, target, rcond=None)[0]
    return null @ theta


def _design_matrix(x, sigma, knots):
    segments = len(knots) + 1
    params = 3 * segments + 1
    design = np.zeros((len(x), params))
    segment = np.searchsorted(knots, x, side="right")
    segment = np.clip(segment, 0, segments - 1)
    for i, (value, direction) in enumerate(zip(x, segment)):
        design[i, 3 * direction:3 * direction + 3] = [x[i] ** 2, x[i], 1.0]
        design[i, 3 * segments] = 1.0 if sigma[i] == CLOSE else 0.0
    return design, segment


def _continuity_constraints(knots):
    segments = len(knots) + 1
    params = 3 * segments + 1
    constraints = np.zeros((len(knots), params))
    for k, knot in enumerate(knots):
        constraints[k, 3 * k:3 * k + 3] = [knot ** 2, knot, 1.0]
        constraints[k, 3 * (k + 1):3 * (k + 1) + 3] = [-(knot ** 2), -knot, -1.0]
    return constraints


def _predict(beta, knots, x, sigma):
    segments = len(knots) + 1
    segment = np.clip(np.searchsorted(knots, x, side="right"), 0, segments - 1)
    coefficients = np.stack([beta[3 * s:3 * s + 3] for s in range(segments)])
    a = coefficients[segment, 0]
    b = coefficients[segment, 1]
    c = coefficients[segment, 2]
    offset = np.where(np.asarray(sigma) == CLOSE, beta[3 * segments], 0.0)
    return a * x * x + b * x + c + offset


def fit(samples, direction_window=DIRECTION_WINDOW,
        direction_threshold=DIRECTION_THRESHOLD):
    """Fit the piecewise quadratic hysteresis model.

    ``samples`` is an iterable of ``(raw_angle, width_mm, direction)`` with
    direction ``0`` for opening and ``1`` for closing.
    """
    x_raw = np.asarray([float(s[0]) for s in samples])
    y = np.asarray([float(s[1]) for s in samples])
    sigma = np.asarray([int(s[2]) for s in samples])
    if len(x_raw) < 6:
        raise ValueError("need at least 6 calibration samples")

    reference = float(np.median(x_raw))
    x = np.asarray([unwrap_near(value, reference) for value in x_raw])

    levels = sorted(set(np.round(y, 6)))
    if len(levels) < 3:
        raise ValueError("need at least 3 distinct width levels for a piecewise fit")
    knots = [float(np.median(x[y == level])) for level in levels[1:-1]]
    design, _ = _design_matrix(x, sigma, knots)
    params = design.shape[1]
    if len(x) <= params:
        raise ValueError(
            f"not enough samples: {len(x)} rows for {params} parameters; "
            "add levels or repeat the open/close sweep")

    test_mask = np.zeros(len(x), dtype=bool)
    test_mask[::5] = True

    constraints = _continuity_constraints(knots)
    train_beta = _constrained_lstsq(design[~test_mask], y[~test_mask], constraints)
    test_pred = _predict(train_beta, knots, x[test_mask], sigma[test_mask])
    test_rmse = float(np.sqrt(np.mean((test_pred - y[test_mask]) ** 2)))

    linear_design = np.column_stack([x, (sigma == CLOSE).astype(float), np.ones(len(x))])
    linear_beta = np.linalg.lstsq(linear_design[~test_mask], y[~test_mask], rcond=None)[0]
    linear_pred = linear_design[test_mask] @ linear_beta
    linear_rmse = float(np.sqrt(np.mean((linear_pred - y[test_mask]) ** 2)))

    beta = _constrained_lstsq(design, y, constraints)
    train_pred = _predict(beta, knots, x, sigma)
    train_rmse = float(np.sqrt(np.mean((train_pred - y) ** 2)))

    open_median = [float(np.median(x[(y == level) & (sigma == OPEN)])) for level in levels
                   if np.any((y == level) & (sigma == OPEN))]
    close_median = [float(np.median(x[(y == level) & (sigma == CLOSE)])) for level in levels
                    if np.any((y == level) & (sigma == CLOSE))]
    open_sign = 1 if np.mean(open_median) >= np.mean(close_median) else -1

    segments = [[float(v) for v in beta[3 * s:3 * s + 3]] for s in range(len(knots) + 1)]
    return {
        "version": 1,
        "type": "piecewise_quadratic_hysteresis",
        "units": "mm",
        "counts_per_turn": COUNTS,
        "reference": reference,
        "knots": knots,
        "segments": segments,
        "offsets": {"open": 0.0, "close": float(beta[3 * (len(knots) + 1)])},
        "open_sign": open_sign,
        "direction_window": int(direction_window),
        "direction_threshold": float(direction_threshold),
        "levels": [{"width_mm": float(level)} for level in levels],
        "metrics": {
            "n": int(len(x)),
            "train_rmse_mm": train_rmse,
            "test_rmse_mm": test_rmse,
            "linear_test_rmse_mm": linear_rmse,
        },
    }


def save(model, path):
    with open(path, "w") as handle:
        json.dump(model, handle, indent=2)


def load(path):
    with open(path) as handle:
        return json.load(handle)


class GripperCalibration:
    """Runtime encoder -> width mapping with direction inference."""

    def __init__(self, model):
        self.model = model
        self.knots = list(model["knots"])
        self.segments = [list(map(float, s)) for s in model["segments"]]
        self.offsets = model["offsets"]
        self.reference = float(model["reference"])
        self.open_sign = int(model.get("open_sign", 1))
        self.window = int(model.get("direction_window", DIRECTION_WINDOW))
        self.threshold = float(model.get("direction_threshold", DIRECTION_THRESHOLD))
        self._history = deque(maxlen=self.window)
        self._direction = OPEN

    def reset(self):
        self._history.clear()
        self._direction = OPEN

    def _map(self, x, direction):
        segments = len(self.knots) + 1
        segment = min(max(int(np.searchsorted(self.knots, x, side="right")), 0), segments - 1)
        a, b, c = self.segments[segment]
        offset = self.offsets["close"] if direction == CLOSE else self.offsets["open"]
        return a * x * x + b * x + c + offset

    def update(self, raw_angle):
        """Feed one (smoothed) angle and return the calibrated width in mm."""
        x = unwrap_near(raw_angle, self.reference)
        self._history.append(x)
        if len(self._history) >= 2:
            delta = self._history[-1] - self._history[0]
            if abs(delta) >= self.threshold:
                moving_open = (delta * self.open_sign) > 0
                self._direction = OPEN if moving_open else CLOSE
        return self._map(x, self._direction)


def analyze(samples, rate_hz=None):
    """Summarize jitter: span, std, and dominant frequency of a raw angle series."""
    angles = np.asarray(samples, dtype=np.float64)
    reference = float(np.median(angles))
    unwrapped = np.asarray([unwrap_near(value, reference) for value in angles])
    result = {
        "n": int(len(angles)),
        "min": float(unwrapped.min()),
        "max": float(unwrapped.max()),
        "span": float(unwrapped.max() - unwrapped.min()),
        "std": float(unwrapped.std()),
        "mean": float(unwrapped.mean()),
    }
    if rate_hz and len(unwrapped) >= 8:
        centered = unwrapped - unwrapped.mean()
        spectrum = np.abs(np.fft.rfft(centered))
        frequencies = np.fft.rfftfreq(len(centered), d=1.0 / rate_hz)
        index = int(np.argmax(spectrum[1:]) + 1) if len(spectrum) > 1 else 0
        result["dominant_hz"] = float(frequencies[index])
        result["rate_hz"] = float(rate_hz)
    return result
