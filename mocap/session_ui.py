"""Record a camera, Quest face, and microphone together, then crop or change the voice."""
from datetime import datetime
from pathlib import Path
import json
import shutil
import subprocess
import time
from PySide6.QtCore import Qt, QProcess, QThread, Signal, QTimer
from PySide6.QtWidgets import (QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QDoubleSpinBox, QCheckBox, QMessageBox, QInputDialog, QScrollArea, QFrame)
from .paths import HOME, PROJECTS
from .storage import atomic_json
from .face_capture import clip_recording, folder, import_audio, read_meta, save_recording
from .session_record import (align_take, audio_command, cut_media, extract_audio, list_devices,
                             record_command, remux_transport, replace_tree, session_for, write_session)
from .elevenlabs import convert, fetch_models, fetch_voices

KEY_PATH = HOME / '.setup' / 'elevenlabs.json'


def _load_key():
    try:
        return json.loads(KEY_PATH.read_text(encoding='utf-8')).get('key', '')
    except (OSError, ValueError):
        return ''


def _save_key(value):
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(KEY_PATH, {'key': value})


class _Devices(QThread):
    ready = Signal(object, object)
    failed = Signal(str)

    def run(self):
        try:
            video, audio = list_devices()
            self.ready.emit(video, audio)
        except Exception as exc:
            self.failed.emit(str(exc))


class _Convert(QThread):
    ready = Signal(str, bytes, str)
    failed = Signal(str)

    def __init__(self, key, voice_id, audio, options, voice_name):
        super().__init__()
        self.key, self.voice_id, self.audio, self.options, self.voice_name = key, voice_id, audio, options, voice_name

    def run(self):
        try:
            payload, suffix = convert(self.key, self.voice_id, self.audio, self.options)
            self.ready.emit(self.voice_name, payload, suffix)
        except Exception as exc:
            self.failed.emit(str(exc))


class SessionWorkspace:
    def init_session(self):
        self.session_dir = None
        self._session_process = None
        self._session_video = None
        self._session_started = None
        self._device_thread = None
        self._voice_thread = None
        dock = QDockWidget('Session', self)
        dock.setObjectName('sessionDock')
        panel = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(QLabel('The Quest app, the iPhone app, and the microphone share one take. Reconstruct 3D runs afterward.'))
        self.session_camera = QComboBox()
        self.session_mic = QComboBox()
        layout.addWidget(QLabel('iPhone / camera'))
        layout.addWidget(self.session_camera)
        layout.addWidget(QLabel('Microphone'))
        layout.addWidget(self.session_mic)
        row = QHBoxLayout()
        self.session_refresh = QPushButton('Refresh devices')
        self.session_record = QPushButton('Record')
        self.session_record.setObjectName('primary')
        row.addWidget(self.session_refresh)
        row.addWidget(self.session_record)
        layout.addLayout(row)
        self.session_status = QLabel('Open the Quest and iPhone apps at the viewport address, then Record.')
        self.session_status.setWordWrap(True)
        layout.addWidget(self.session_status)
        crop_row = QHBoxLayout()
        self.session_crop = QPushButton('Crop session')
        self.session_save_clip = QPushButton('Save clip')
        crop_row.addWidget(self.session_crop)
        crop_row.addWidget(self.session_save_clip)
        layout.addLayout(crop_row)
        self.session_clips = QComboBox()
        self.session_clips.addItem('Open a saved clip…', None)
        layout.addWidget(self.session_clips)
        layout.addWidget(QLabel('ElevenLabs'))
        self.session_key = QLineEdit()
        self.session_key.setEchoMode(QLineEdit.Password)
        self.session_key.setPlaceholderText('API key')
        self.session_key.setText(_load_key())
        layout.addWidget(self.session_key)
        self.session_voices = QComboBox()
        self.session_models = QComboBox()
        layout.addWidget(self.session_voices)
        layout.addWidget(self.session_models)
        self.session_stability = QDoubleSpinBox()
        self.session_stability.setRange(0, 1)
        self.session_stability.setSingleStep(0.05)
        self.session_stability.setValue(0.5)
        self.session_similarity = QDoubleSpinBox()
        self.session_similarity.setRange(0, 1)
        self.session_similarity.setSingleStep(0.05)
        self.session_similarity.setValue(0.75)
        layout.addWidget(QLabel('Stability'))
        layout.addWidget(self.session_stability)
        layout.addWidget(QLabel('Similarity'))
        layout.addWidget(self.session_similarity)
        self.session_noise = QCheckBox('Remove background noise')
        self.session_noise.setChecked(True)
        layout.addWidget(self.session_noise)
        self.session_format = QComboBox()
        self.session_format.addItem('Default', '')
        self.session_format.addItem('MP3 44.1 kHz 128k', 'mp3_44100_128')
        self.session_format.addItem('PCM 44.1 kHz', 'pcm_44100')
        layout.addWidget(self.session_format)
        voice_row = QHBoxLayout()
        self.session_fetch = QPushButton('Fetch voices')
        self.session_generate = QPushButton('Generate voice')
        voice_row.addWidget(self.session_fetch)
        voice_row.addWidget(self.session_generate)
        layout.addLayout(voice_row)
        self.session_use_original = QPushButton('Use original audio')
        layout.addWidget(self.session_use_original)
        self.session_voice_status = QLabel('')
        self.session_voice_status.setWordWrap(True)
        layout.addWidget(self.session_voice_status)
        layout.addStretch()
        scroll.setWidget(body)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        dock.setWidget(panel)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        self.session_dock = dock
        self.session_refresh.clicked.connect(self.refresh_session_devices)
        self.session_record.clicked.connect(self.toggle_session_record)
        self.session_crop.clicked.connect(self.crop_session)
        self.session_save_clip.clicked.connect(self.save_session_clip)
        self.session_clips.activated.connect(self.open_session_clip)
        self.session_fetch.clicked.connect(self.fetch_session_voices)
        self.session_generate.clicked.connect(self.generate_session_voice)
        self.session_use_original.clicked.connect(self.use_original_session_audio)
        self.session_key.editingFinished.connect(lambda: _save_key(self.session_key.text().strip()))
        self.session_clock = QTimer(self)
        self.session_clock.setInterval(500)
        self.session_clock.timeout.connect(self._session_tick)
        self._phone_path = None
        self._audio_process = None
        self._session_from_device = False
        self._session_stopping = False
        self.session_follow = QTimer(self)
        self.session_follow.setInterval(200)
        self.session_follow.timeout.connect(self._follow_device_record)
        self.session_follow.start()
        self.session_camera.setToolTip('iPhone app streams full-resolution video. A webcam is the fallback.')
        self.session_crop.setToolTip('Replace this session with the timeline selection. Video, face, and audio stay aligned.')
        self.session_save_clip.setToolTip('Keep the full session and write the timeline selection as a separate clip.')
        self.refresh_session_devices()

    def refresh_session_devices(self):
        if self._device_thread is not None and self._device_thread.isRunning():
            return
        self.session_status.setText('Looking for cameras and microphones…')
        self._device_thread = _Devices(self)
        self._device_thread.ready.connect(self._devices_ready)
        self._device_thread.failed.connect(lambda msg: self.session_status.setText(msg))
        self._device_thread.start()

    def _devices_ready(self, video, audio):
        current = self.session_camera.currentData()
        self.session_camera.clear()
        self.session_camera.addItem('iPhone app', 'phone')
        for name in video or []:
            self.session_camera.addItem(name, name)
        index = self.session_camera.findData(current) if current else 0
        self.session_camera.setCurrentIndex(index if index >= 0 else 0)
        self.session_mic.clear()
        self.session_mic.addItem('No microphone', '')
        for name in audio:
            self.session_mic.addItem(name, name)
        self.session_status.setText('Ready. Record starts the Quest, the iPhone, and the microphone together.')

    def _fill(self, box, names):
        current = box.currentText()
        box.clear()
        for name in names:
            box.addItem(name, name)
        index = box.findText(current)
        if index >= 0:
            box.setCurrentIndex(index)

    def _session_active(self):
        return (getattr(self, '_session_process', None) is not None
                or getattr(self, '_phone_path', None) is not None
                or getattr(self, '_audio_process', None) is not None)

    def toggle_session_record(self):
        if self._session_active():
            if self.face_server is not None:
                self.face_server.hub.stop()
            self._stop_session_record()
            return
        self._begin_session(reset_face=True)

    def _follow_device_record(self):
        if self._session_stopping or self.face_server is None:
            return
        recording = bool(self.face_server.hub.status().get('recording'))
        active = self._session_active()
        if recording and not active:
            self._begin_session(reset_face=False)
        elif not recording and active:
            self._stop_session_record()

    def _begin_session(self, reset_face):
        if self._session_active() or getattr(self, 'busy', False):
            return
        source = self.session_camera.currentData()
        phone = source == 'phone'
        if not phone and (not source or source == 'No camera found'):
            self.session_status.setText('Choose the iPhone app or a camera first.')
            if self.face_server is not None and self.face_server.hub.recording:
                self.face_server.hub.stop()
            return
        name = datetime.now().strftime('session-%Y%m%d-%H%M%S')
        folder = write_session(PROJECTS / 'sessions' / name)
        if self.face_server is not None and reset_face:
            self.face_server.hub.begin_take()
        mic = self.session_mic.currentData() or ''
        self.session_dir = folder
        self._session_video = folder / 'take.mp4'
        if phone:
            self._phone_path = folder / 'take.ts'
            if self.face_server is not None and self.face_server.phone is not None:
                self.face_server.phone.open_take(self._phone_path)
            if mic:
                self._audio_process = self._spawn(audio_command(mic, folder / 'mic.wav'))
        else:
            process = self._spawn(record_command(source, mic, self._session_video))
            if process is None or not process.waitForStarted(3000):
                self.session_status.setText('Could not start the camera recorder.')
                if self.face_server is not None:
                    self.face_server.hub.stop()
                return
            self._session_process = process
        self._session_started = time.monotonic()
        self.session_record.setText('Stop')
        self.session_clock.start()
        self.session_status.setText('Recording. The Quest and iPhone follow this take.')

    def _spawn(self, command):
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.start()
        return process

    def _session_tick(self):
        if self._session_started is None:
            return
        elapsed = time.monotonic() - self._session_started
        self.session_status.setText('Recording  %d:%02d  ·  Quest URL is in the viewport header.' % (int(elapsed) // 60, int(elapsed) % 60))

    def _stop_session_record(self):
        if self._session_stopping:
            return
        self._session_stopping = True
        try:
            self._finish_session()
        finally:
            self._session_stopping = False

    def _finish_process(self, process):
        if process is None:
            return
        process.write(b'q\n')
        if not process.waitForFinished(8000):
            process.kill()
            process.waitForFinished(2000)

    def _finish_session(self):
        process, audio, video = self._session_process, self._audio_process, self._session_video
        phone_path = self._phone_path
        self._session_process = None
        self._audio_process = None
        self._phone_path = None
        self.session_clock.stop()
        self.session_record.setText('Record')
        self._finish_process(process)
        self._finish_process(audio)
        lead = 0.0
        phone_frames = 0
        if phone_path is not None and self.face_server is not None and self.face_server.phone is not None:
            phone_frames, first = self.face_server.phone.close_take()
            lead = float(first or 0.0)
            if phone_frames and phone_path.is_file() and phone_path.stat().st_size > 0:
                try:
                    remux_transport(phone_path, video)
                except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                    self.session_status.setText('The iPhone stream did not remux: ' + str(exc))
                    video = None
        frames = []
        eyes = []
        if self.face_server is not None:
            frames = self.face_server.hub.take_frames()
            eyes = self.face_server.hub.take_eyes()
            frames.extend(self.face_server.hub.stop())
        frames = align_take(frames, lead)
        folder = self.session_dir
        if video is None or not video.is_file() or video.stat().st_size < 1024:
            if frames and folder is not None:
                try:
                    save_recording(folder, frames)
                    self._save_eyes(folder, eyes, lead)
                except (OSError, ValueError):
                    pass
            self.session_status.setText('Recording produced no video. The iPhone app sends full-resolution frames while Record is on.')
            return
        try:
            if frames:
                save_recording(folder, frames)
            self._save_eyes(folder, eyes, lead)
            mic = folder / 'mic.wav'
            if any(stream.get('codec_type') == 'audio' for stream in self._streams(video)):
                wav = extract_audio(video, folder / 'face_capture' / 'audio.wav')
                import_audio(folder, wav)
            elif mic.is_file() and mic.stat().st_size > 1024:
                import_audio(folder, mic)
            else:
                self.session_status.setText('Video saved. No microphone track was captured.')
        except (OSError, ValueError, RuntimeError) as exc:
            self.session_status.setText('Saved the video, but face or audio failed: ' + str(exc))
        self.load_video(video)
        self.reload_face(force=True)
        self._fill_clips()
        extra = f' {len(frames)} face frames.' if frames else ' No Quest frames arrived.'
        if phone_frames:
            extra += f' {phone_frames} video frames.'
        self.session_status.setText('Session saved.' + extra + ' Trim it, or Reconstruct 3D for the body.')
        self.status.setText('Live session saved. Body tracking runs when you Reconstruct 3D.')

    def _save_eyes(self, folder, eyes, lead):
        kept = []
        for row in eyes or []:
            try:
                t = float(row.get('t', 0)) - float(lead or 0)
            except (TypeError, ValueError):
                continue
            if t < -0.05:
                continue
            kept.append(dict(t=round(max(0.0, t), 4), eyes=row.get('eyes')))
        if not kept:
            return
        dest = folder / 'face_capture'
        dest.mkdir(parents=True, exist_ok=True)
        atomic_json(dest / 'eyes.json', dict(version=1, frames=kept))

    def _streams(self, video):
        from .video import executable
        proc = subprocess.run([executable('ffprobe'), '-v', 'error', '-show_streams', '-of', 'json', str(video)],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=0x08000000)
        try:
            return json.loads(proc.stdout).get('streams') or []
        except ValueError:
            return []

    def _range(self):
        found = session_for(self.video_path) if getattr(self, 'video_path', None) else None
        if found is None:
            raise ValueError('Crop applies to a recorded session, not an existing body project.')
        self.session_dir = found
        fps = self.info['fps'] if self.info else 30
        start = self.slider.clip_in / fps
        end = self.slider.clip_out / fps
        if end - start < 0.2:
            raise ValueError('Select a longer timeline range.')
        return start, end

    def crop_session(self):
        try:
            start, end = self._range()
        except (ValueError, AttributeError, TypeError, ZeroDivisionError) as exc:
            QMessageBox.warning(self, 'Crop session', str(exc)); return
        folder = Path(self.session_dir)
        self.stop_playback()
        self.video_view.shutdown()
        if not (folder / 'full' / 'take.mp4').is_file():
            (folder / 'full').mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / 'take.mp4', folder / 'full' / 'take.mp4')
            if (folder / 'face_capture').is_dir():
                shutil.copytree(folder / 'face_capture', folder / 'full' / 'face_capture')
        if self._write_clip(folder, folder / 'take.mp4', start, end, replace=True):
            self.session_status.setText('Cropped the session to the timeline selection. The original is in full/.')

    def save_session_clip(self):
        try:
            start, end = self._range()
        except (ValueError, AttributeError, TypeError, ZeroDivisionError) as exc:
            QMessageBox.warning(self, 'Save clip', str(exc)); return
        name, ok = QInputDialog.getText(self, 'Save clip', 'Clip name:')
        if not ok or not name.strip():
            return
        safe = ''.join(ch if ch.isalnum() or ch in '-_ ' else '-' for ch in name.strip())[:40].strip() or 'clip'
        dest = Path(self.session_dir) / 'clips' / safe
        if dest.exists():
            QMessageBox.warning(self, 'Save clip', 'A clip with that name already exists.'); return
        if not self._write_clip(dest, Path(self.session_dir) / 'take.mp4', start, end, replace=False):
            return
        write_session(dest)
        self._fill_clips()
        self.session_status.setText('Saved clip “%s”. The full session is still open.' % safe)

    def _write_clip(self, dest, source_video, start, end, replace):
        self.stop_playback()
        self.video_view.shutdown()
        temporary = dest / '_crop' if replace else dest
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True, exist_ok=True)
        try:
            cut_media(source_video, temporary / 'take.mp4', start, end - start)
            if (Path(self.session_dir) / 'face_capture' / 'meta.json').is_file():
                try:
                    clip_recording(self.session_dir, temporary, start, end)
                except ValueError:
                    pass
            if any(stream.get('codec_type') == 'audio' for stream in self._streams(temporary / 'take.mp4')):
                wav = extract_audio(temporary / 'take.mp4', temporary / 'face_capture' / 'audio.wav')
                import_audio(temporary, wav)
        except Exception as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            self.load_video(source_video)
            QMessageBox.warning(self, 'Crop', str(exc))
            return False
        if replace:
            shutil.copy2(temporary / 'take.mp4', dest / 'take.mp4')
            if (temporary / 'face_capture').is_dir():
                replace_tree(temporary / 'face_capture', dest / 'face_capture')
            shutil.rmtree(temporary, ignore_errors=True)
            self.load_video(dest / 'take.mp4')
            self.reload_face(force=True)
        else:
            self.load_video(source_video)
        return True

    def _fill_clips(self):
        self.session_clips.blockSignals(True)
        self.session_clips.clear()
        self.session_clips.addItem('Open a saved clip…', None)
        folder = Path(self.session_dir) / 'clips' if self.session_dir else None
        if folder and folder.is_dir():
            for child in sorted(folder.iterdir()):
                if (child / 'take.mp4').is_file():
                    self.session_clips.addItem(child.name, str(child / 'take.mp4'))
        self.session_clips.blockSignals(False)

    def open_session_clip(self, index):
        path = self.session_clips.itemData(index)
        if path:
            self.load_video(Path(path))
            self.reload_face(force=True)

    def fetch_session_voices(self):
        key = self.session_key.text().strip()
        _save_key(key)
        if not key:
            self.session_voice_status.setText('Enter an ElevenLabs API key.'); return
        self.session_voice_status.setText('Fetching voices…')
        thread = _Voices(key, self)
        thread.ready.connect(self._voices_ready)
        thread.failed.connect(self.session_voice_status.setText)
        thread.start()
        self._voice_thread = thread

    def _voices_ready(self, voices, models):
        self.session_voices.clear()
        for voice in voices:
            self.session_voices.addItem(voice['name'], voice['voice_id'])
        self.session_models.clear()
        self.session_models.addItem('Default model', '')
        for model in models:
            self.session_models.addItem(model['name'], model['model_id'])
        self.session_voice_status.setText('%d voices.' % len(voices))

    def generate_session_voice(self):
        key = self.session_key.text().strip()
        _save_key(key)
        voice = self.session_voices.currentData()
        audio = self._original_audio()
        if not key or not voice or audio is None:
            self.session_voice_status.setText('Fetch a voice and record or import audio first.'); return
        options = {
            'model_id': self.session_models.currentData() or None,
            'stability': self.session_stability.value(),
            'similarity_boost': self.session_similarity.value(),
            'remove_background_noise': self.session_noise.isChecked(),
            'output_format': self.session_format.currentData() or None,
        }
        self.session_generate.setEnabled(False)
        self.session_voice_status.setText('Converting…')
        thread = _Convert(key, voice, audio, options, self.session_voices.currentText())
        thread.ready.connect(self._voice_ready)
        thread.failed.connect(self._voice_failed)
        thread.start()
        self._voice_thread = thread

    def _original_audio(self):
        root = self.face_root()
        meta = read_meta(root) or {}
        name = meta.get('original_audio') or meta.get('audio')
        if not name:
            return None
        path = folder(root) / name
        return path if path.is_file() else None

    def _voice_ready(self, voice_name, payload, suffix):
        self.session_generate.setEnabled(True)
        root = self.face_root()
        dest = folder(root) / 'voices'
        dest.mkdir(parents=True, exist_ok=True)
        safe = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in voice_name)[:32] or 'voice'
        path = dest / (safe + suffix)
        path.write_bytes(payload)
        meta = read_meta(root) or {'version': 1}
        meta.setdefault('original_audio', meta.get('audio'))
        meta['audio'] = str(path.relative_to(folder(root))).replace('\\', '/')
        atomic_json(folder(root) / 'meta.json', meta)
        self.reload_face(force=True)
        self.session_voice_status.setText('Using the %s voice. Original audio is kept.' % voice_name)

    def _voice_failed(self, message):
        self.session_generate.setEnabled(True)
        self.session_voice_status.setText(message)

    def close_session(self):
        self.session_clock.stop()
        process = self._session_process
        self._session_process = None
        if process is not None:
            process.write(b'q\n')
            if not process.waitForFinished(2000):
                process.kill()
                process.waitForFinished(1000)

    def use_original_session_audio(self):
        root = self.face_root()
        meta = read_meta(root)
        if not meta or not meta.get('original_audio'):
            self.session_voice_status.setText('No original audio is stored yet.'); return
        meta['audio'] = meta['original_audio']
        atomic_json(folder(root) / 'meta.json', meta)
        self.reload_face(force=True)
        self.session_voice_status.setText('Playback is using the original recording.')


class _Voices(QThread):
    ready = Signal(object, object)
    failed = Signal(str)

    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.key = key

    def run(self):
        try:
            self.ready.emit(fetch_voices(self.key), fetch_models(self.key))
        except Exception as exc:
            self.failed.emit(str(exc))
