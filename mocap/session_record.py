"""One-clock capture of a camera, microphone, and Quest face track."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import threading
import time
import wave
import numpy as np
from .paths import HOME
from .storage import atomic_json
from .video import executable, probe, run_video_command

LAST_OPEN = HOME / '.setup' / 'last_open.json'


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
    # One argv element already keeps the spaces. Extra quotes become part of the
    # device name, and DirectShow then opens nothing.
    return str(name).replace('"', '')


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


def audio_pipe_command(audio_device):
    return [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error',
            '-f', 'dshow', '-audio_buffer_size', '20',
            '-i', 'audio=' + _dshow_name(audio_device),
            '-ac', '1', '-ar', '48000', '-flush_packets', '1', '-f', 's16le', 'pipe:1']


def headset_microphone(names):
    for name in names or []:
        key = name.casefold()
        if any(token in key for token in ('quest', 'oculus', 'virtual desktop')):
            return name
    return None


class PcmTrack:
    """Mono PCM on the take clock. Times are seconds since the take epoch."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rate = None
        self.origin = None
        self._buf = bytearray()

    def add(self, pts, rate, pcm):
        if pcm is None:
            return
        try:
            pts = float(pts)
            rate = int(rate)
        except (TypeError, ValueError):
            return
        pcm = bytes(pcm)
        pcm = pcm[:len(pcm) - (len(pcm) % 2)]
        # Wall-clock stamps are ~1e9. One of those sets the origin, and every
        # later take-relative packet is then dropped as "too early".
        if pts != pts or pts < -1.0 or pts > 24 * 3600 or len(pcm) < 2 or rate < 8000 or rate > 96000:
            return
        with self.lock:
            if self.rate is None:
                self.rate = rate
                self.origin = float(pts)
            if rate != self.rate:
                return
            frames = len(pcm) // 2
            expected = self.origin + (len(self._buf) // 2) / self.rate
            delta = float(pts) - expected
            # The phone stamps each block with Date.now(). That clock jumps by
            # tens of milliseconds while the samples are already contiguous, and
            # padding or cutting those jumps puts holes through the voice.
            if delta > 0.5:
                self._buf += b'\x00\x00' * int(delta * self.rate)
            elif delta < -0.5:
                skip = min(frames, int(-delta * self.rate))
                pcm = pcm[skip * 2:]
            if pcm:
                self._buf += pcm

    def coverage(self):
        with self.lock:
            if self.origin is None or len(self._buf) < 2 or not self.rate:
                return None
            return float(self.origin), (len(self._buf) // 2) / float(self.rate)

    def snapshot(self):
        with self.lock:
            if self.origin is None or len(self._buf) < 2 or not self.rate:
                return None
            return bytes(self._buf), int(self.rate), float(self.origin)


def write_aligned_wav(path, pcm, rate, origin, lead, output_rate=48000):
    """Write a wav whose first sample is the video's t = 0."""
    samples = np.frombuffer(pcm, np.int16).astype(np.float32)
    if len(samples) < 2:
        return None
    if int(rate) != int(output_rate):
        count = max(2, int(round(len(samples) * float(output_rate) / float(rate))))
        positions = np.linspace(0, len(samples) - 1, count)
        left = np.floor(positions).astype(np.int32)
        right = np.minimum(left + 1, len(samples) - 1)
        mix = positions - left
        samples = samples[left] * (1 - mix) + samples[right] * mix
        rate = int(output_rate)
    else:
        rate = int(rate)
    try:
        start = int(round((float(lead) - float(origin)) * rate))
    except (TypeError, ValueError, OverflowError):
        start = 0
    # A few seconds of lead is the mic opening before the first frame.
    # Anything past that is a broken clock, not silence to allocate.
    if start < -(rate * 30) or start >= len(samples) + rate * 30:
        start = 0
    elif start >= len(samples):
        return None
    if start > 0:
        samples = samples[start:]
    elif start < 0:
        samples = np.concatenate((np.zeros(-start, np.float32), samples))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = np.clip(np.rint(samples), -32768, 32767).astype(np.int16)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.tobytes())
    return path if path.is_file() and path.stat().st_size > 44 else None


class DeviceMic(threading.Thread):
    """Windows microphone, timestamped on the take clock."""

    def __init__(self, device, track, epoch, on_pcm=None):
        super().__init__(daemon=True, name='session-mic')
        self.device = device
        self.track = track
        self.epoch = float(epoch)
        self.on_pcm = on_pcm
        self.error = None
        self._halt = threading.Event()
        self._proc = None

    def stop(self):
        self._halt.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        self.join(3)
        if proc is not None and proc.poll() is None:
            proc.kill()
            self.join(1)

    def run(self):
        try:
            proc = subprocess.Popen(audio_pipe_command(self.device), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, creationflags=0x08000000)
        except OSError as exc:
            self.error = str(exc)
            return
        self._proc = proc
        started = False
        try:
            while not self._halt.is_set():
                chunk = proc.stdout.read(4800 * 2)
                if not chunk:
                    break
                if not started:
                    started = True
                    pts = max(0.0, time.time() - self.epoch - (len(chunk) // 2) / 48000.0)
                else:
                    covered = self.track.coverage()
                    pts = (covered[0] + covered[1]) if covered else 0.0
                if self.on_pcm is not None:
                    self.on_pcm(chunk)
                self.track.add(pts, 48000, chunk)
        finally:
            if proc.poll() is None:
                proc.terminate()
            if not started and not self.error:
                self.error = 'The microphone did not start.'


class LevelTap(threading.Thread):
    """Read a Windows microphone only to feed the timeline level meter."""

    def __init__(self, device, on_pcm):
        super().__init__(daemon=True, name='mic-level')
        self.device = device
        self.on_pcm = on_pcm
        self.error = None
        self._halt = threading.Event()
        self._proc = None

    def stop(self):
        self._halt.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        self.join(2)
        if proc is not None and proc.poll() is None:
            proc.kill()
            self.join(1)

    def run(self):
        try:
            proc = subprocess.Popen(audio_pipe_command(self.device), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, creationflags=0x08000000)
        except OSError as exc:
            self.error = str(exc)
            return
        self._proc = proc
        try:
            while not self._halt.is_set():
                chunk = proc.stdout.read(1024 * 2)
                if not chunk:
                    err = b''
                    if proc.stderr is not None:
                        err = proc.stderr.read() or b''
                    text = err.decode('utf-8', 'replace').strip()
                    self.error = text[-180:] or 'The microphone did not start.'
                    break
                self.on_pcm(chunk)
        finally:
            if proc.poll() is None:
                proc.terminate()


def write_last_open(video, project, microphone, speaker=''):
    atomic_json(LAST_OPEN, {
        'version': 1,
        'video': str(video) if video else None,
        'project': str(project) if project else None,
        'microphone': microphone or '',
        'speaker': speaker or '',
    })


def read_last_open():
    if not LAST_OPEN.is_file():
        return None
    try:
        data = json.loads(LAST_OPEN.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('version') == 1 else None


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


def pcm_wav(path):
    try:
        with wave.open(str(path)) as handle:
            return handle.getsampwidth() == 2 and handle.getnchannels() >= 1 and handle.getframerate() > 0
    except (wave.Error, OSError):
        return False


def write_variation_wav(payload, suffix, output_format, destination):
    """Store an ElevenLabs result as 16-bit wav so timeline playback can read it."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.src')
    temporary.write_bytes(payload)
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y']
    fmt = str(output_format or '')
    if fmt.startswith('pcm_'):
        rate = ''.join(ch for ch in fmt.split('_', 1)[-1] if ch.isdigit()) or '44100'
        command += ['-f', 's16le', '-ar', rate, '-ac', '1']
    elif suffix == '.wav' and not pcm_wav(temporary):
        command += ['-f', 's16le', '-ar', '44100', '-ac', '1']
    command += ['-i', str(temporary), '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(destination)]
    try:
        run_video_command(command)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(detail.splitlines()[-1] if detail else 'The voice variation could not be prepared for playback.') from None
    finally:
        temporary.unlink(missing_ok=True)
    if not pcm_wav(destination):
        raise RuntimeError('The voice variation could not be prepared for playback.')
    return destination


def prepare_speech(source, destination, normalize=False, compress=False):
    """Level a copy for speech-to-speech. The source file is left unchanged."""
    source, destination = Path(source), Path(destination)
    if not normalize and not compress:
        raise ValueError('Choose normalize, compress, or both.')
    filters = []
    if compress:
        # Hold louder parts down. Makeup lifts quieter words only when loudness is not normalized afterward.
        makeup = 1 if normalize else 2
        filters.append(f'acompressor=threshold=0.05:ratio=6:attack=5:release=80:makeup={makeup}:knee=2.5')
    if normalize:
        filters.append('loudnorm=I=-16:TP=-1.5:LRA=11')
    else:
        filters.append('alimiter=limit=0.89:level=false')
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
               '-ac', '1', '-ar', '48000', '-af', ','.join(filters), '-c:a', 'pcm_s16le', str(destination)]
    try:
        run_video_command(command)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(detail.splitlines()[-1] if detail else 'The recording could not be prepared.') from None
    if not pcm_wav(destination) or destination.stat().st_size <= 44:
        raise RuntimeError('The recording could not be prepared.')
    return destination


def ensure_wav(source, destination=None):
    source = Path(source)
    if pcm_wav(source) and destination is None:
        return source
    destination = Path(destination) if destination else source.with_suffix('.wav')
    if destination.resolve() == source.resolve():
        destination = source.with_name(source.stem + '-pcm.wav')
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
               '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(destination)]
    try:
        run_video_command(command)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(detail.splitlines()[-1] if detail else 'The voice variation could not be prepared for playback.') from None
    if not pcm_wav(destination):
        raise RuntimeError('The voice variation could not be prepared for playback.')
    return destination


def wav_info(path):
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        if handle.getsampwidth() != 2 or handle.getnchannels() < 1 or rate <= 0:
            raise wave.Error('not 16-bit pcm')
        return handle.getnframes() / float(rate), handle.getnchannels(), rate


def aligned_window(file_lead, body_duration, crop):
    """Audible piece of a wav on a body clock.

    `file_lead` is seconds into the wav at body time 0. `crop` is that wav's
    audible [start, end] in seconds, or None for the whole body window.
    Returns (content_lead, pad, output_duration). `pad` is leading silence so
    the speech stays on the body clock. None when the window has no audio.
    """
    play = max(0.0, float(body_duration))
    lead = float(file_lead)
    if crop is None:
        return (lead, 0.0, play) if play > 0 else None
    start, end = float(crop[0]), float(crop[1])
    content_lead = max(lead, start)
    content_end = min(lead + play, end)
    if content_end - content_lead <= 1e-3:
        return None
    pad = content_lead - lead
    return content_lead, pad, pad + (content_end - content_lead)


def cut_audio(source, dest, lead, duration=None, delay=0.0):
    """Mono 48 kHz wav whose sample 0 is `lead` seconds into the source, at most `duration`
    long. A negative lead, or `delay`, starts with silence. Returns the written length in seconds."""
    source, dest = Path(source), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lead = float(lead)
    delay = max(0.0, float(delay or 0.0))
    try:
        length, channels, rate = wav_info(source)
    except (wave.Error, OSError, EOFError):
        length, channels, rate = None, None, None
    whole = duration is None or (length is not None and length <= float(duration) + 0.02)
    if length is not None and channels == 1 and rate == 48000 and abs(lead) < 0.02 and delay < 0.02 and whole:
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        return length
    filters = []
    if lead > 0.02:
        filters.append('atrim=start=%.4f,asetpts=PTS-STARTPTS' % lead)
    elif lead < -0.02:
        filters.append('adelay=%d:all=1' % int(round(-lead * 1000)))
    if delay > 0.02:
        filters.append('adelay=%d:all=1' % int(round(delay * 1000)))
    command = [executable('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source)]
    if filters:
        command += ['-af', ','.join(filters)]
    if duration is not None:
        command += ['-t', '%.4f' % max(0.0, float(duration))]
    command += ['-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(dest)]
    try:
        run_video_command(command)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b'').decode('utf-8', errors='replace').strip().splitlines()
        raise RuntimeError(detail[-1] if detail else 'The audio could not be trimmed.') from None
    if not dest.is_file():
        raise RuntimeError('The audio could not be trimmed.')
    return wav_info(dest)[0]


def clip_take(root, dest, start, end):
    """Copy a take's face, every audio take, captions, and eye track into dest for the
    video range [start, end). Times are rewritten so `start` becomes 0."""
    from . import captions
    from .face_capture import audio_library, clip_recording, folder, read_meta
    meta = read_meta(root)
    if not meta:
        return False
    offset = float(meta.get('offset') or 0)
    start, end = float(start), float(end)
    lead, length = start - offset, end - start
    try:
        clip_recording(root, dest, start, end)
    except ValueError:
        pass
    source, target = folder(root), folder(dest)
    target.mkdir(parents=True, exist_ok=True)
    takes = audio_library(root, meta)
    kept = []
    for take in takes:
        if cut_audio(source / take['file'], target / take['file'], lead, length) > 0.01:
            kept.append(take)
    out = read_meta(dest) or dict(version=1, face=None)
    out['offset'] = 0.0
    files = {take['file'] for take in kept}
    for key in ('audio', 'original_audio'):
        value = str(meta.get(key) or '').replace('\\', '/')
        if value in files:
            out[key] = value
    if kept:
        out['audio_takes'] = kept
        out.setdefault('audio', kept[0]['file'])
    atomic_json(target / 'meta.json', out)
    timed = {}
    for file, entry in captions.load(root).items():
        if file in files and isinstance(entry, dict):
            timed[file] = captions.shift_entry(entry, lead, length)
    if timed:
        atomic_json(target / captions.FILE, dict(version=1, takes=timed))
    try:
        eyes = json.loads((source / 'eyes.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        eyes = None
    if isinstance(eyes, dict):
        rows = [dict(row, t=round(float(row['t']) + offset - start, 4)) for row in eyes.get('frames') or []
                if isinstance(row, dict) and start - 1e-4 <= float(row.get('t', -1)) + offset < end - 1e-4]
        if rows:
            atomic_json(target / 'eyes.json', dict(version=1, frames=rows))
    return True


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
