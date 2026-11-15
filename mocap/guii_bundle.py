"""A Guii holodrone take: retargeted VRMA, face clip, and the selected voice."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .face_capture import ARKIT_NAMES
from .paths import ROOT


def slug(text):
    cleaned = ''.join(ch.lower() if ch.isalnum() else '-' for ch in str(text).strip())
    return '-'.join(part for part in cleaned.split('-') if part)[:48]


def takes_root():
    holo = ROOT.parent / 'guii' / 'public' / 'holo'
    if (holo / 'agent.vrm').is_file():
        return holo / 'takes'
    return None


def blender_export_command(blend, vrma):
    from .retarget.runtime import blender
    script = ROOT.parent / 'guii' / 'scripts' / 'mocap' / 'export_vrma.py'
    addons = ROOT / 'third_party' / 'character_import'
    if not script.is_file():
        raise RuntimeError('The Guii VRMA exporter was not found next to this project.')
    if not addons.is_dir():
        raise RuntimeError('The character importer add-on is missing.')
    return [str(blender()), '-b', str(Path(blend).resolve()), '--python', str(script.resolve()), '--', str(Path(vrma).resolve()), str(addons.resolve())]


def face_document(take, scene_start, duration, fps):
    """Face weights on the body clock. Holds stay flat; identical in-betweens are dropped."""
    fps = float(fps)
    duration = max(0.0, float(duration))
    count = max(2, int(round(duration * fps)) + 1)
    rows = []
    for index in range(count):
        t = min(duration, index / fps)
        sample = take.sample(float(scene_start) + t)
        rows.append((round(t, 4), [0.0 if abs(float(v)) < 5e-4 else round(float(v), 3) for v in sample]))
    frames = []
    last = len(rows) - 1
    for index, (t, values) in enumerate(rows):
        if index not in (0, last) and values == rows[index - 1][1] and values == rows[index + 1][1]:
            continue
        frames.append({'t': t, 'values': values})
    return {'names': list(ARKIT_NAMES), 'fps': fps, 'frames': frames}


def write_json(path, value, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        value, ensure_ascii=False,
        separators=(',', ':') if compact else (', ', ': '),
        indent=None if compact else 2,
    )
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def encode_take_audio(wav, mp3):
    """48 kHz mono MP3 at LAME VBR quality 2. The wav is removed after a successful encode."""
    import subprocess
    from .video import executable, run_video_command
    wav, mp3 = Path(wav), Path(mp3)
    command = [
        executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(wav),
        '-ac', '1', '-ar', '48000', '-c:a', 'libmp3lame', '-q:a', '2', str(mp3),
    ]
    try:
        run_video_command(command)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip().splitlines()
        raise RuntimeError(detail[-1] if detail else 'The take audio could not be encoded.') from None
    if not mp3.is_file():
        raise RuntimeError('The take audio could not be encoded.')
    wav.unlink(missing_ok=True)


def prepare_take(dest, face_take, scene_start, fps, body_duration, audio_path, offset, audio_crop=None):
    """Write face.json and audio.mp3 for the body clip only. The returned duration is how long
    the holodrone plays. audio_crop is seconds into the source; speech stays on the body clock."""
    from .session_record import aligned_window, cut_audio
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    play = float(body_duration)
    audio_duration = 0.0
    (dest / 'audio.wav').unlink(missing_ok=True)
    (dest / 'audio.mp3').unlink(missing_ok=True)
    if audio_path is not None and Path(audio_path).is_file():
        window = aligned_window(float(scene_start) - float(offset), play, audio_crop)
        if window is not None:
            content_lead, pad, out_len = window
            wav = dest / 'audio.wav'
            audio_duration = cut_audio(audio_path, wav, content_lead, out_len, delay=pad)
            if audio_duration > 0:
                encode_take_audio(wav, dest / 'audio.mp3')
            else:
                wav.unlink(missing_ok=True)
    if play <= 0:
        raise RuntimeError('This take has no length to export.')
    write_json(dest / 'face.json', face_document(face_take, scene_start, play, fps), compact=True)
    return play, audio_duration > 0


def publish(dest, take_id, duration, fps, has_audio, captions=None):
    dest = Path(dest)
    bundle = {
        'version': 1,
        'id': take_id,
        'body': 'body.vrma',
        'face': 'face.json',
        'fps': float(fps),
        'duration': round(float(duration), 3),
    }
    if has_audio:
        bundle['audio'] = 'audio.mp3'
    if captions:
        bundle['captions'] = list(captions)
    write_json(dest / 'bundle.json', bundle)
    entry = {
        'id': take_id,
        'body': '%s/body.vrma' % take_id,
        'face': '%s/face.json' % take_id,
        'duration': round(float(duration), 3),
        'fps': float(fps),
        'v': int(__import__('time').time()),
    }
    if has_audio:
        entry['audio'] = '%s/audio.mp3' % take_id
    if captions:
        entry['captions'] = list(captions)
    index = dest.parent / 'index.json'
    rows = []
    if index.is_file():
        try:
            loaded = json.loads(index.read_text(encoding='utf-8'))
            found = loaded.get('takes') if isinstance(loaded, dict) else None
            if isinstance(found, list):
                rows = [row for row in found if isinstance(row, dict) and row.get('id') != take_id]
        except (OSError, ValueError):
            rows = []
    rows.append(entry)
    from .storage import atomic_json
    atomic_json(index, {'version': 1, 'takes': rows})
