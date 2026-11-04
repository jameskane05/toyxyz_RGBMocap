"""Face-mocap JSON (v1-v5) plus optional sibling audio, stored in the project."""
from pathlib import Path
import json
import shutil
from bisect import bisect_right
import numpy as np
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
    if version not in range(1, 6):
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
    if len(names) < 1 or len(names) > 128:
        raise ValueError('Face capture has an invalid blendshape list.')
    times = np.empty(len(frames), np.float64)
    values = np.zeros((len(frames), len(ARKIT_NAMES)), np.float32)
    index = [canonicalize(name) for name in names]
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
        for j, dest in enumerate(index):
            if dest is None or j >= len(src):
                continue
            values[i, dest] = float(src[j])
    order = np.argsort(times, kind='stable')
    return dict(fps=float(record.get('fps') or 30), names=list(ARKIT_NAMES),
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

    def sample(self, scene_time):
        t = float(scene_time) - self.offset
        if not len(self.times):
            return np.zeros(len(ARKIT_NAMES), np.float32)
        if t <= self.times[0]:
            return self.values[0].copy()
        if t >= self.times[-1]:
            return self.values[-1].copy()
        i = bisect_right(self.times, t) - 1
        t0, t1 = self.times[i], self.times[i+1]
        a = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return self.values[i]*(1-a) + self.values[i+1]*a


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
    atomic_json(dest/META, meta)
    return dest/name


def save_recording(root, frames, fps=30.0, offset=0.0):
    if not frames:
        raise ValueError('No face frames were received.')
    dest = folder(root)
    dest.mkdir(parents=True, exist_ok=True)
    record = dict(version=4, fps=float(fps), names=list(ARKIT_NAMES), frames=frames)
    (dest/FACE_FILE).write_text(json.dumps(record, separators=(',', ':')), encoding='utf-8')
    meta = read_meta(root) or dict(version=1)
    meta.update(face=FACE_FILE, offset=float(offset), frames=len(frames),
                duration=float(frames[-1][0]), fps=float(fps))
    atomic_json(dest/META, meta)
    return dest/FACE_FILE


def load_take(root):
    meta = read_meta(root)
    path = face_path(root, meta)
    if path is None:
        return None, meta
    return FaceTake(load_file(path), float((meta or {}).get('offset', 0))), meta


def clip_recording(root, dest, start, end):
    """Copy the face track into dest, shifting times so start becomes 0."""
    take, _meta = load_take(root)
    if take is None:
        return 0
    rows = []
    start, end = float(start), float(end)
    for t, values in zip(take.times, take.values):
        if start - 1e-4 <= float(t) < end - 1e-4:
            rows.append([round(float(t) - start, 4), *[round(float(v), 4) for v in values]])
    if not rows:
        raise ValueError('The selected range has no face frames.')
    save_recording(dest, rows, fps=take.fps, offset=0.0)
    return len(rows)


def set_offset(root, offset):
    meta = read_meta(root) or dict(version=1)
    meta['offset'] = float(offset)
    folder(root).mkdir(parents=True, exist_ok=True)
    atomic_json(folder(root)/META, meta)
    return meta
