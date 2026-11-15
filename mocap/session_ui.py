"""Record a camera, Quest face, and microphone together, then crop or change the voice."""
from datetime import datetime
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import time
from PySide6.QtCore import Qt, QProcess, QThread, Signal, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QDoubleSpinBox, QCheckBox, QMessageBox, QInputDialog, QScrollArea, QFrame,
    QListWidget, QListWidgetItem, QAbstractItemView, QPlainTextEdit, QApplication)
from .paths import HOME, PROJECTS
from .storage import atomic_json
from .face_capture import (audio_library, folder, import_audio, read_meta,
                           save_recording, wave_from_rows)
from .session_record import (DeviceMic, LevelTap, PcmTrack, align_take, clip_take, cut_media, ensure_wav, extract_audio,
                             headset_microphone, list_devices, pcm_wav, prepare_speech, read_last_open, record_command,
                             remux_transport, replace_tree, session_for, write_aligned_wav,
                             write_last_open, write_session, write_variation_wav)
from .tooltips import tooltip_text
from .elevenlabs import convert, fetch_models, fetch_voices, transcribe
from . import captions

KEY_PATH = HOME / '.setup' / 'elevenlabs.json'


def _load_key():
    try:
        return json.loads(KEY_PATH.read_text(encoding='utf-8')).get('key', '')
    except (OSError, ValueError):
        return ''


def _save_key(value):
    try:
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(KEY_PATH, {'key': value})
    except OSError:
        pass


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

    def __init__(self, key, voice_id, audio, options, voice_name, crop=None, cleanup=None):
        super().__init__()
        self.key, self.voice_id, self.audio, self.options, self.voice_name = key, voice_id, audio, options, voice_name
        self.crop = crop
        self.cleanup = cleanup

    def run(self):
        temporary = None
        try:
            audio = self.audio
            options = dict(self.options)
            normalize = bool(options.pop('normalize', False))
            compress = bool(options.pop('compress', False))
            if self.crop or normalize or compress:
                temporary = Path(tempfile.mkdtemp(prefix='rgb-speech-'))
            if self.crop:
                from .session_record import aligned_window, cut_audio, wav_info
                length = wav_info(audio)[0]
                window = aligned_window(0.0, length, self.crop)
                if window is None:
                    raise RuntimeError('The audio crop has no sound in it.')
                content_lead, pad, out_len = window
                audio = cut_audio(audio, temporary / 'cropped.wav', content_lead, out_len, delay=pad)
            if normalize or compress:
                audio = prepare_speech(audio, temporary / 'speech.wav', normalize=normalize, compress=compress)
            payload, suffix = convert(self.key, self.voice_id, audio, options)
            self.ready.emit(self.voice_name, payload, suffix)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            if self.cleanup:
                shutil.rmtree(self.cleanup, ignore_errors=True)


class _Transcribe(QThread):
    ready = Signal(str, object)
    failed = Signal(str)

    def __init__(self, key, audio, model, crop=None, cleanup=None):
        super().__init__()
        self.key, self.audio, self.model, self.crop = key, audio, model, crop
        self.cleanup = cleanup

    def run(self):
        temporary = None
        try:
            audio = self.audio
            if self.crop:
                from .session_record import aligned_window, cut_audio, wav_info
                length = wav_info(audio)[0]
                window = aligned_window(0.0, length, self.crop)
                if window is None:
                    raise RuntimeError('The audio crop has no sound in it.')
                temporary = Path(tempfile.mkdtemp(prefix='rgb-speech-'))
                content_lead, pad, out_len = window
                audio = cut_audio(audio, temporary / 'cropped.wav', content_lead, out_len, delay=pad)
            text, words = transcribe(self.key, audio, self.model)
            self.ready.emit(text, words)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            if self.cleanup:
                shutil.rmtree(self.cleanup, ignore_errors=True)


class SessionWorkspace:
    def init_session(self):
        self.session_dir = None
        self._record_undo = []
        self._record_redo = []
        self._before_record = None
        self._session_process = None
        self._session_video = None
        self._session_started = None
        self._device_thread = None
        self._voice_thread = None
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.session_key = QLineEdit()
        self.session_key.setEchoMode(QLineEdit.Password)
        self.session_key.setPlaceholderText('API key')
        self.session_key.setText(_load_key())
        layout.addWidget(self.session_key)
        self.session_voices = QComboBox()
        self.session_models = QComboBox()
        layout.addWidget(QLabel('Voice'))
        layout.addWidget(self.session_voices)
        layout.addWidget(QLabel('Model'))
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
        self.session_normalize = QCheckBox('Normalize')
        self.session_normalize.setChecked(True)
        self.session_normalize.setToolTip(tooltip_text('Raise a quiet recording to a steady level before it is sent. The saved audio is not changed.'))
        layout.addWidget(self.session_normalize)
        self.session_compress = QCheckBox('Compress')
        self.session_compress.setChecked(True)
        self.session_compress.setToolTip(tooltip_text('Even out loud and quiet parts of the selected recording before it is sent, so softer words carry. The saved audio is not changed.'))
        layout.addWidget(self.session_compress)
        self.session_format = QComboBox()
        self.session_format.addItem('Default', '')
        self.session_format.addItem('MP3 44.1 kHz 128k', 'mp3_44100_128')
        self.session_format.addItem('PCM 44.1 kHz', 'pcm_44100')
        layout.addWidget(QLabel('Format'))
        layout.addWidget(self.session_format)
        voice_row = QHBoxLayout()
        self.session_fetch = QPushButton('Fetch voices')
        self.session_generate = QPushButton('Generate variant')
        voice_row.addWidget(self.session_fetch)
        voice_row.addWidget(self.session_generate)
        layout.addLayout(voice_row)
        self.session_voice_status = QLabel('')
        self.session_voice_status.setWordWrap(True)
        layout.addWidget(self.session_voice_status)
        layout.addWidget(QLabel('Playback'))
        self.session_audio_takes = QListWidget()
        self.session_audio_takes.setObjectName('audioTakes')
        self.session_audio_takes.setMinimumHeight(132)
        self.session_audio_takes.setSelectionMode(QAbstractItemView.SingleSelection)
        self.session_audio_takes.setToolTip('Choose the original recording or a voice variation. Playback uses the selected row. Generating adds a variation and leaves the others in this list.')
        layout.addWidget(self.session_audio_takes)
        self.session_audio_empty = QLabel('Record a take, then generate a variation. The original stays in this list.')
        self.session_audio_empty.setWordWrap(True)
        self.session_audio_empty.setObjectName('muted')
        layout.addWidget(self.session_audio_empty)
        layout.addWidget(QLabel('Captions'))
        self.caption_model = QComboBox()
        self.caption_model.addItem('Scribe v2', 'scribe_v2')
        self.caption_model.addItem('Scribe v1', 'scribe_v1')
        self.caption_model.setToolTip(tooltip_text('ElevenLabs speech-to-text model. It returns a start and end time for every word.'))
        layout.addWidget(self.caption_model)
        self.caption_transcribe = QPushButton('Transcribe take')
        self.caption_transcribe.setToolTip(tooltip_text('Transcribe the applied take in the list above. Captions are kept per take.'))
        layout.addWidget(self.caption_transcribe)
        self.caption_lines = QPlainTextEdit()
        self.caption_lines.setPlaceholderText('Transcribe first, then edit. Each line becomes one timed caption, in order.')
        self.caption_lines.setMinimumHeight(120)
        layout.addWidget(self.caption_lines)
        caption_row = QHBoxLayout()
        self.caption_align = QPushButton('Get timings')
        self.caption_align.setToolTip(tooltip_text('Match each line to the transcript words. Edit the wording or split lines freely, but keep the spoken order.'))
        self.caption_copy = QPushButton('Copy SRT')
        caption_row.addWidget(self.caption_align)
        caption_row.addWidget(self.caption_copy)
        layout.addLayout(caption_row)
        self.caption_status = QLabel('')
        self.caption_status.setWordWrap(True)
        layout.addWidget(self.caption_status)
        self.caption_timings_label = QLabel('Final captions')
        self.caption_timings_label.hide()
        layout.addWidget(self.caption_timings_label)
        self.caption_timings = QPlainTextEdit()
        self.caption_timings.setObjectName('captionTimings')
        self.caption_timings.setToolTip(tooltip_text('One caption per line: start - end  text, in seconds from the start of the take audio. Edits save as you type. Guii export shifts them to the exported clip.'))
        self.caption_timings.setMinimumHeight(120)
        self.caption_timings.hide()
        layout.addWidget(self.caption_timings)
        self._caption_edit = QTimer(self)
        self._caption_edit.setSingleShot(True)
        self._caption_edit.setInterval(400)
        self._caption_edit.timeout.connect(self._save_caption_edits)
        self._caption_showing = False
        self.caption_timings.textChanged.connect(self._caption_edited)
        layout.addStretch()
        scroll.setWidget(body)
        outer = QVBoxLayout(self.session_host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        capture_note = QLabel('Enter this address in RGB Capture on the iPhone. Record here or on the phone; both start the same take. Reconstruct 3D runs on the saved take afterward.')
        capture_note.setWordWrap(True)
        layout.addWidget(capture_note)
        self.session_link = QLabel(self.capture_url())
        self.session_link.setWordWrap(True)
        self.session_link.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.session_link)
        self.session_copy = QPushButton('Copy address')
        layout.addWidget(self.session_copy)
        self.session_camera = QComboBox()
        self.session_mic = QComboBox()
        layout.addWidget(QLabel('Camera'))
        layout.addWidget(self.session_camera)
        layout.addWidget(QLabel('Microphone'))
        layout.addWidget(self.session_mic)
        self.session_output = QComboBox()
        layout.addWidget(QLabel('Speakers'))
        layout.addWidget(self.session_output)
        row = QHBoxLayout()
        self.session_refresh = QPushButton('Refresh devices')
        self.session_record = QPushButton('Record')
        self.session_record.setObjectName('primary')
        row.addWidget(self.session_refresh)
        row.addWidget(self.session_record)
        layout.addLayout(row)
        self.session_status = QLabel('Connect RGB Capture on the iPhone, then Record.')
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
        layout.addStretch()
        scroll.setWidget(body)
        outer = QVBoxLayout(self.capture_host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self.caption_transcribe.clicked.connect(self.transcribe_captions)
        self.caption_align.clicked.connect(self.align_captions)
        self.caption_copy.clicked.connect(self.copy_captions)
        self._caption_thread = None
        self._caption_key = None
        self.session_refresh.clicked.connect(self.refresh_session_devices)
        self.session_record.clicked.connect(self.toggle_session_record)
        if hasattr(self, 'capture_record'):
            self.capture_record.clicked.connect(self.toggle_session_record)
        self.session_copy.clicked.connect(self.copy_camera_url)
        self.session_crop.clicked.connect(self.crop_session)
        self.session_save_clip.clicked.connect(self.save_session_clip)
        self.session_clips.activated.connect(self.open_session_clip)
        self.session_fetch.clicked.connect(self.fetch_session_voices)
        self.session_voices.currentIndexChanged.connect(self.remember_character_voice)
        self.session_generate.clicked.connect(self.generate_session_voice)
        self.session_audio_takes.currentRowChanged.connect(self._choose_audio_take)
        self.session_key.textChanged.connect(self._remember_key)
        self._filling_audio = False
        self._fill_audio_takes()
        self.session_clock = QTimer(self)
        self.session_clock.setInterval(500)
        self.session_clock.timeout.connect(self._session_tick)
        self._phone_path = None
        self._audio_process = None
        self._device_mic = None
        self._level_tap = None
        self._meter_shown = 0.0
        self._audio_names = []
        self._pending_mic = None
        self._capture_lead = None
        self._session_from_device = False
        self._session_stopping = False
        self.session_follow = QTimer(self)
        self.session_follow.setInterval(200)
        self.session_follow.timeout.connect(self._follow_device_record)
        self.session_follow.start()
        self._meter_clock = QTimer(self)
        self._meter_clock.setInterval(50)
        self._meter_clock.timeout.connect(self._meter_tick)
        self._meter_clock.start()
        self.session_camera.setToolTip('RGB Capture streams the iPhone back camera. A webcam on this PC is the fallback.')
        self.session_mic.setToolTip('iPhone and Quest microphones use the same clock as the picture. Windows inputs are every recording device on this PC.')
        if hasattr(self, 'capture_mic'):
            self.capture_mic.setToolTip(self.session_mic.toolTip())
            self.capture_mic.currentIndexChanged.connect(self._mic_from_header)
        self.session_mic.currentIndexChanged.connect(self._mic_from_dock)
        self.session_output.setToolTip('Play sends the recorded microphone to this device.')
        self.session_output.currentIndexChanged.connect(self._output_from_dock)
        if hasattr(self, 'capture_output'):
            self.capture_output.currentIndexChanged.connect(self._output_from_header)
        from PySide6.QtMultimedia import QMediaDevices
        self._media_devices = QMediaDevices(self)
        self._media_devices.audioOutputsChanged.connect(self._fill_outputs)
        self._speaker_id = (read_last_open() or {}).get('speaker') or ''
        self._fill_outputs()
        self.session_crop.setToolTip('Replace this session with the timeline selection. Video, face, and audio stay aligned.')
        self.session_save_clip.setToolTip('Keep the full session and write the timeline selection as a separate clip.')
        self.refresh_session_devices()
        if self.session_key.text().strip():
            QTimer.singleShot(0, self.fetch_session_voices)

    def copy_camera_url(self):
        url = self.capture_url()
        QGuiApplication.clipboard().setText(url)
        self.session_status.setText('Copied the capture address.')

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
        self.session_camera.addItem('iPhone camera', 'phone')
        for name in video or []:
            self.session_camera.addItem(name, name)
        index = self.session_camera.findData(current) if current else 0
        self.session_camera.setCurrentIndex(index if index >= 0 else 0)
        self._audio_names = list(audio or [])
        self._fill_mics(self._pending_mic if self._pending_mic is not None else self.session_mic.currentData())
        self._pending_mic = None
        self.session_status.setText('Ready. Connect RGB Capture on the iPhone, then Record.')

    def _fill_mics(self, selected):
        choices = [('iPhone camera microphone', 'phone'), ('Quest Pro microphone', 'quest'), ('No microphone', '')]
        choices.extend((name, name) for name in self._audio_names)
        if selected not in {item[1] for item in choices}:
            selected = 'phone'
        for box in self._mic_boxes():
            box.blockSignals(True)
            box.clear()
            for label, value in choices:
                box.addItem(label, value)
            index = box.findData(selected)
            box.setCurrentIndex(index if index >= 0 else 0)
            box.blockSignals(False)
        self._sync_meter()

    def _mic_boxes(self):
        boxes = [self.session_mic]
        if hasattr(self, 'capture_mic'):
            boxes.append(self.capture_mic)
        return boxes

    def _mic_from_header(self):
        if not hasattr(self, 'capture_mic'):
            return
        self._select_mic(self.capture_mic.currentData())

    def _mic_from_dock(self):
        self._select_mic(self.session_mic.currentData())

    def _select_mic(self, value):
        for box in self._mic_boxes():
            if box.currentData() == value:
                continue
            box.blockSignals(True)
            index = box.findData(value)
            if index >= 0:
                box.setCurrentIndex(index)
            box.blockSignals(False)
        self._sync_meter()

    def _output_boxes(self):
        boxes = [self.session_output]
        if hasattr(self, 'capture_output'):
            boxes.append(self.capture_output)
        return boxes

    def _output_from_header(self):
        if hasattr(self, 'capture_output'):
            self._choose_output(self.capture_output.currentData() or '')

    def _output_from_dock(self):
        self._choose_output(self.session_output.currentData() or '')

    def _choose_output(self, ident):
        self._select_output(ident)
        name = self.session_output.currentText()
        if name:
            self.status.setText('Playback through %s.' % name)
        self.remember_last_open()

    def _fill_outputs(self, *_):
        from PySide6.QtMultimedia import QMediaDevices
        devices = [device for device in QMediaDevices.audioOutputs() if not device.isNull()]
        default = QMediaDevices.defaultAudioOutput()
        chosen = getattr(self, '_speaker_id', '') or ''
        if not chosen and not default.isNull():
            chosen = bytes(default.id()).decode('latin1')
        for box in self._output_boxes():
            box.blockSignals(True)
            box.clear()
            for device in devices:
                box.addItem(device.description(), bytes(device.id()).decode('latin1'))
            box.blockSignals(False)
        self._select_output(chosen)

    def _select_output(self, ident):
        ident = ident or ''
        boxes = self._output_boxes()
        if ident and not any(box.findData(ident) >= 0 for box in boxes) and boxes and boxes[0].count():
            ident = boxes[0].itemData(0) or ''
        self._speaker_id = ident
        for box in boxes:
            if box.currentData() == ident:
                continue
            box.blockSignals(True)
            index = box.findData(ident)
            if index >= 0:
                box.setCurrentIndex(index)
            box.blockSignals(False)
        self._apply_output(ident)

    def _apply_output(self, ident):
        from PySide6.QtMultimedia import QMediaDevices
        if not hasattr(self, 'face_audio'):
            return
        for device in QMediaDevices.audioOutputs():
            if bytes(device.id()).decode('latin1') == ident:
                self.face_audio.set_device(device)
                return

    def _stop_level(self):
        tap = getattr(self, '_level_tap', None)
        self._level_tap = None
        if tap is not None:
            tap.stop()

    def _sync_meter(self):
        mode, device = ('', '')
        if hasattr(self, 'session_mic') and self.session_mic.count():
            mode, device = self._mic_choice()
        hub = self.face_server.hub if getattr(self, 'face_server', None) is not None else None
        if hub is not None:
            hub.set_monitor(mode if mode in ('phone', 'quest') else '')
        slider = getattr(self, 'slider', None)
        if slider is not None:
            slider.audio_meter_on = bool(mode)
            if not mode:
                slider.audio_level = 0.0
                self._meter_shown = 0.0
            slider.update()
        if self._session_active() or mode != 'device' or not device:
            self._stop_level()
            return
        tap = self._level_tap
        if tap is not None and tap.is_alive() and tap.device == device:
            return
        self._stop_level()
        if hub is None:
            return
        tap = LevelTap(device, hub.note_pcm)
        self._level_tap = tap
        tap.start()

    def _meter_tick(self):
        slider = getattr(self, 'slider', None)
        hub = self.face_server.hub if getattr(self, 'face_server', None) is not None else None
        if slider is None or hub is None or not getattr(slider, 'audio_meter_on', False):
            return
        tap = self._level_tap
        if tap is not None and tap.error and not tap.is_alive() and slider.audio_empty != tap.error:
            slider.audio_empty = tap.error
            slider.update()
        playing = self.timer.isActive() if hasattr(self, 'timer') else False
        face_audio = getattr(self, 'face_audio', None)
        if playing and face_audio is not None and face_audio.active:
            peak, at = face_audio.level, time.monotonic()
        else:
            peak, at = hub.meter()
            if at <= 0 or time.monotonic() - at > 0.35:
                peak = 0.0
        shown = peak if peak >= self._meter_shown else self._meter_shown * 0.72
        if shown < 0.0015:
            shown = 0.0
        self._meter_shown = shown
        display = min(1.0, (shown / 0.2) ** 0.5)
        if abs(display - slider.audio_level) < 0.004:
            return
        slider.audio_level = display
        slider.update()

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

    def _set_record_label(self, text):
        self.session_record.setText(text)
        if hasattr(self, 'capture_record'):
            self.capture_record.setText(text)
        self._sync_record_undo()

    def _phone_ready(self):
        if self.face_server is None:
            return False
        if self.face_server.hub.preview_jpeg():
            return True
        status = self.face_server.hub.status()
        if status.get('phone_connected'):
            return True
        return 'iphone' in (status.get('clients') or {})

    def toggle_session_record(self):
        if self._session_active():
            if self.face_server is not None:
                self.face_server.hub.stop()
            self._stop_session_record()
            return
        if self._phone_ready():
            index = self.session_camera.findData('phone')
            if index >= 0:
                self.session_camera.setCurrentIndex(index)
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
        elif not active:
            ready = self._phone_ready()
            self._set_record_label('Record iPhone' if ready else 'Record')
            text = 'iPhone connected. Record starts the phone from here.' if ready else 'Open RGB Capture on the iPhone. Record starts it once it is connected.'
            current = self.session_status.text()
            if current != text and (current == 'iPhone connected. Record starts the phone from here.'
                                     or current == 'Open RGB Capture on the iPhone. Record starts it once it is connected.'
                                     or current.startswith(('Connect RGB Capture', 'Ready.', 'Looking for', 'Copied the capture'))):
                self.session_status.setText(text)

    def _begin_session(self, reset_face):
        if self._session_active() or getattr(self, 'busy', False):
            return
        source = 'phone' if self._phone_ready() else self.session_camera.currentData()
        phone = source == 'phone'
        if not phone and (not source or source == 'No camera found'):
            self.session_status.setText('Choose the iPhone camera or a webcam first.')
            if self.face_server is not None and self.face_server.hub.recording:
                self.face_server.hub.stop()
            return
        self._before_record = self._open_state()
        name = datetime.now().strftime('session-%Y%m%d-%H%M%S')
        folder = write_session(PROJECTS / 'sessions' / name)
        if self.face_server is not None and reset_face:
            self.face_server.hub.begin_take()
        mode, device = self._mic_choice()
        self._stop_level()
        self.session_dir = folder
        self._session_video = folder / 'take.mp4'
        self._capture_lead = None
        self._audio_error = None
        self._start_microphone(mode, device, phone)
        if phone:
            self._phone_path = folder / 'take.ts'
            if self.face_server is not None and self.face_server.phone is not None:
                self.face_server.phone.open_take(self._phone_path)
        else:
            process = self._spawn(record_command(source, device if mode == 'device' else '', self._session_video))
            if process is None or not process.waitForStarted(3000):
                self._stop_microphone()
                self.session_status.setText('Could not start the camera recorder.')
                if self.face_server is not None:
                    self.face_server.hub.stop()
                self._sync_meter()
                return
            self._session_process = process
            if self.face_server is not None and self.face_server.hub.epoch is not None:
                self._capture_lead = max(0.0, time.time() - self.face_server.hub.epoch)
        self._session_started = time.monotonic()
        if hasattr(self, '_select_feed'):
            self._select_feed('live')
        self._arm_record_timeline()
        self._set_record_label('Stop')
        self.session_clock.start()
        self.session_status.setText('Recording. The Quest and the phone camera follow this take.')

    def _mic_choice(self):
        mic = self.session_mic.currentData() or ''
        if mic == 'quest':
            matched = headset_microphone(self._audio_names)
            if matched:
                return 'device', matched
            return 'quest', ''
        if mic == 'phone':
            return 'phone', ''
        if mic:
            return 'device', mic
        return '', ''

    def _start_microphone(self, mode, device, phone):
        hub = self.face_server.hub if self.face_server is not None else None
        if hub is None:
            return
        source = mode if mode in ('phone', 'quest') else ''
        track = None
        if mode == 'device' and phone and hub.epoch is not None:
            track = PcmTrack()
            self._device_mic = DeviceMic(device, track, hub.epoch, hub.note_pcm)
        elif mode in ('phone', 'quest'):
            track = PcmTrack()
        hub.set_audio_source(source)
        if track is not None:
            hub.open_audio(track)
        if self._device_mic is not None:
            self._device_mic.start()

    def _stop_microphone(self):
        mic = self._device_mic
        self._device_mic = None
        if mic is not None:
            mic.stop()
            if mic.error:
                self._audio_error = mic.error
        if self.face_server is None:
            return None
        track, _source = self.face_server.hub.take_audio()
        return track

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
        phone = self.face_server.phone if self.face_server is not None else None
        frames = phone.frames if phone is not None else 0
        error = ('  ' + phone.error) if phone is not None and phone.error else ''
        waiting = '' if frames else ' Waiting for the iPhone.'
        rows, quest = self._quest_take()
        quest_note = ''
        if rows:
            quest_note = '  %d Quest frames.' % len(rows)
        elif quest:
            quest_note = '  Quest connected.'
        audio_note = self._audio_note()
        self.session_status.setText('Recording  %d:%02d  ·  %d camera frames.%s%s%s%s' % (
            int(elapsed) // 60, int(elapsed) % 60, frames, waiting, quest_note, audio_note, error))
        self._paint_record_timeline(elapsed, rows, quest)

    def _quest_take(self):
        if self.face_server is None:
            return [], False
        hub = self.face_server.hub
        status = hub.status()
        clients = status.get('clients') or {}
        quest = bool(status.get('connected') or 'quest' in clients)
        with hub.lock:
            return list(hub.frames), quest

    def _arm_record_timeline(self):
        slider = getattr(self, 'slider', None)
        if slider is None:
            return
        slider.blockSignals(True)
        slider.setRange(0, 0)
        slider.clip_in, slider.clip_out = 0, 1
        slider.blockSignals(False)
        slider.frame_all()
        slider.fps = 30.0
        slider.has_motion = False
        slider.face_span = None
        slider.face_wave = None
        slider.face_empty = 'No face capture'
        slider.audio_span = None
        slider.audio_segments = []
        slider.audio_selection = None
        slider.audio_empty = 'No audio'
        if hasattr(self, 'face_audio'):
            self.face_audio.clear_arrangement()
        self._audio_play_sig = None
        slider.face_revision += 1
        slider.update()

    def _paint_record_timeline(self, elapsed, rows, quest):
        slider = getattr(self, 'slider', None)
        if slider is None:
            return
        end = max(1, int(round(elapsed * 30.0)) + 1)
        if slider.maximum() != end - 1:
            slider.blockSignals(True)
            slider.setRange(0, end - 1)
            slider.clip_in, slider.clip_out = 0, end
            slider.blockSignals(False)
            slider.frame_all()
        if rows:
            slider.face_span = (0.0, max(float(rows[-1][0]), 1.0 / 30.0))
            slider.face_empty = 'No face capture'
            slider.face_wave = wave_from_rows(rows)
        else:
            slider.face_span = None
            slider.face_wave = None
            slider.face_empty = 'Quest connected' if quest else 'No face capture'
        covered = None
        track = self.face_server.hub.audio_track if self.face_server is not None else None
        if track is not None:
            covered = track.coverage()
        if covered:
            origin = max(0.0, covered[0])
            slider.audio_span = (origin, covered[1])
            first = int(round(origin * 30))
            last = max(first + 1, int(round((origin + covered[1]) * 30)))
            slider.audio_segments = [(first, first, last)]
            slider.audio_empty = 'No audio'
        else:
            slider.audio_span = None
            slider.audio_segments = []
            slider.audio_empty = 'Waiting for microphone' if self._mic_choice()[0] else 'No audio'
        slider.face_revision += 1
        slider.update()

    def _audio_note(self):
        mode = self._mic_choice()[0]
        if not mode:
            return ''
        track = self.face_server.hub.audio_track if self.face_server is not None else None
        covered = track.coverage() if track is not None else None
        if covered and covered[1] > 0.05:
            return '  Microphone.'
        if mode == 'phone':
            return '  Waiting for the iPhone microphone.'
        if mode == 'quest':
            return '  Waiting for the Quest microphone.'
        return '  Waiting for the microphone.'

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
        try:
            self._finish_session_body()
        finally:
            self._sync_meter()

    def _finish_session_body(self):
        process, audio, video = self._session_process, self._audio_process, self._session_video
        phone_path = self._phone_path
        self._session_process = None
        self._audio_process = None
        self._phone_path = None
        self.session_clock.stop()
        self._set_record_label('Record')
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
        face_names = self.face_server.hub.expression_list() if self.face_server is not None else None
        if lead <= 0 and self._capture_lead:
            lead = float(self._capture_lead)
        audio_track = self._stop_microphone()
        folder = self.session_dir
        audio_saved = self._import_take_audio(folder, audio_track, lead, video)
        if video is None or not video.is_file() or video.stat().st_size < 1024:
            if frames and folder is not None:
                try:
                    save_recording(folder, frames, names=face_names)
                    self._save_eyes(folder, eyes, lead)
                    self.reload_face(force=True)
                except (OSError, ValueError):
                    pass
            detail = ''
            phone = self.face_server.phone if self.face_server is not None else None
            if phone is not None and phone.error:
                detail = ' ' + phone.error
            if phone_path is not None:
                self.session_status.setText('Recording produced no video. Connect RGB Capture on the iPhone, then Record.' + detail)
            else:
                self.session_status.setText('Recording produced no video.' + detail)
            return
        try:
            if frames:
                save_recording(folder, frames, names=face_names)
            self._save_eyes(folder, eyes, lead)
        except (OSError, ValueError, RuntimeError) as exc:
            self.session_status.setText('Saved the video, but face or audio failed: ' + str(exc))
        self.load_video(video)
        self.reload_face(force=True)
        self._fill_clips()
        if self._before_record is not None:
            self._record_undo.append(self._before_record)
            self._record_redo.clear()
            self._before_record = None
            self._sync_record_undo()
        extra = f' {len(frames)} face frames.' if frames else ' No Quest frames arrived.'
        if phone_frames:
            extra += f' {phone_frames} video frames.'
        if audio_saved:
            extra += ' Audio saved.'
        elif self._mic_choice()[0]:
            extra += ' No microphone audio.'
            if self._audio_error:
                extra += ' ' + self._audio_error
        self.session_status.setText('Session saved.' + extra + ' Trim it, or Reconstruct 3D for the body.')
        self.status.setText('Live session saved. Body tracking runs when you Reconstruct 3D.')

    def _import_take_audio(self, folder, track, lead, video):
        if folder is None:
            return False
        try:
            if track is not None:
                snap = track.snapshot()
                if snap:
                    pcm, rate, origin = snap
                    wav = write_aligned_wav(folder / 'mic.wav', pcm, rate, origin, lead)
                    if wav is not None:
                        import_audio(folder, wav)
                        return True
            if video is not None and video.is_file() and any(
                    stream.get('codec_type') == 'audio' for stream in self._streams(video)):
                wav = extract_audio(video, folder / 'mic-from-video.wav')
                import_audio(folder, wav)
                return True
        except (OSError, ValueError, RuntimeError, MemoryError):
            return False
        return False

    def _open_state(self):
        project = getattr(self, 'workspace_root', None) or getattr(self, 'project', None)
        video = getattr(self, 'video_path', None)
        if project is None and video is None:
            return None
        return dict(project=project, video=video)

    def _reopen(self, state):
        project, video = state.get('project'), state.get('video')
        if project and Path(project).joinpath('project.json').is_file():
            self.load_project(Path(project))
            return True
        if video and Path(video).is_file():
            self.load_video(Path(video))
            self.reload_face(force=True)
            self._fill_clips()
            return True
        return False

    def _sync_record_undo(self):
        if not hasattr(self, 'undo_recording_action'):
            return
        idle = not self._session_active() and not getattr(self, 'busy', False)
        self.undo_recording_action.setEnabled(idle and bool(self._record_undo))
        self.redo_recording_action.setEnabled(idle and bool(self._record_redo))

    def _step_recording(self, back, forward, verb):
        if self._session_active() or getattr(self, 'busy', False) or not back:
            return
        current = self._open_state()
        state = back.pop()
        if not self._reopen(state):
            self.session_status.setText(f'Cannot {verb}: that take is no longer on disk.')
            self._sync_record_undo()
            return
        if current is not None:
            forward.append(current)
        self._sync_record_undo()
        shown = Path(state['project'] or state['video'])
        self.session_status.setText(f'Reopened {shown.parent.name if shown.name == "take.mp4" else shown.name}. '
                                    'No recording was deleted; every take stays in projects/sessions.')

    def undo_recording(self):
        graph = getattr(self, 'face_graph', None)
        if graph is not None and graph.claim_history(False):
            return
        self._step_recording(self._record_undo, self._record_redo, 'undo the recording')

    def redo_recording(self):
        graph = getattr(self, 'face_graph', None)
        if graph is not None and graph.claim_history(True):
            return
        self._step_recording(self._record_redo, self._record_undo, 'redo the recording')

    def remember_last_open(self):
        if getattr(self, '_suppress_last_open', False):
            return
        video = getattr(self, 'video_path', None)
        project = getattr(self, 'project', None) or getattr(self, 'workspace_root', None)
        mic = self.session_mic.currentData() if hasattr(self, 'session_mic') else ''
        try:
            write_last_open(video, project, mic, getattr(self, '_speaker_id', ''))
        except OSError:
            pass

    def restore_last_open(self):
        data = read_last_open()
        if not data:
            return
        self._pending_mic = data.get('microphone') or None
        if self._pending_mic and hasattr(self, 'session_mic') and self.session_mic.count():
            self._fill_mics(self._pending_mic)
        project = data.get('project')
        video = data.get('video')
        if project and Path(project).joinpath('project.json').is_file():
            self.load_project(Path(project))
            return
        if video and Path(video).is_file():
            self.load_video(Path(video))
            self.reload_face(force=True)

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
            clip_take(self.session_dir, temporary, start, end)
            if not (read_meta(temporary) or {}).get('audio') and any(
                    stream.get('codec_type') == 'audio' for stream in self._streams(temporary / 'take.mp4')):
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

    def _remember_key(self, *_):
        if hasattr(self, 'session_key'):
            _save_key(self.session_key.text().strip())

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
        self._filling_voices = True
        try:
            self.session_voices.clear()
            for voice in voices:
                self.session_voices.addItem(voice['name'], voice['voice_id'])
            self.session_models.clear()
            self.session_models.addItem('Default model', '')
            for model in models:
                self.session_models.addItem(model['name'], model['model_id'])
        finally:
            self._filling_voices = False
        if hasattr(self, 'apply_character_voice') and self.apply_character_voice():
            return
        self.session_voice_status.setText('%d voices.' % len(voices))
        if hasattr(self, 'remember_character_voice'):
            self.remember_character_voice()

    def generate_session_voice(self):
        key = self.session_key.text().strip()
        _save_key(key)
        voice = self.session_voices.currentData()
        audio = self._playback_audio()
        if not key or not voice or audio is None:
            self.session_voice_status.setText('Fetch a voice and record or import audio first.'); return
        normalize = self.session_normalize.isChecked()
        compress = self.session_compress.isChecked()
        options = {
            'model_id': self.session_models.currentData() or None,
            'stability': self.session_stability.value(),
            'similarity_boost': self.session_similarity.value(),
            'remove_background_noise': self.session_noise.isChecked(),
            'output_format': self.session_format.currentData() or None,
            'normalize': normalize,
            'compress': compress,
        }
        target = self.current_library_character() if hasattr(self, 'current_library_character') else None
        self._voice_target = (target, voice, self.session_voices.currentText()) if target else None
        audio, cleanup = self.arranged_audio()
        if audio is None:
            self.session_voice_status.setText('Fetch a voice and record or import audio first.')
            return
        self.session_generate.setEnabled(False)
        self.session_voice_status.setText('Preparing the recording, then converting…' if normalize or compress else 'Converting…')
        thread = _Convert(key, voice, audio, options, self.session_voices.currentText(), cleanup=cleanup)
        thread.ready.connect(self._voice_ready)
        thread.failed.connect(self._voice_failed)
        thread.start()
        self._voice_thread = thread

    def _playback_audio(self):
        root = self.face_root()
        meta = read_meta(root) or {}
        for name in (meta.get('audio'), meta.get('original_audio'), 'audio.wav'):
            path = folder(root) / name if name else None
            if path is not None and path.is_file():
                return path
        return None

    def _variant_destination(self, base, label):
        safe = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in (label or 'voice'))[:32].strip('-') or 'voice'
        dest_dir = base / 'voices'
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (safe + '.wav')
        number = 2
        while dest.exists():
            dest = dest_dir / ('%s-%d.wav' % (safe, number))
            number += 1
        return dest

    def _repair_playback_audio(self, root, meta):
        name = str(meta.get('audio') or '').replace('\\', '/')
        path = folder(root) / name if name else None
        if path is None or not path.is_file() or pcm_wav(path):
            return False
        if getattr(self, '_audio_repair_failed', None) == str(path):
            return False
        try:
            wav = ensure_wav(path)
        except (OSError, RuntimeError, ValueError) as exc:
            self._audio_repair_failed = str(path)
            self.session_voice_status.setText(str(exc))
            return False
        rel = wav.relative_to(folder(root)).as_posix()
        meta['audio'] = rel
        for item in meta.get('audio_takes') or []:
            if isinstance(item, dict) and str(item.get('file') or '').replace('\\', '/') == name:
                item['file'] = rel
        atomic_json(folder(root) / 'meta.json', meta)
        return True

    def _fill_audio_takes(self):
        box = getattr(self, 'session_audio_takes', None)
        if box is None or self._filling_audio:
            return
        root = self.face_root()
        meta = read_meta(root) or {}
        if self._repair_playback_audio(root, meta):
            self._filling_audio = True
            try:
                self.reload_face(force=True)
            finally:
                self._filling_audio = False
            self._fill_audio_takes()
            return
        takes = audio_library(root, meta)
        active = str(meta.get('audio') or '').replace('\\', '/')
        box.blockSignals(True)
        box.clear()
        selected = -1
        for index, take in enumerate(takes):
            text = take['label']
            if take['file'] == active:
                text += '  —  Applied'
                selected = index
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, take['file'])
            item.setData(Qt.UserRole + 1, take['label'])
            box.addItem(item)
        if selected >= 0:
            box.setCurrentRow(selected)
        box.blockSignals(False)
        empty = self.session_audio_empty
        empty.setVisible(not takes)
        self._load_captions()

    def _caption_take(self):
        root = self.face_root()
        audio = self._playback_audio()
        return root, audio.relative_to(folder(root)).as_posix() if audio is not None else ''

    def _load_captions(self):
        if not hasattr(self, 'caption_lines'):
            return
        root, take = self._caption_take()
        key = (str(root), take)
        if key == self._caption_key:
            return
        if self._caption_edit.isActive():
            self._save_caption_edits()
        self._caption_key = key
        entry = captions.for_take(root, take) if take else {}
        self.caption_lines.setPlainText(entry.get('lines') or entry.get('text') or '')
        self._show_captions(entry.get('captions'))
        if entry.get('captions'):
            self.caption_status.setText('%d captions for %s.' % (len(entry['captions']), take))
        elif entry.get('words'):
            self.caption_status.setText('Transcribed. Split into one caption per line, then Get timings.')
        else:
            self.caption_status.setText('Transcribe the applied take to start.' if take else 'Record or import audio first.')

    def _show_captions(self, rows):
        self._caption_edit.stop()
        self._caption_showing = True
        try:
            self.caption_timings.setPlainText(captions.summary(rows) if rows else '')
        finally:
            self._caption_showing = False
        self.caption_timings.setVisible(bool(rows))
        self.caption_timings_label.setVisible(bool(rows))

    def _caption_edited(self):
        if not self._caption_showing:
            self._caption_edit.start()

    def _save_caption_edits(self):
        self._caption_edit.stop()
        root, take = self._caption_key or ('', '')
        if not take:
            return
        try:
            rows = captions.parse(self.caption_timings.toPlainText())
            captions.save_take(root, take, captions=rows)
        except (OSError, ValueError) as exc:
            self.caption_status.setText(str(exc)); return
        self.caption_status.setText('Saved %d captions.' % len(rows))

    def transcribe_captions(self):
        if self._caption_thread is not None and self._caption_thread.isRunning():
            return
        key = self.session_key.text().strip()
        _save_key(key)
        root, take = self._caption_take()
        if not key:
            self.caption_status.setText('Enter an ElevenLabs API key at the top of this tab.'); return
        if not take:
            self.caption_status.setText('Record or import audio first.'); return
        self._caption_job = (root, take)
        audio, cleanup = self.arranged_audio()
        if audio is None:
            self.caption_status.setText('Record or import audio first.')
            return
        self.caption_transcribe.setEnabled(False)
        self.caption_status.setText('Transcribing %s…' % take)
        thread = _Transcribe(key, audio, self.caption_model.currentData(), cleanup=cleanup)
        thread.ready.connect(self._captions_transcribed)
        thread.failed.connect(self._captions_failed)
        thread.start()
        self._caption_thread = thread

    def _captions_transcribed(self, text, words):
        self.caption_transcribe.setEnabled(True)
        root, take = self._caption_job
        lines = text.strip()
        slider = getattr(self, 'slider', None)
        if slider is not None and slider.audio_is_edited():
            words = captions.source_clock(words, slider.audio_segments_seconds())
        try:
            captions.save_take(root, take, model=self.caption_model.currentData(), text=text, words=words,
                               lines=lines, captions=[])
        except OSError as exc:
            self.caption_status.setText('Cannot save the transcript: ' + str(exc)); return
        if self._caption_key != (str(root), take):
            return
        self.caption_lines.setPlainText(lines)
        self._show_captions(None)
        self.caption_status.setText('%d words. Split into one caption per line, then Get timings.'
                                    % len(captions.timed_words(words)))

    def _captions_failed(self, message):
        self.caption_transcribe.setEnabled(True)
        self.caption_status.setText(message or 'Transcription failed.')

    def align_captions(self):
        root, take = self._caption_take()
        words = captions.for_take(root, take).get('words') if take else None
        if not words:
            self.caption_status.setText('Transcribe the take first.'); return
        text = self.caption_lines.toPlainText()
        try:
            rows, changed = captions.align(words, text)
            captions.save_take(root, take, lines=text, captions=rows)
        except (OSError, ValueError) as exc:
            self.caption_status.setText(str(exc)); return
        self._show_captions(rows)
        note = (' %d corrected word%s took the timing of the transcript words they replace.'
                % (changed, '' if changed == 1 else 's')) if changed else ''
        self.caption_status.setText('%d captions.%s Edit text or times below.' % (len(rows), note))

    def copy_captions(self):
        root, take = self._caption_take()
        rows = captions.for_take(root, take).get('captions') if take else None
        if not rows:
            self.caption_status.setText('Get timings first.'); return
        QGuiApplication.clipboard().setText(captions.srt(rows))
        self.caption_status.setText('Copied %d captions as SRT.' % len(rows))

    def _choose_audio_take(self, row):
        if row < 0 or self._filling_audio:
            return
        item = self.session_audio_takes.item(row)
        if item is None:
            return
        file = item.data(Qt.UserRole)
        root = self.face_root()
        meta = read_meta(root) or {'version': 1}
        if str(meta.get('audio') or '').replace('\\', '/') == file:
            return
        meta['audio'] = file
        meta['audio_takes'] = audio_library(root, meta)
        recorded = next((take['file'] for take in meta['audio_takes'] if take['kind'] == 'original'), None)
        if recorded:
            meta['original_audio'] = recorded
        atomic_json(folder(root) / 'meta.json', meta)
        self.reload_face(force=True)
        self.session_voice_status.setText('Playback is using %s.' % item.data(Qt.UserRole + 1))

    def _voice_ready(self, voice_name, payload, suffix):
        self.session_generate.setEnabled(True)
        root = self.face_root()
        base = folder(root)
        label = (voice_name or 'Variation').strip() or 'Variation'
        dest = self._variant_destination(base, label)
        try:
            write_variation_wav(payload, suffix, self.session_format.currentData() or '', dest)
        except (OSError, RuntimeError, ValueError) as exc:
            self.session_voice_status.setText(str(exc))
            return
        rel = dest.relative_to(base).as_posix()
        meta = read_meta(root) or {'version': 1}
        takes = [take for take in audio_library(root, meta) if take['file'] != rel]
        takes.append({'label': label, 'kind': 'generated', 'file': rel})
        meta['audio_takes'] = takes
        recorded = next((take['file'] for take in takes if take['kind'] == 'original'), None)
        if recorded:
            meta['original_audio'] = recorded
        meta['audio'] = rel
        atomic_json(base / 'meta.json', meta)
        self.reload_face(force=True)
        target, self._voice_target = getattr(self, '_voice_target', None), None
        if target is not None:
            self.store_character_voice(*target)
        self.session_voice_status.setText('Playback is using %s. The original recording is still in the list.' % label)

    def _voice_failed(self, message):
        self._voice_target = None
        self.session_generate.setEnabled(True)
        self.session_voice_status.setText(message)

    def close_session(self):
        self.session_clock.stop()
        if hasattr(self, '_meter_clock'):
            self._meter_clock.stop()
        self._stop_level()
        self._stop_microphone()
        process = self._session_process
        self._session_process = None
        if process is not None:
            process.write(b'q\n')
            if not process.waitForFinished(2000):
                process.kill()
                process.waitForFinished(1000)

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
