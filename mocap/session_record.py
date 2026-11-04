"""One-clock capture of a camera, microphone, and Quest face track."""
from pathlib import Path
import json
import re
import shutil
import subprocess
from .storage import atomic_json
from .video import executable, probe, run_video_command


def list_devices():
    command = [executable('ffmpeg'), '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy']
    proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=0x08000000)
    video, audio = [], []
    for line in (proc.stderr or '').splitlines():
        match = re.search(r'"([^"]+)" \((video|audio)\)', line)
        if not match:
            continue
        (video if match.group(2) == 'video' else audio).append(match.group(1))
    if not video and not audio and proc.returncode not in (0, 1):
        raise RuntimeError((proc.stderr or 'Cannot list cameras').strip()[-400:])
    return video, audio


def _dshow_name(name):
    return '"' + str(name).replace('"', '') + '"'


def record_command(video_device, audio_device, destination):
    spec = 'video=' + _dshow_name(video_device)
    if audio_device:
        spec += ':audio=' + _dshow_name(audio_device)
    return [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'dshow', '-framerate', '30', '-i', spec,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '160k', str(destination)]


def audio_command(audio_device, destination):
    return [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'dshow', '-i', 'audio=' + _dshow_name(audio_device),
            '-c:a', 'pcm_s16le', str(destination)]


def remux_transport(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y',
               '-i', str(source), '-c', 'copy', '-movflags', '+faststart', str(destination)]
    run_video_command(command)
    return destination


def align_take(frames, lead):
    """Shift face times so the first phone frame is t = 0."""
    if not frames:
        return []
    origin = float(lead or 0)
    aligned = []
    for row in frames:
        t = float(row[0]) - origin
        if t < -0.05:
            continue
        aligned.append([round(max(0.0, t), 4), *row[1:]])
    return aligned


def extract_audio(video, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(video),
               '-vn', '-ac', '1', '-ar', '48000', str(destination)]
    run_video_command(command)
    return destination


def cut_media(source, destination, start, duration):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
               '-ss', f'{start:.3f}', '-t', f'{duration:.3f}',
               '-map', '0:v:0', '-map', '0:a?',
               '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '160k', str(destination)]
    run_video_command(command)
    if probe(destination)['frames'] < 4:
        raise ValueError('Select a range containing at least four frames.')
    return destination


def write_session(folder, video_name='take.mp4'):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(folder / 'session.json', {'version': 1, 'video': video_name})
    return folder


def session_for(path):
    path = Path(path).resolve()
    parents = [path.parent, *list(path.parents)[:4]]
    for parent in parents:
        marker = parent / 'session.json'
        if marker.is_file():
            try:
                data = json.loads(marker.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get('version') == 1:
                return parent
    return None


def replace_tree(source, dest):
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
