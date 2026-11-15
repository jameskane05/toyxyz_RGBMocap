"""Curve edits for a recorded face take. The recording in face.json is left as captured."""
import json
from pathlib import Path
import numpy as np
from .storage import atomic_json

EDIT_FILE = 'edit.json'
FOLDER = 'face_capture'
PEAK = 0.02
EPSILON = 0.02
INTERPS = ('auto', 'linear', 'constant', 'free')


def edit_path(root):
    return Path(root) / FOLDER / EDIT_FILE


def _bezier(p0, p1, p2, p3, u):
    o = 1.0 - u
    return o*o*o*p0 + 3*o*o*u*p1 + 3*o*u*u*p2 + u*u*u*p3


def _solve_u(t0, t1, t2, t3, t):
    # Time controls stay ordered, so the cubic is monotonic and binary search is stable.
    lo, hi = 0.0, 1.0
    for _ in range(18):
        mid = (lo + hi) * 0.5
        if _bezier(t0, t1, t2, t3, mid) < t:
            lo = mid
        else:
            hi = mid
    return (lo + hi) * 0.5


def _slope(keys, index):
    key = keys[index]
    if index <= 0:
        nxt = keys[1]
        dt = float(nxt['t']) - float(key['t'])
        return 0.0 if dt <= 1e-8 else (float(nxt['v']) - float(key['v'])) / dt
    if index >= len(keys) - 1:
        prev = keys[index - 1]
        dt = float(key['t']) - float(prev['t'])
        return 0.0 if dt <= 1e-8 else (float(key['v']) - float(prev['v'])) / dt
    prev, nxt = keys[index - 1], keys[index + 1]
    dt_l = float(key['t']) - float(prev['t'])
    dt_r = float(nxt['t']) - float(key['t'])
    chord_l = 0.0 if dt_l <= 1e-8 else (float(key['v']) - float(prev['v'])) / dt_l
    chord_r = 0.0 if dt_r <= 1e-8 else (float(nxt['v']) - float(key['v'])) / dt_r
    if chord_l * chord_r <= 0.0 or dt_l + dt_r <= 1e-8:
        return 0.0
    slope = (float(nxt['v']) - float(prev['v'])) / (dt_l + dt_r)
    limit = 3.0 * min(abs(chord_l), abs(chord_r))
    return max(-limit, min(limit, slope))


def handles_for(keys, index):
    """Handle offsets (dt, dv) from this key. Auto handles are clamped like a graph editor."""
    key = keys[index]
    kind = key.get('interp') or 'auto'
    if kind in ('linear', 'constant') or len(keys) < 2:
        return (0.0, 0.0), (0.0, 0.0)
    if kind == 'free':
        left = key.get('left') or (0.0, 0.0)
        right = key.get('right') or (0.0, 0.0)
        return (float(left[0]), float(left[1])), (float(right[0]), float(right[1]))
    slope = _slope(keys, index)
    if index <= 0:
        dt = float(keys[1]['t']) - float(key['t'])
        return (0.0, 0.0), (dt / 3.0, slope * dt / 3.0)
    if index >= len(keys) - 1:
        dt = float(key['t']) - float(keys[index - 1]['t'])
        return (-dt / 3.0, slope * -dt / 3.0), (0.0, 0.0)
    dt_l = float(key['t']) - float(keys[index - 1]['t'])
    dt_r = float(keys[index + 1]['t']) - float(key['t'])
    return (-dt_l / 3.0, slope * -dt_l / 3.0), (dt_r / 3.0, slope * dt_r / 3.0)


def linear_handles(keys, index):
    """Thirds of the neighboring chords, so a free handle starts out as a straight segment."""
    key = keys[index]
    if index <= 0:
        left = (0.0, 0.0)
    else:
        prev = keys[index - 1]
        left = ((float(prev['t']) - float(key['t'])) / 3.0, (float(prev['v']) - float(key['v'])) / 3.0)
    if index >= len(keys) - 1:
        right = (0.0, 0.0)
    else:
        nxt = keys[index + 1]
        right = ((float(nxt['t']) - float(key['t'])) / 3.0, (float(nxt['v']) - float(key['v'])) / 3.0)
    return left, right


def eval_channel(keys, t):
    if not keys:
        return None
    t = float(t)
    if t <= float(keys[0]['t']):
        return float(keys[0]['v'])
    if t >= float(keys[-1]['t']):
        return float(keys[-1]['v'])
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if float(keys[mid]['t']) <= t:
            lo = mid
        else:
            hi = mid
    left_key, right_key = keys[lo], keys[hi]
    kind = left_key.get('interp') or 'auto'
    if kind == 'constant':
        return float(left_key['v'])
    t0, t3 = float(left_key['t']), float(right_key['t'])
    if t3 <= t0:
        return float(left_key['v'])
    if kind == 'linear':
        u = (t - t0) / (t3 - t0)
        return float(left_key['v']) * (1.0 - u) + float(right_key['v']) * u
    right = handles_for(keys, lo)[1]
    left = handles_for(keys, hi)[0]
    t1 = min(t3, max(t0, t0 + float(right[0])))
    t2 = min(t3, max(t1, t3 + float(left[0])))
    u = _solve_u(t0, t1, t2, t3, t)
    v0, v3 = float(left_key['v']), float(right_key['v'])
    return _bezier(v0, v0 + float(right[1]), v3 + float(left[1]), v3, u)


def sort_keys(keys, nudge=False):
    keys.sort(key=lambda item: float(item['t']))
    if nudge:
        for index in range(1, len(keys)):
            if keys[index]['t'] <= keys[index - 1]['t']:
                keys[index]['t'] = float(keys[index - 1]['t']) + 1e-3
    return keys


def reduce_channel(times, values, epsilon=EPSILON):
    """Keep the ends and every sample the straight chords miss by more than epsilon."""
    times = np.asarray(times, np.float64)
    values = np.asarray(values, np.float64)
    count = len(times)
    if count == 0:
        return []
    if count <= 2:
        return [_key(times[i], values[i]) for i in range(count)]
    keep = np.zeros(count, np.bool_)
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        span = float(times[end] - times[start])
        v0, v1 = float(values[start]), float(values[end])
        worst = 0.0
        mid = start
        for index in range(start + 1, end):
            u = 0.0 if span <= 1e-8 else (float(times[index]) - float(times[start])) / span
            dist = abs(float(values[index]) - (v0 * (1.0 - u) + v1 * u))
            if dist > worst:
                worst = dist
                mid = index
        if worst > epsilon:
            keep[mid] = True
            stack.append((start, mid))
            stack.append((mid, end))
    return [_key(times[index], values[index]) for index in range(count) if keep[index]]


def _key(t, v, interp='auto'):
    return {'t': float(t), 'v': float(v), 'interp': interp}


def _clean_key(key):
    if not isinstance(key, dict):
        return None
    try:
        item = {'t': float(key['t']), 'v': float(key['v']), 'interp': key.get('interp') or 'auto'}
    except (KeyError, TypeError, ValueError):
        return None
    if item['interp'] not in INTERPS:
        item['interp'] = 'auto'
    if item['interp'] == 'free':
        left = key.get('left') or (0.0, 0.0)
        right = key.get('right') or (0.0, 0.0)
        try:
            item['left'] = [float(left[0]), float(left[1])]
            item['right'] = [float(right[0]), float(right[1])]
        except (TypeError, ValueError, IndexError):
            item['interp'] = 'auto'
    return item


class FaceCurves:
    def __init__(self, channels=None):
        self.channels = {}
        for name, keys in (channels or {}).items():
            cleaned = [item for item in (_clean_key(key) for key in keys or []) if item]
            if cleaned:
                self.channels[str(name)] = sort_keys(cleaned)

    def empty(self):
        return not self.channels

    def keys(self, name):
        return self.channels.get(name) or []

    def clear(self, name=None):
        if name is None:
            self.channels.clear()
        else:
            self.channels.pop(str(name), None)

    def overlay(self, names, t, raw):
        """Replace raw samples that fall between the first and last key of a channel."""
        raw = np.array(np.asarray(raw, np.float32), np.float32, copy=True)
        if not self.channels:
            return raw
        t = float(t)
        for index, name in enumerate(names):
            keys = self.channels.get(name)
            if not keys or index >= len(raw):
                continue
            if t < float(keys[0]['t']) - 1e-4 or t > float(keys[-1]['t']) + 1e-4:
                continue
            value = eval_channel(keys, t)
            if value is not None:
                raw[index] = value
        return raw

    def dump(self):
        channels = {}
        for name, keys in self.channels.items():
            rows = []
            for key in keys:
                item = {
                    't': round(float(key['t']), 4),
                    'v': round(float(key['v']), 4),
                    'interp': key.get('interp') or 'auto',
                }
                if item['interp'] == 'free':
                    left = key.get('left') or (0.0, 0.0)
                    right = key.get('right') or (0.0, 0.0)
                    item['left'] = [round(float(left[0]), 4), round(float(left[1]), 4)]
                    item['right'] = [round(float(right[0]), 4), round(float(right[1]), 4)]
                rows.append(item)
            if rows:
                channels[name] = rows
        return {'version': 1, 'channels': channels}


def active_names(names, values, curves=None, peak=PEAK):
    """Channels that moved in the recording, plus any channel that already has keys."""
    names = [str(name) for name in names]
    values = np.asarray(values, np.float32)
    chosen = []
    if values.size and values.ndim == 2:
        peaks = np.max(np.abs(values), axis=0)
        for index, name in enumerate(names):
            if index < len(peaks) and float(peaks[index]) > peak:
                chosen.append(name)
    if curves is not None:
        for name in curves.channels:
            if name in names and name not in chosen:
                chosen.append(name)
    return chosen


def load_edit(root):
    path = edit_path(root)
    if not path.is_file():
        return FaceCurves()
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return FaceCurves()
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return FaceCurves()
    channels = data.get('channels') if isinstance(data, dict) else None
    return FaceCurves(channels if isinstance(channels, dict) else None)


def save_edit(root, curves):
    path = edit_path(root)
    if curves is None or curves.empty():
        path.unlink(missing_ok=True)
        return path
    atomic_json(path, curves.dump() if isinstance(curves, FaceCurves) else curves)
    return path
