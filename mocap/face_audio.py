"""Play a take's microphone on the selected speakers.

QMediaPlayer handed the wav over as 48 kHz mono. These outputs refuse that:
the interface mix is 44.1 kHz 12-channel float, and the monitor is stereo float.
The sink is fed the device's own format, so the same file is audible on either.
"""
import time
import wave

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices


def _wav_mono(path):
    with wave.open(str(path)) as handle:
        rate = handle.getframerate() or 0
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if not rate or width != 2 or not raw:
        return None, 0, 0.0
    samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    if channels > 1:
        usable = (len(samples) // channels) * channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), int(rate), len(samples) / float(rate)


def _sample_width(fmt):
    kind = fmt.sampleFormat()
    if kind == QAudioFormat.SampleFormat.UInt8:
        return 1
    if kind == QAudioFormat.SampleFormat.Int16:
        return 2
    if kind in (QAudioFormat.SampleFormat.Int32, QAudioFormat.SampleFormat.Float):
        return 4
    return 0


def compose_arrangement(samples, rate, segments):
    """Place source ranges on the wav clock. Returns (samples, duration, clock of sample 0)."""
    if samples is None or rate <= 0 or len(samples) < 2:
        return np.zeros(1, np.float32), 0.0, 0.0
    pieces = [(float(place), float(src_in), float(src_out)) for place, src_in, src_out in segments or []
              if float(src_out) > float(src_in)]
    if not pieces:
        return np.zeros(1, np.float32), 0.0, 0.0
    start = min(place for place, _src_in, _src_out in pieces)
    end = max(place + (src_out - src_in) for place, src_in, src_out in pieces)
    count = max(1, int(round((end - start) * rate)))
    out = np.zeros(count, np.float32)
    total = len(samples)
    for place, src_in, src_out in pieces:
        src_a = int(round(src_in * rate))
        src_b = int(round(src_out * rate))
        dst_a = int(round((place - start) * rate))
        src_a = max(0, min(total, src_a))
        src_b = max(src_a, min(total, src_b))
        if dst_a < 0:
            src_a -= dst_a
            dst_a = 0
        length = min(src_b - src_a, count - dst_a)
        if length > 0:
            out[dst_a:dst_a + length] = samples[src_a:src_a + length]
    return out, count / float(rate), start


def write_float_wav(path, samples, rate):
    import wave
    shaped = np.clip(np.rint(np.asarray(samples, np.float32) * 32767.0), -32768, 32767).astype('<i2')
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'w') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(shaped.tobytes())


def _render(samples, src_rate, origin, written, frames, speed, fmt):
    dst_rate = fmt.sampleRate()
    if frames <= 0 or dst_rate <= 0 or src_rate <= 0 or len(samples) < 2:
        return b''
    start = (origin + written / float(dst_rate) * speed) * src_rate
    step = src_rate * speed / float(dst_rate)
    pos = start + np.arange(frames, dtype=np.float64) * step
    idx = np.floor(pos).astype(np.int64)
    frac = (pos - idx).astype(np.float32)
    out = np.zeros(frames, np.float32)
    valid = (idx >= 0) & (idx + 1 < len(samples))
    if np.any(valid):
        left = idx[valid]
        mix = frac[valid]
        out[valid] = samples[left] * (1.0 - mix) + samples[left + 1] * mix
    kind = fmt.sampleFormat()
    if kind == QAudioFormat.SampleFormat.Float:
        shaped = out.astype('<f4')
    elif kind == QAudioFormat.SampleFormat.Int16:
        shaped = np.clip(np.rint(out * 32767.0), -32768, 32767).astype('<i2')
    elif kind == QAudioFormat.SampleFormat.Int32:
        shaped = np.clip(np.rint(out * 2147483647.0), -2147483648, 2147483647).astype('<i4')
    elif kind == QAudioFormat.SampleFormat.UInt8:
        shaped = np.clip(np.rint(out * 127.0 + 128.0), 0, 255).astype(np.uint8)
    else:
        return b''
    channels = max(1, fmt.channelCount())
    if channels > 1:
        shaped = np.repeat(shaped[:, None], channels, axis=1)
    return np.ascontiguousarray(shaped).tobytes()


class FaceAudio:
    def __init__(self, parent=None):
        self.device = None
        self.device_id = ''
        self.path = None
        self.offset = 0.0
        self.duration = 0.0
        self.samples = None
        self.arranged = None
        self.arranged_duration = 0.0
        self.arranged_start = 0.0
        self._play = None
        self.rate = 0
        self.error = ''
        self.level = 0.0
        self.sink = None
        self.io = None
        self.fmt = None
        self.frame_bytes = 0
        self.origin = 0.0
        self.written = 0
        self.limit = None
        self.speed = 1.0
        self.started = 0.0
        self.active = False
        self._failed_at = 0.0
        self._clock = QTimer(parent)
        self._clock.setInterval(20)
        self._clock.timeout.connect(self._pump)

    def set_device(self, device):
        if device is None or device.isNull():
            return
        self.device_id = bytes(device.id()).decode('latin1')
        self.device = device
        if self.active:
            at = self._heard()
            speed = self.speed
            self._close_sink()
            self._open_sink(at, speed)

    def load(self, path, offset=0.0):
        self.offset = float(offset)
        path = str(path) if path else ''
        if path == self.path and self.samples is not None:
            return
        self.stop()
        self.path = path
        self.samples = None
        self.duration = 0.0
        self.rate = 0
        self.error = ''
        if not path:
            return
        try:
            samples, rate, duration = _wav_mono(path)
        except (wave.Error, OSError, ValueError):
            self.error = 'The microphone recording could not be read.'
            return
        if samples is None:
            self.error = 'The microphone recording is not 16-bit audio.'
            return
        peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        # Interface recordings often peak near -27 dB. Bring a quiet take up
        # for the speakers; the file on disk is left untouched.
        if 0.0005 < peak < 0.35:
            samples = np.ascontiguousarray(samples * min(10.0, 0.35 / peak), dtype=np.float32)
        self.samples = samples
        self.rate = rate
        self.duration = duration
        self.arranged = None
        self.arranged_duration = 0.0
        self.arranged_start = 0.0
        self._play = None

    def set_arrangement(self, samples, duration, start):
        self.stop()
        self.arranged = np.ascontiguousarray(samples, dtype=np.float32)
        self.arranged_duration = float(duration)
        self.arranged_start = float(start)

    def clear_arrangement(self):
        if self.arranged is None:
            return
        self.stop()
        self.arranged = None
        self.arranged_duration = 0.0
        self.arranged_start = 0.0

    def _audible(self):
        if self.arranged is not None:
            return self.arranged, self.arranged_duration, self.arranged_start
        return self.samples, self.duration, 0.0

    def sync(self, scene_time, playing, speed=1.0, start=None, end=None):
        samples, duration, clock = self._audible()
        if not playing or samples is None:
            self.stop()
            return
        self._play = samples
        t = float(scene_time) - self.offset - clock
        begin = 0.0 if start is None else max(0.0, float(start))
        limit = duration
        if end is not None and self.arranged is None:
            limit = min(limit, float(end)) if limit else max(0.0, float(end))
            begin = max(begin, 0.0)
        self.limit = limit
        if t < begin - 0.03 or (limit and t >= limit - 1e-3):
            self.stop()
            return
        speed = max(0.25, min(4.0, float(speed or 1.0)))
        if self.active and abs(speed - self.speed) <= 0.01 and abs(self._heard() - t) <= 0.35:
            if limit and self._heard() >= limit - 1e-3:
                self.stop()
                return
            self._pump()
            return
        if not self.active and self.error and time.perf_counter() - self._failed_at < 1.0:
            return
        self._close_sink()
        self._play = samples
        self._open_sink(max(begin, t), speed)
        if not self.active:
            self._failed_at = time.perf_counter()

    def stop(self):
        self._clock.stop()
        self._close_sink()
        self._play = None
        self.level = 0.0

    def _heard(self):
        if not self.active:
            return self.origin
        return self.origin + (time.perf_counter() - self.started) * self.speed

    def _open_sink(self, source_time, speed):
        device = self.device
        if device is None or device.isNull():
            device = QMediaDevices.defaultAudioOutput()
        fmt = device.preferredFormat() if device is not None and not device.isNull() else QAudioFormat()
        width = _sample_width(fmt)
        channels = fmt.channelCount()
        rate = fmt.sampleRate()
        if width <= 0 or channels <= 0 or rate <= 0:
            self.error = 'The selected speakers have no supported format.'
            self.active = False
            return
        self.fmt = fmt
        self.frame_bytes = width * channels
        self.speed = speed
        self.origin = float(source_time)
        self.written = 0
        self.sink = QAudioSink(device, fmt)
        self.sink.setVolume(1.0)
        self.sink.setBufferSize(self.frame_bytes * max(1, rate // 10))
        self.io = self.sink.start()
        self.started = time.perf_counter()
        self.active = self.io is not None
        if not self.active:
            self.error = 'Playback did not start on the selected speakers.'
            self._close_sink()
            return
        self.error = ''
        self._clock.start()
        self._pump()

    def _close_sink(self):
        self.active = False
        sink, self.sink = self.sink, None
        self.io = None
        if sink is not None:
            sink.stop()
            sink.deleteLater()

    def _pump(self):
        if not self.active or self.io is None or self.sink is None or self.samples is None or self.fmt is None:
            return
        err = self.sink.error()
        if err not in (QAudio.Error.NoError, QAudio.Error.UnderrunError):
            self.error = 'Playback stopped (%s).' % err
            self.stop()
            return
        frames = self.sink.bytesFree() // self.frame_bytes
        if frames <= 0:
            return
        if self.limit:
            remain = self.limit - (self.origin + self.written / float(self.fmt.sampleRate()) * self.speed)
            if remain <= 0.0:
                self.stop()
                return
            frames = min(frames, max(1, int(remain * self.fmt.sampleRate() / max(self.speed, 0.01))))
        heard = int((time.perf_counter() - self.started) * self.fmt.sampleRate())
        ahead = self.written - heard
        limit = max(1, self.fmt.sampleRate() // 8)
        if ahead >= limit:
            return
        frames = min(frames, limit - max(0, ahead))
        samples = self._play if self._play is not None else self.samples
        payload = _render(samples, self.rate, self.origin, self.written, frames, self.speed, self.fmt)
        if not payload:
            return
        wrote = self.io.write(payload)
        if wrote <= 0:
            return
        used = wrote // self.frame_bytes
        self.written += used
        start = (self.origin + (self.written - used) / float(self.fmt.sampleRate()) * self.speed) * self.rate
        end = start + used * self.rate * self.speed / float(self.fmt.sampleRate())
        left = max(0, int(start))
        right = min(len(samples), int(end) + 1)
        self.level = float(np.max(np.abs(samples[left:right]))) if right > left else 0.0
