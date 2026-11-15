"""Face-mocap JSON (v1-v6) plus optional sibling audio, stored in the project."""
from pathlib import Path
import json
import shutil
import wave
from bisect import bisect_right
import numpy as np
from .face_curves import edit_path, load_edit
from .storage import atomic_json

ARKIT_NAMES = (
    'eyeBlinkLeft','eyeLookDownLeft','eyeLookInLeft','eyeLookOutLeft','eyeLookUpLeft',
    'eyeSquintLeft','eyeWideLeft','eyeBlinkRight','eyeLookDownRight','eyeLookInRight',
    'eyeLookOutRight','eyeLookUpRight','eyeSquintRight','eyeWideRight',
    'jawForward','jawLeft','jawRight','jawOpen','mouthClose','mouthFunnel',
    'mouthPucker','mouthLeft','mouthRight','mouthSmileLeft','mouthSmileRight',
    'mouthFrownLeft','mouthFrownRight','mouthDimpleLeft','mouthDimpleRight',
    'mouthStretchLeft','mouthStretchRight','mouthRollLower','mouthRollUpper',
    'mouthShrugUpper','mouthShrugLower','mouthPressLeft','mouthPressRight',
    'mouthLowerDownLeft','mouthLowerDownRight','mouthUpperUpLeft','mouthUpperUpRight',
    'browDownLeft','browDownRight','browInnerUp','browOuterUpLeft','browOuterUpRight',
    'cheekPuff','cheekSquintLeft','cheekSquintRight','noseSneerLeft','noseSneerRight',
)
ARKIT_SET = {name.casefold(): i for i, name in enumerate(ARKIT_NAMES)}
FOLDER = 'face_capture'
META = 'meta.json'
FACE_FILE = 'face.json'
AUDIO_NAMES = ('audio.wav','audio.mp3','audio.m4a','audio.flac','audio.ogg')


def canonicalize(name):
    key = ''.join(ch for ch in name if ch.isalnum()).casefold()
    # Quest face-expression names (XR_FB_face_tracking2) onto ARKit.
    aliases = {
        'blinkl':'eyeblinkleft','blinkleft':'eyeblinkleft',
        'blinkr':'eyeblinkright','blinkright':'eyeblinkright',
        'browlowererl':'browdownleft','browlowererr':'browdownright',
        'innerbrowraiserl':'browinnerup','innerbrowraiserr':'browinnerup',
        'outerbrowraiserl':'browouterupleft','outerbrowraiserr':'browouterupright',
        'cheekpuffl':'cheekpuff','cheekpuffr':'cheekpuff',
        'cheekraiserl':'cheeksquintleft','cheekraiserr':'cheeksquintright',
        'eyesclosedl':'eyeblinkleft','eyesclosedr':'eyeblinkright',
        'eyeblinkl':'eyeblinkleft','eyeblinkr':'eyeblinkright',
        'eyeslookdownl':'eyelookdownleft','eyeslookdownr':'eyelookdownright',
        'eyeslookupl':'eyelookupleft','eyeslookupr':'eyelookupright',
        'eyeslookleftl':'eyelookoutleft','eyeslookleftr':'eyelookinright',
        'eyeslookrightl':'eyelookinleft','eyeslookrightr':'eyelookoutright',
        'lidtightenerl':'eyesquintleft','lidtightenerr':'eyesquintright',
        'upperlidraiserl':'eyewideleft','upperlidraiserr':'eyewideright',
        'jawdrop':'jawopen','jawthrust':'jawforward',
        'jawsidewaysleft':'jawleft','jawsidewaysright':'jawright',
        'lipcornerpullerl':'mouthsmileleft','lipcornerpullerr':'mouthsmileright',
        'lipcornerdepressorl':'mouthfrownleft','lipcornerdepressorr':'mouthfrownright',
        'lipfunnelerlb':'mouthfunnel','lipfunnelerlt':'mouthfunnel',
        'lipfunnelerrb':'mouthfunnel','lipfunnelerrt':'mouthfunnel',
        'lippuckerl':'mouthpucker','lippuckerr':'mouthpucker',
        'lipstretcherl':'mouthstretchleft','lipstretcherr':'mouthstretchright',
        'lipsucklb':'mouthrolllower','lipsuckrb':'mouthrolllower',
        'lipsucklt':'mouthrollupper','lipsuckrt':'mouthrollupper',
        'lippressorl':'mouthpressleft','lippressorr':'mouthpressright',
        'lipstoward':'mouthclose',
        'lowerlipdepressorl':'mouthlowerdownleft','lowerlipdepressorr':'mouthlowerdownright',
        'upperlipraiserl':'mouthupperupleft','upperlipraiserr':'mouthupperupright',
        'chinraiserb':'mouthshruglower','chinraisert':'mouthshruglower',
        'dimplerl':'mouthdimpleleft','dimplerr':'mouthdimpleright',
        'nosewrinklerl':'nosesneerleft','nosewrinklerr':'nosesneerright',
    }
    key = aliases.get(key, key)
    if key in ARKIT_SET:
        return ARKIT_SET[key]
    for label, index in ARKIT_SET.items():
        if key.endswith(label) or label in key:
            return index
    return None


def folder(root):
    return Path(root)/FOLDER


def meta_path(root):
    return folder(root)/META


def read_meta(root):
    path = meta_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def face_path(root, meta=None):
    meta = meta if meta is not None else read_meta(root)
    if not meta or not meta.get('face'):
        return None
    path = (folder(root)/meta['face']).resolve()
    return path if path.is_file() and path.is_relative_to(folder(root).resolve()) else None


def audio_path(root, meta=None):
    meta = meta if meta is not None else read_meta(root)
    if not meta or not meta.get('audio'):
        return None
    path = (folder(root)/meta['audio']).resolve()
    return path if path.is_file() and path.is_relative_to(folder(root).resolve()) else None


def _audio_rel(root, name):
    name = str(name or '').replace('\\', '/')
    if not name:
        return None
    base = folder(root).resolve()
    path = (base / name).resolve()
    try:
        if path.is_file() and path.is_relative_to(base):
            return path.relative_to(base).as_posix()
    except OSError:
        return None
    return None


def audio_library(root, meta=None):
    """Original recording, then each saved voice variation."""
    meta = meta if meta is not None else read_meta(root) or {}
    base = folder(root)
    takes = []
    seen = set()

    def add(name, label, kind):
        file = _audio_rel(root, name)
        if not file or file in seen:
            return
        if not file.endswith('.wav') and _audio_rel(root, Path(file).with_suffix('.wav').as_posix()):
            return
        seen.add(file)
        takes.append({'label': label or Path(file).stem, 'kind': kind, 'file': file})

    recorded = str(meta.get('original_audio') or '').replace('\\', '/')
    if recorded.startswith('voices/') or not _audio_rel(root, recorded):
        recorded = 'audio.wav' if (base / 'audio.wav').is_file() else ''
    if not recorded:
        current = str(meta.get('audio') or '').replace('\\', '/')
        if current and not current.startswith('voices/'):
            recorded = current
    if recorded:
        add(recorded, 'Original recording', 'original')
    for item in meta.get('audio_takes') or []:
        if isinstance(item, dict):
            add(item.get('file'), item.get('label'), item.get('kind') or 'generated')
    voice_dir = base / 'voices'
    if voice_dir.is_dir():
        for path in sorted(voice_dir.iterdir()):
            if path.is_file():
                add(path.relative_to(base).as_posix(), path.stem, 'generated')
    current = str(meta.get('audio') or '').replace('\\', '/')
    if current:
        add(current, Path(current).stem, 'generated' if current.startswith('voices/') else 'original')
    return takes


def sibling_audio(source):
    source = Path(source)
    for suffix in ('.audio.mp3','.audio.wav','.audio.m4a','.wav','.mp3','.m4a'):
        if suffix.startswith('.audio'):
            candidate = source.with_name(source.stem+suffix)
        else:
            candidate = source.with_suffix(suffix)
        if candidate.is_file() and candidate != source:
            return candidate
    return None


def _face_track(record):
    if not isinstance(record, dict):
        raise ValueError('Face capture must be a JSON object.')
    version = int(record.get('version', 1))
    if version not in range(1, 7):
        raise ValueError('Unsupported face-capture version %s.' % version)
    if version == 5:
        face = (record.get('tracks') or {}).get('face')
        if not face:
            raise ValueError('This recording has no face track.')
        names = list(face.get('names') or ARKIT_NAMES)
        frames = face.get('frames') or []
    else:
        names = list(record.get('names') or ARKIT_NAMES)
        frames = record.get('frames') or []
    if not frames:
        raise ValueError('Face capture has no frames.')
    names = [str(name) for name in names]
    if len(names) < 1 or len(names) > 128:
        raise ValueError('Face capture has an invalid blendshape list.')
    # Version 6 stores the tracker's own columns. Older files collapse into ARKit.
    native = version >= 6
    width = len(names) if native else len(ARKIT_NAMES)
    times = np.empty(len(frames), np.float64)
    values = np.zeros((len(frames), width), np.float32)
    index = None if native else [canonicalize(name) for name in names]
    for i, row in enumerate(frames):
        if isinstance(row, dict):
            t = float(row['t'])
            src = row.get('values') or []
        elif isinstance(row, (list, tuple)) and row:
            t = float(row[0])
            src = row[1:1+len(names)]
        else:
            raise ValueError('Invalid face keyframe.')
        times[i] = t
        if native:
            for j in range(min(width, len(src))):
                values[i, j] = float(src[j])
        else:
            for j, dest in enumerate(index):
                if dest is None or j >= len(src):
                    continue
                values[i, dest] = float(src[j])
    order = np.argsort(times, kind='stable')
    return dict(fps=float(record.get('fps') or 30),
                names=list(names) if native else list(ARKIT_NAMES),
                times=times[order], values=values[order])


def load_file(path):
    path = Path(path)
    if path.stat().st_size > 64*1024**2:
        raise ValueError('Face capture is larger than 64 MB.')
    record = json.loads(path.read_text(encoding='utf-8'))
    return _face_track(record)


class FaceTake:
    def __init__(self, track, offset=0.0):
        self.fps = track['fps']
        self.names = track['names']
        self.times = np.asarray(track['times'], np.float64)
        self.values = np.asarray(track['values'], np.float32)
        self.offset = float(offset)
        self.duration = float(self.times[-1]) if len(self.times) else 0.0
        self.curves = None
        self.arkit = list(self.names) == list(ARKIT_NAMES)

    def raw_at(self, scene_time):
        t = float(scene_time) - self.offset
        width = self.values.shape[1] if len(self.values) else len(self.names)
        if not len(self.times):
            return np.zeros(width, np.float32)
        if t <= self.times[0]:
            return np.array(self.values[0], np.float32, copy=True)
        if t >= self.times[-1]:
            return np.array(self.values[-1], np.float32, copy=True)
        i = bisect_right(self.times, t) - 1
        t0, t1 = float(self.times[i]), float(self.times[i+1])
        a = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return np.asarray(self.values[i]*(1-a) + self.values[i+1]*a, np.float32)

    def channels_at(self, scene_time):
        raw = np.asarray(self.raw_at(scene_time), np.float32).copy()
        curves = self.curves
        if curves is not None and not curves.empty():
            raw = curves.overlay(self.names, float(scene_time) - self.offset, raw)
        return raw

    def as_arkit(self, channels):
        channels = np.asarray(channels, np.float32)
        if self.arkit and len(channels) == len(ARKIT_NAMES):
            return np.array(channels, np.float32, copy=True)
        out = np.zeros(len(ARKIT_NAMES), np.float32)
        for index, name in enumerate(self.names):
            if index >= len(channels):
                break
            dest = canonicalize(name)
            if dest is not None:
                out[dest] = max(float(out[dest]), float(channels[index]))
        return out

    def sample(self, scene_time):
        if not len(self.times):
            return np.zeros(len(ARKIT_NAMES), np.float32)
        return self.as_arkit(self.channels_at(scene_time))

    def activity_peaks(self):
        if not len(self.times):
            return np.zeros(0, np.float32)
        if self.curves is None or self.curves.empty():
            return self.values.max(axis=1)
        peaks = np.empty(len(self.times), np.float32)
        for index, t in enumerate(self.times):
            peaks[index] = float(np.max(self.channels_at(float(t) + self.offset)))
        return peaks


def activity_wave(times, peaks, bins=160):
    count = len(times)
    if count < 1:
        return None
    start, end = float(times[0]), float(times[-1])
    span = max(1e-3, end - start)
    wave = [0.0] * bins
    last = bins - 1
    for t, peak in zip(times, peaks):
        index = int((float(t) - start) / span * last)
        if index < 0:
            index = 0
        elif index > last:
            index = last
        peak = float(peak)
        if peak > wave[index]:
            wave[index] = peak
    return wave


def wave_from_wav(path, bins=160):
    path = Path(path)
    try:
        with wave.open(str(path)) as handle:
            channels = handle.getnchannels()
            if handle.getsampwidth() != 2 or channels < 1:
                return None
            frames = handle.getnframes()
            raw = handle.readframes(frames)
    except (wave.Error, OSError):
        return None
    if frames < 2 or len(raw) < 4:
        return None
    samples = np.abs(np.frombuffer(raw, dtype='<i2').astype(np.float32)) / 32768.0
    if channels > 1:
        usable = (len(samples) // channels) * channels
        samples = samples[:usable].reshape(-1, channels).max(axis=1)
    if len(samples) < 2:
        return None
    index = np.minimum((np.arange(len(samples)) * bins / len(samples)).astype(np.int32), bins - 1)
    levels = np.zeros(bins, np.float32)
    np.maximum.at(levels, index, samples)
    peak = float(levels.max())
    # The row is ~20px, so a linear peak of 0.15 (enough to fill the meter) is
    # only a few pixels, and quieter speech in a loud clip disappears. Scale to
    # this clip, then sqrt. Exact silence stays on the baseline.
    if peak >= 0.001:
        levels = np.sqrt(levels / peak)
    return levels.tolist()


def wave_from_rows(rows, bins=160):
    if not rows:
        return None
    peaks = [max(row[1:]) if len(row) > 1 else 0.0 for row in rows]
    return activity_wave([row[0] for row in rows], peaks, bins)


def _alnum(name):
    return ''.join(ch for ch in name if ch.isalnum()).casefold()


def merge_alias_deltas(target, options):
    """Keep one delta per vertex when several shape keys share an ARKit name."""
    exact = _alnum(target)
    count = len(options[0][1])
    chosen = np.zeros((count, 3), np.float32)
    rank = np.full(count, -1, np.int8)
    size = np.zeros(count, np.float32)
    ordered = sorted(options, key=lambda item: _alnum(item[0]) != exact)
    for name, delta in ordered:
        priority = np.int8(1 if _alnum(name) == exact else 0)
        mag = np.linalg.norm(delta, axis=1)
        take = (mag > 1e-5) & ((priority > rank) | ((priority == rank) & (mag > size)))
        chosen[take] = delta[take]
        rank[take] = priority
        size[take] = mag[take]
    return chosen


def collapse_repeated_morphs(rows, ids, deltas):
    """Drop a second copy of the same morph on one vertex. Keep the larger delta."""
    rows = np.asarray(rows, np.uint32)
    ids = np.asarray(ids, np.int32)
    deltas = np.asarray(deltas, np.float32)
    if ids.size == 0:
        return rows, ids, deltas
    out_ids = []
    out_deltas = []
    out_rows = np.zeros_like(rows)
    cursor = 0
    repeated = False
    for vertex, (start, count) in enumerate(rows):
        start = int(start)
        count = int(count)
        chosen = {}
        order = []
        for k in range(count):
            morph = int(ids[start + k])
            value = deltas[start + k]
            previous = chosen.get(morph)
            if previous is None:
                chosen[morph] = value
                order.append(morph)
            else:
                repeated = True
                if float(np.dot(value, value)) > float(np.dot(previous, previous)):
                    chosen[morph] = value
        out_rows[vertex] = (cursor, len(order))
        for morph in order:
            out_ids.append(morph)
            out_deltas.append(chosen[morph])
        cursor += len(order)
    if not repeated:
        return rows, ids, deltas
    return out_rows, np.asarray(out_ids, np.int32), np.asarray(out_deltas, np.float32)


def weights_from_sample(sample, names):
    if not names:
        return None
    out = np.zeros(len(names), np.float32)
    for i, name in enumerate(names):
        index = canonicalize(name)
        if index is not None:
            out[i] = sample[index]
    return out


def weights_for(take, names, scene_time):
    return weights_from_sample(take.sample(scene_time), names)


def import_files(root, face_source, audio_source=None, offset=0.0):
    root = Path(root)
    dest = folder(root)
    dest.mkdir(parents=True, exist_ok=True)
    track = load_file(face_source)
    target = dest/FACE_FILE
    shutil.copy2(face_source, target)
    audio_name = None
    audio_source = Path(audio_source) if audio_source else sibling_audio(face_source)
    if audio_source:
        suffix = audio_source.suffix.lower() or '.wav'
        audio_name = 'audio'+suffix
        shutil.copy2(audio_source, dest/audio_name)
    atomic_json(dest/META, dict(version=1, face=FACE_FILE, audio=audio_name,
        offset=float(offset), frames=int(len(track['times'])), duration=float(track['times'][-1]),
        fps=track['fps']))
    edit_path(root).unlink(missing_ok=True)
    return FaceTake(track, offset)


def import_audio(root, audio_source):
    root = Path(root)
    dest = folder(root)
    dest.mkdir(parents=True, exist_ok=True)
    audio_source = Path(audio_source)
    suffix = audio_source.suffix.lower() or '.wav'
    name = 'audio'+suffix
    shutil.copy2(audio_source, dest/name)
    meta = read_meta(root) or dict(version=1, face=None, offset=0.0)
    meta['audio'] = name
    meta.pop('audio_crop', None)
    meta.pop('audio_edits', None)
    atomic_json(dest/META, meta)
    return dest/name


def save_recording(root, frames, fps=30.0, offset=0.0, names=None):
    if not frames:
        raise ValueError('No face frames were received.')
    names = [str(name) for name in names] if names else list(ARKIT_NAMES)
    if len(names) < 1 or len(names) > 128:
        raise ValueError('Face capture has an invalid blendshape list.')
    width = len(names)
    rows = []
    for row in frames:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        try:
            stamp = round(float(row[0]), 4)
            vals = [round(float(v), 4) for v in list(row[1:1+width])]
        except (TypeError, ValueError):
            continue
        if len(vals) < width:
            vals.extend([0.0] * (width - len(vals)))
        rows.append([stamp, *vals])
    if not rows:
        raise ValueError('No face frames were received.')
    dest = folder(root)
    dest.mkdir(parents=True, exist_ok=True)
    version = 4 if names == list(ARKIT_NAMES) else 6
    record = dict(version=version, fps=float(fps), names=names, frames=rows)
    (dest/FACE_FILE).write_text(json.dumps(record, separators=(',', ':')), encoding='utf-8')
    meta = read_meta(root) or dict(version=1)
    meta.update(face=FACE_FILE, offset=float(offset), frames=len(rows),
                duration=float(rows[-1][0]), fps=float(fps))
    atomic_json(dest/META, meta)
    edit_path(root).unlink(missing_ok=True)
    return dest/FACE_FILE


def load_take(root):
    meta = read_meta(root)
    path = face_path(root, meta)
    if path is None:
        return None, meta
    return FaceTake(load_file(path), float((meta or {}).get('offset', 0))), meta


def clip_recording(root, dest, start, end):
    """Copy the face track for the video range [start, end) into dest, so start becomes 0."""
    take, _meta = load_take(root)
    if take is None:
        return 0
    take.curves = load_edit(root)
    rows = []
    start, end = float(start), float(end)
    for t in take.times:
        scene = float(t) + take.offset
        if start - 1e-4 <= scene < end - 1e-4:
            channels = take.channels_at(scene)
            rows.append([round(scene - start, 4), *[round(float(v), 4) for v in channels]])
    if not rows:
        raise ValueError('The selected range has no face frames.')
    save_recording(dest, rows, fps=take.fps, offset=0.0, names=list(take.names))
    return len(rows)


def set_offset(root, offset):
    meta = read_meta(root) or dict(version=1)
    meta['offset'] = float(offset)
    folder(root).mkdir(parents=True, exist_ok=True)
    atomic_json(folder(root)/META, meta)
    return meta


def set_audio_edits(root, edits):
    """Remember arranged clips for the current audio file, or the whole file when edits is None.

    Each clip is {t, in, out} in seconds on the wav clock. t is when it plays;
    in/out are the source range. Edits stay with that file, so a voice variation
    does not inherit the recording's cuts.
    """
    meta = read_meta(root)
    if meta is None:
        return None
    audio = str(meta.get('audio') or '').replace('\\', '/')
    book = meta.get('audio_edits_by_file')
    book = dict(book) if isinstance(book, dict) else {}
    if edits is not None and audio:
        book[audio] = list(edits)
    elif audio:
        book.pop(audio, None)
    meta.pop('audio_crop', None)
    meta.pop('audio_edits', None)
    if book:
        meta['audio_edits_by_file'] = book
    else:
        meta.pop('audio_edits_by_file', None)
    atomic_json(folder(root)/META, meta)
    return meta
