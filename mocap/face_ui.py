"""Import, live-receive, and play face-mocap captures with project audio."""
import socket
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (QFileDialog, QMessageBox, QInputDialog, QFrame, QLabel,
    QPushButton, QVBoxLayout, QHBoxLayout, QProgressBar, QWidget, QLineEdit, QSizePolicy)
from .face_capture import (ARKIT_NAMES, activity_wave, audio_path, folder, import_audio,
    import_files, load_take, read_meta, save_recording, set_audio_edits, set_offset,
    weights_for, weights_from_sample, wave_from_wav)
from .face_curves import load_edit, save_edit
from .face_audio import FaceAudio
from .face_listen import DEFAULT_PORT, FaceListenServer

READOUT = (
    ('jawOpen', 'Jaw'),
    ('eyeBlinkLeft', 'Blink L'),
    ('eyeBlinkRight', 'Blink R'),
    ('mouthSmileLeft', 'Smile L'),
    ('mouthSmileRight', 'Smile R'),
    ('browInnerUp', 'Brow'),
    ('mouthFunnel', 'Funnel'),
    ('cheekPuff', 'Puff'),
)


class FaceLivePanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('faceLive')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.collapsed = False
        self.source = None
        self._shown_mode = None
        self._bars = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel('Face')
        self.title.setObjectName('faceLiveTitle')
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bar.addWidget(self.title, 1)
        self.live_button = QPushButton('Live')
        self.take_button = QPushButton('Take')
        self.live_button.setToolTip('Show the Quest blendshapes in this meter.')
        self.take_button.setToolTip('Show the recorded face capture at the timeline time.')
        self.live_button.clicked.connect(lambda: self._pick('live'))
        self.take_button.clicked.connect(lambda: self._pick('take'))
        for button in (self.live_button, self.take_button):
            button.setFixedWidth(46)
            bar.addWidget(button)
        self.toggle = QPushButton('–')
        self.toggle.setFixedWidth(22)
        self.toggle.setToolTip('Hide or show the live blendshape readout.')
        self.toggle.clicked.connect(self._toggle)
        bar.addWidget(self.toggle)
        layout.addLayout(bar)
        self.url = QLabel('')
        self.url.setObjectName('faceUrl')
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.url.setWordWrap(True)
        layout.addWidget(self.url)
        self.detail = QLabel('')
        self.detail.setObjectName('muted')
        layout.addWidget(self.detail)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 2, 0, 0)
        body.setSpacing(2)
        for key, label in READOUT:
            row = QHBoxLayout()
            name = QLabel(label)
            name.setFixedWidth(52)
            meter = QProgressBar()
            meter.setRange(0, 100)
            meter.setTextVisible(False)
            meter.setFixedHeight(10)
            value = QLabel('0.00')
            value.setFixedWidth(34)
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(name)
            row.addWidget(meter, 1)
            row.addWidget(value)
            body.addLayout(row)
            self._bars[key] = (meter, value)
        layout.addWidget(self.body)
        self.setFixedWidth(268)
        self.hide()

    def _pick(self, source):
        self.source = source
        self._shown_mode = None

    def set_mode(self, mode, live_enabled, take_enabled):
        self.live_button.setEnabled(live_enabled)
        self.take_button.setEnabled(take_enabled)
        if mode == self._shown_mode:
            return
        self._shown_mode = mode
        self._paint_button(self.live_button, mode == 'live')
        self._paint_button(self.take_button, mode == 'take')

    def _paint_button(self, button, active):
        name = 'primary' if active else ''
        if button.objectName() == name:
            return
        button.setObjectName(name)
        button.style().unpolish(button)
        button.style().polish(button)

    def _toggle(self):
        self.collapsed = not self.collapsed
        self.body.setVisible(not self.collapsed)
        self.url.setVisible(not self.collapsed)
        self.detail.setVisible(not self.collapsed)
        self.toggle.setText('+' if self.collapsed else '–')
        self.adjustSize()
        self.place()

    def show_take(self, sample, time_):
        self.title.setText('Face · recorded')
        self.url.clear()
        self.detail.setText('Timeline %.2f s' % float(time_))
        self._apply_sample(sample)
        self._reveal(False)

    def show_idle(self, title, detail):
        self.title.setText(title)
        self.url.clear()
        self.detail.setText(detail)
        self._clear_sample()
        self._reveal(False)

    def _apply_sample(self, sample):
        if sample is None:
            return
        for key, (meter, value) in self._bars.items():
            weight = float(sample[ARKIT_NAMES.index(key)]) if key in ARKIT_NAMES else 0.0
            meter.setValue(int(max(0.0, min(1.0, weight)) * 100))
            value.setText('%.2f' % weight)

    def _clear_sample(self):
        for meter, value in self._bars.values():
            meter.setValue(0)
            value.setText('0.00')

    def _reveal(self, show_url):
        self.body.setVisible(not self.collapsed)
        self.url.setVisible(show_url and not self.collapsed)
        self.detail.setVisible(not self.collapsed)
        self.adjustSize()
        self.show()
        self.raise_()
        self.place()

    def show_live(self, url, status, sample, retain=False):
        connected = bool(status and status.get('connected'))
        if not connected:
            if not retain:
                self.hide()
                return
            self.title.setText('Face · live')
            self.url.setText(url or '')
            self.detail.setText('Quest not connected')
            self._clear_sample()
            self._reveal(True)
            return
        fps = int(status.get('fps') or 0)
        frames = int(status.get('frames') or 0)
        client = status.get('client') or 'headset'
        self.title.setText('Face · %d fps' % fps if not self.collapsed else 'Face · %d fps · connected' % fps)
        self.url.setText(url)
        eyes = status.get('eyes') or {}
        gaze = ''
        if eyes:
            gaze = ' · eyes %s/%s' % (eyes.get('left', '–'), eyes.get('right', '–'))
        self.detail.setText('%s · %d frames%s' % (client, frames, gaze))
        self._apply_sample(sample)
        self._reveal(True)

    def place(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.move(max(8, parent.width() - self.width() - 12), 96)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self.place()
        return super().eventFilter(obj, event)


class FaceWorkspace:
    def init_face(self):
        self.face_take = None
        self.face_meta = None
        self.face_audio = FaceAudio(self)
        self.face_server = None
        self.face_live = None
        self.face_loaded_root = None
        self.face_port = DEFAULT_PORT
        self._face_retries = 0
        self.face_live_timer = QTimer(self)
        self.face_live_timer.setInterval(33)
        self.face_live_timer.timeout.connect(self._tick_face_live)
        self.import_face_action.triggered.connect(self.choose_face_capture)
        self.import_face_audio_action.triggered.connect(self.choose_face_audio)
        self.listen_face_action.triggered.connect(self.toggle_face_listen)
        self.save_face_listen_action.triggered.connect(self.save_face_listen)
        self.save_face_listen_action.setEnabled(False)
        self.face_offset_action.triggered.connect(self.edit_face_offset)
        if hasattr(self, 'slider'):
            self.slider.audio_edit_finished.connect(self.commit_audio_crop)
        self.copy_face_url.clicked.connect(self.copy_face_listen_url)
        self.face_url.mousePressEvent = self._select_face_url
        self.feed_mode = 'live'
        if hasattr(self, 'view_feed'):
            self.view_feed.currentIndexChanged.connect(self._feed_changed)
        self.face_panel = FaceLivePanel(self.motion_view)
        self.motion_view.installEventFilter(self.face_panel)
        if hasattr(self, 'face_graph'):
            self.face_graph.changed.connect(self._face_curves_changed)
            self.face_graph.edit_started.connect(self.stop_playback)
            self.face_graph.edit_finished.connect(self.commit_face_curves)
            self._sync_face_clock()
        self.reload_face()
        self.start_face_listen(DEFAULT_PORT)

    def face_root(self):
        if self.workspace_root:
            return Path(self.workspace_root)
        session = getattr(self, 'session_dir', None)
        if session:
            return Path(session)
        return Path(self.project or self.character_session)

    def capture_url(self):
        return 'https://%s:%s' % (self.lan_address(), self.face_port)

    def reload_face(self, force=False):
        root = self.face_root()
        if not force and self.face_loaded_root==root and (self.face_take is not None or self.face_meta is not None or not (folder(root)/'meta.json').is_file()):
            self.update_face_status();return
        self.face_loaded_root=root
        if force:
            self._audio_layout = None
        try:
            self.face_take, self.face_meta = load_take(root)
        except (OSError, ValueError) as exc:
            self.face_take = None
            self.face_meta = read_meta(root)
            self.status.setText('Cannot load face capture: '+str(exc))
        if self.face_take is not None:
            self.face_take.curves = load_edit(root)
        audio = audio_path(root, self.face_meta)
        self.face_audio.load(audio, float((self.face_meta or {}).get('offset', 0)))
        self._show_face_graph()
        self.update_face_status()

    def update_face_status(self):
        listening = self.face_server is not None
        self.listen_face_action.setText('Stop Face Listen' if listening else 'Start Face Listen')
        status = self.face_server.hub.status() if listening else None
        self.save_face_listen_action.setEnabled(bool(status and (status['frames'] or status['latest'])))
        if hasattr(self, 'import_face_action'):
            busy = getattr(self, 'busy', False)
            self.import_face_action.setEnabled(not busy)
            self.import_face_audio_action.setEnabled(not busy)
            self.face_offset_action.setEnabled(not busy and (self.face_take is not None or audio_path(self.face_root()) is not None))
        if hasattr(self, 'slider'):
            origin=float((self.face_meta or {}).get('offset',0))-float(getattr(self,'source_offset',0) or 0)
            take=self.face_take
            if take is not None and len(take.times):
                start,end=float(take.times[0]),float(take.times[-1])
                self.slider.face_span=(origin+start,max(end-start,1/30))
                self.slider.face_wave=activity_wave(take.times,take.activity_peaks())
            else:
                self.slider.face_span=None
                self.slider.face_wave=None
            self.slider.face_empty='No face capture'
            self.slider.face_revision+=1
            if not self._recording_now():
                self._layout_timeline_audio(origin)
            self.slider.update()
        self._refresh_face_url(status)
        if hasattr(self, '_fill_audio_takes'):
            self._fill_audio_takes()

    def _layout_timeline_audio(self, origin):
        path = self.face_audio.path if self.face_audio.duration else None
        slider = self.slider
        if slider.maximum() <= 0:
            if path is None and slider.audio_span:
                slider.set_audio_span(None, None, restore=True)
                self._audio_layout = None
                self.apply_audio_playback()
            return
        layout = (
            None if path is None else str(path),
            slider.minimum(), slider.maximum(), float(slider.fps or 30.0),
            round(float(origin), 6),
            None if path is None else round(float(self.face_audio.duration), 6))
        restore = layout != getattr(self, '_audio_layout', None)
        dragging = slider._audio_gesture is not None or bool(slider._trim_drag and slider._trim_drag[0] == 'audio')
        if path:
            meta = self.face_meta or {}
            edits = self._audio_edits_for(path, meta) if restore else None
            crop = None
            if restore and not edits and not isinstance(meta.get('audio_edits_by_file'), dict):
                saved = meta.get('audio_crop')
                if isinstance(saved, (list, tuple)) and len(saved) == 2:
                    try:
                        crop = (float(saved[0]), float(saved[1]))
                    except (TypeError, ValueError):
                        crop = None
            slider.set_audio_span((origin, self.face_audio.duration), wave_from_wav(path), crop, restore=restore, edits=edits)
        else:
            slider.set_audio_span(None, None, restore=restore)
        if not dragging:
            self._audio_layout = layout
        self.apply_audio_playback()

    def _refresh_face_url(self, status=None):
        if not hasattr(self, 'face_url'):
            return
        if self.face_server is None:
            if self.face_url.text() != 'not listening':
                self.face_url.setText('not listening')
            if hasattr(self, 'face_live_badge'):
                self.face_live_badge.setText('')
            self.copy_face_url.setEnabled(False)
            return
        url = self.capture_url()
        if self.face_url.text() != url:
            self.face_url.setText(url)
        if hasattr(self, 'session_link'):
            camera = self.capture_url()
            if self.session_link.text() != camera:
                self.session_link.setText(camera)
        if hasattr(self, 'face_live_badge'):
            self.face_live_badge.setText('live' if status and status.get('connected') else 'waiting')
        self.copy_face_url.setEnabled(True)

    def _select_face_url(self, event):
        QLineEdit.mousePressEvent(self.face_url, event)
        self.face_url.selectAll()

    def choose_face_capture(self):
        name, _ = QFileDialog.getOpenFileName(self, 'Import Face Capture', '', 'Face capture (*.json)')
        if not name:
            return
        try:
            import_files(self.face_root(), name)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Import Face Capture', str(exc)); return
        self.reload_face(force=True)
        extra = ' Audio was copied beside it.' if audio_path(self.face_root()) else ''
        self.status.setText('Face capture imported.'+extra+' Reimport a VRM to apply expressions in the viewport.')

    def choose_face_audio(self):
        name, _ = QFileDialog.getOpenFileName(self, 'Import Face Audio', '', 'Audio (*.wav *.mp3 *.m4a *.flac *.ogg)')
        if not name:
            return
        try:
            import_audio(self.face_root(), name)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Import Face Audio', str(exc)); return
        self.reload_face(force=True)
        self.status.setText('Audio imported. Play the timeline to hear it with the motion.')

    def edit_face_offset(self):
        current = float((self.face_meta or {}).get('offset', 0))
        value, ok = QInputDialog.getDouble(self, 'Face / Audio Offset',
            'Seconds to add to the video clock before sampling face and audio (negative starts them later):',
            current, -600.0, 600.0, 3)
        if not ok:
            return
        set_offset(self.face_root(), value)
        self.reload_face(force=True)

    def start_face_listen(self, port=DEFAULT_PORT):
        if self.face_server is not None:
            self.face_live_timer.stop()
            self.face_server.close(); self.face_server = None
        self.face_port = int(port)
        self.face_server = FaceListenServer(self.face_port, self)
        self.face_server.changed.connect(self._face_listen_ready)
        self.face_server.failed.connect(self._face_listen_failed)
        self.face_server.start()
        self.face_live_timer.start()
        self.update_face_status()

    def toggle_face_listen(self):
        if self.face_server is not None:
            self.face_live_timer.stop()
            self.face_server.close(); self.face_server = None
            if hasattr(self, 'face_panel'):
                self.face_panel.hide()
            self.update_face_status()
            self.status.setText('Stopped listening for face capture.')
            return
        self._face_retries = 0
        self.start_face_listen(DEFAULT_PORT)

    def _face_listen_ready(self, info):
        self.face_port = int(info.get('port', self.face_port))
        self._face_retries = 0
        self.status.setText('Listening for RGB Face and RGB Capture at '+self.capture_url())
        self.update_face_status()

    def _face_listen_failed(self, message):
        self.face_live_timer.stop()
        if self.face_server is not None:
            self.face_server.close(); self.face_server = None
        if self._face_retries < 4:
            self._face_retries += 1
            self.start_face_listen(DEFAULT_PORT + self._face_retries)
            return
        if hasattr(self, 'face_url'):
            self.face_url.setText('Quest listen failed: '+message)
            self.copy_face_url.setEnabled(False)
        self.update_face_status()
        self.status.setText('Face listen failed: '+message)

    def _recording_now(self):
        return hasattr(self, '_session_active') and self._session_active()

    def _recorded_face_sample(self, scene_time):
        if self.face_take is None or self._recording_now():
            return None
        return self.face_take.sample(scene_time)

    def _tick_face_live(self):
        status = self.face_server.hub.status() if self.face_server is not None else None
        if status is not None:
            self.save_face_listen_action.setEnabled(bool(status['frames'] or status['latest']))
            self._refresh_face_url(status)
        scene_time = getattr(self.motion_view, 'scene_time', 0.0) if hasattr(self, 'motion_view') else 0.0
        recorded = self._recorded_face_sample(scene_time)
        mode = self._readout_mode(recorded, status)
        has_take = self.face_take is not None and not self._recording_now()
        self.face_panel.set_mode(mode, self.face_server is not None, has_take)
        if mode == 'take':
            if recorded is not None:
                self.face_panel.show_take(recorded, scene_time)
            else:
                self.face_panel.show_idle('Face · recorded', 'No recorded face')
            self._show_phone_preview()
        elif status is not None:
            retain = has_take or self.face_panel.source == 'live'
            self.face_panel.show_live(self.capture_url(), status, self._live_sample(), retain=retain)
            self._show_phone_preview()
        if hasattr(self, 'motion_view'):
            self.motion_view.update()

    def _readout_mode(self, recorded, status=None):
        if self._recording_now():
            return 'live'
        choice = getattr(self, 'face_panel', None)
        choice = choice.source if choice is not None else None
        if choice == 'live':
            return 'live'
        if choice == 'take' and self.face_take is not None:
            return 'take'
        if status is None and self.face_server is not None:
            status = self.face_server.hub.status()
        if status and status.get('connected'):
            return 'live'
        return 'take' if recorded is not None else 'live'

    def _live_sample(self):
        if self.face_server is None:
            return None
        live = self.face_server.hub.latest_values()
        if live is None:
            return None
        import numpy as np
        sample = np.zeros(len(ARKIT_NAMES), np.float32)
        n = min(len(ARKIT_NAMES), max(0, len(live) - 1))
        if n:
            sample[:n] = np.asarray(live[1:1 + n], np.float32)
        return sample

    def _select_feed(self, mode):
        self.feed_mode = mode
        if hasattr(self, 'view_feed'):
            self.view_feed.blockSignals(True)
            index = self.view_feed.findData(mode)
            if index >= 0:
                self.view_feed.setCurrentIndex(index)
            self.view_feed.blockSignals(False)
        self._apply_feed(mode)

    def _feed_changed(self):
        self._apply_feed(self.view_feed.currentData() or 'live')

    def _apply_feed(self, mode):
        self.feed_mode = mode or 'live'
        if not hasattr(self, 'video_view'):
            return
        if self.feed_mode == 'live':
            self.video_view.live = True
            if hasattr(self, 'slider'):
                self.present_frame(self.slider.value())
            return
        self.video_view.live = False
        if getattr(self, 'video_path', None) and self.video_view.decoder is not None:
            if self.video_view.logical_size:
                self.video_view.source_size = self.video_view.logical_size
            elif self.info and self.info.get('width') and self.info.get('height'):
                self.video_view.source_size = (int(self.info['width']), int(self.info['height']))
            index = self.slider.value() if hasattr(self, 'slider') else 0
            self.video_view.seek(int(index))
        else:
            self.status.setText('No take is open. Record one, then choose Take.')

    def _show_phone_preview(self):
        if getattr(self, 'feed_mode', 'live') != 'live':
            return
        if not hasattr(self, 'video_view'):
            return
        preview = self.face_server.hub.preview_jpeg() if self.face_server is not None else None
        if not preview:
            return
        generation, data = preview
        if generation == getattr(self, '_preview_seen', None):
            return
        image = QImage()
        if not image.loadFromData(data, 'JPEG'):
            return
        self._preview_seen = generation
        self.video_view.show_live(image)

    def copy_face_listen_url(self):
        url = self.capture_url()
        QGuiApplication.clipboard().setText(url)
        self.status.setText('Copied the capture address: '+url)

    def save_face_listen(self):
        if self.face_server is None:
            return
        frames = self.face_server.hub.stop()
        names = self.face_server.hub.expression_list()
        if not frames:
            QMessageBox.information(self, 'Save Face Capture', 'No frames were received yet.'); return
        try:
            save_recording(self.face_root(), frames, names=names)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Save Face Capture', str(exc)); return
        self.face_server.hub.start()
        self.reload_face(force=True)
        self.status.setText('Saved %d live face frames into the project.' % len(frames))

    def lan_address(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(('1.1.1.1', 80))
            address = sock.getsockname()[0]
            sock.close()
            return address
        except OSError:
            return '127.0.0.1'

    def face_look(self):
        if self.face_server is None:
            return (0., 0., 0., 0.)
        status = self.face_server.hub.status()
        recorded = self._recorded_face_sample(0.0)
        if self._readout_mode(recorded, status) != 'live':
            return (0., 0., 0., 0.)
        live = self.face_server.hub.latest_values() if status.get('connected') else None
        if not live:
            return (0., 0., 0., 0.)
        def weight(name):
            index = ARKIT_NAMES.index(name)
            return float(live[1+index]) if 1+index < len(live) else 0.
        up = (weight('eyeLookUpLeft')+weight('eyeLookUpRight'))*.5
        down = (weight('eyeLookDownLeft')+weight('eyeLookDownRight'))*.5
        left = (weight('eyeLookOutLeft')+weight('eyeLookInRight'))*.5
        right = (weight('eyeLookInLeft')+weight('eyeLookOutRight'))*.5
        return (up, down, left, right)

    def _sync_face_clock(self):
        graph = getattr(self, 'face_graph', None)
        if graph is None:
            return
        def record_time(frame, window=self):
            fps = float(window.slider.fps or 30) or 30.0
            source = float(getattr(window, 'source_offset', 0) or 0)
            offset = window.face_take.offset if window.face_take is not None else 0.0
            return source + float(frame) / fps - offset
        def frame_of(t, window=self):
            fps = float(window.slider.fps or 30) or 30.0
            source = float(getattr(window, 'source_offset', 0) or 0)
            offset = window.face_take.offset if window.face_take is not None else 0.0
            return (float(t) + offset - source) * fps
        graph.record_time = record_time
        graph.frame_of = frame_of

    def _show_face_graph(self):
        graph = getattr(self, 'face_graph', None)
        if graph is None:
            return
        self._sync_face_clock()
        graph.set_take(self.face_take)

    def _face_curves_changed(self):
        if hasattr(self, 'motion_view'):
            self.motion_view.update()

    def commit_face_curves(self):
        if self.face_take is None or self.face_take.curves is None:
            return
        try:
            save_edit(self.face_root(), self.face_take.curves)
        except OSError as exc:
            self.status.setText('Cannot save face curves: ' + str(exc))
            return
        self._refresh_face_wave()

    def _refresh_face_wave(self):
        take = self.face_take
        slider = getattr(self, 'slider', None)
        if slider is None or take is None or not len(take.times):
            return
        slider.face_wave = activity_wave(take.times, take.activity_peaks())
        slider.face_revision += 1
        slider.update()

    def face_weights(self, names, scene_time):
        recorded = self._recorded_face_sample(scene_time)
        status = self.face_server.hub.status() if self.face_server is not None else None
        if self._readout_mode(recorded, status) == 'live' and status and status.get('connected'):
            sample = self._live_sample()
            if sample is not None:
                return weights_from_sample(sample, names)
        if recorded is not None:
            return weights_from_sample(recorded, names)
        if self.face_take is None:
            return None
        return weights_for(self.face_take, names, scene_time)

    def reset_timeline_trim(self):
        self.slider.reset_trim()
        self.slider.reset_audio_trim()
        self.commit_audio_crop()

    def _audio_edit_name(self, path):
        root = folder(self.face_root()).resolve()
        try:
            return Path(path).resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            return Path(path).name

    def _audio_edits_for(self, path, meta):
        name = self._audio_edit_name(path)
        book = meta.get('audio_edits_by_file')
        if isinstance(book, dict):
            found = book.get(name)
            return found if isinstance(found, list) else None
        legacy = meta.get('audio_edits')
        return legacy if isinstance(legacy, list) else None

    def commit_audio_crop(self):
        if not hasattr(self, 'slider'):
            return
        root = self.face_root()
        meta = read_meta(root)
        if meta is None:
            self.apply_audio_playback()
            return
        payload = self.slider.audio_edit_payload()
        audio = self.face_audio.path if getattr(self, 'face_audio', None) is not None else None
        current = self._audio_edits_for(audio, meta) if audio else None
        leftover = meta.get('audio_crop')
        if payload == current and not (payload is None and leftover):
            self.apply_audio_playback()
            return
        written = set_audio_edits(root, payload)
        if self.face_meta is not None and written is not None:
            for key in ('audio_crop', 'audio_edits', 'audio_edits_by_file'):
                if key in written:
                    self.face_meta[key] = written[key]
                else:
                    self.face_meta.pop(key, None)
        self.apply_audio_playback()

    def apply_audio_playback(self):
        audio = getattr(self, 'face_audio', None)
        slider = getattr(self, 'slider', None)
        if audio is None or slider is None or audio.samples is None:
            return
        signature = tuple(slider.audio_segments) if slider.audio_is_edited() else None
        if signature == getattr(self, '_audio_play_sig', None) and (signature is None) == (audio.arranged is None):
            return
        self._audio_play_sig = signature
        if signature is None:
            audio.clear_arrangement()
            return
        from .face_audio import compose_arrangement
        arranged, duration, start = compose_arrangement(audio.samples, audio.rate, slider.audio_segments_seconds())
        audio.set_arrangement(arranged, duration, start)

    def arranged_audio(self):
        """Wav of the edited track, plus a temp directory to delete. The original file is unchanged."""
        path = self._playback_audio() if hasattr(self, '_playback_audio') else None
        slider = getattr(self, 'slider', None)
        audio = getattr(self, 'face_audio', None)
        if path is None or slider is None or not slider.audio_is_edited() or audio is None or not audio.rate:
            return path, None
        from .face_audio import _wav_mono, compose_arrangement, write_float_wav
        import tempfile
        import wave
        try:
            samples, rate, _duration = _wav_mono(path)
        except (OSError, ValueError, wave.Error):
            return path, None
        if samples is None:
            return path, None
        pieces = slider.audio_segments_seconds()
        if not pieces:
            import numpy as np
            arranged = np.zeros(max(1, int(round(_duration * rate))), np.float32)
            start = 0.0
        else:
            arranged, _length, start = compose_arrangement(samples, rate, pieces)
        import numpy as np
        if start > 0.001:
            pad = int(round(start * rate))
            full = np.zeros(pad + len(arranged), np.float32)
            full[pad:pad + len(arranged)] = arranged
            arranged = full
        elif start < -0.001:
            skip = min(len(arranged), int(round(-start * rate)))
            arranged = arranged[skip:]
        folder = Path(tempfile.mkdtemp(prefix='rgb-audio-edit-'))
        dest = folder / 'arranged.wav'
        write_float_wav(dest, arranged, rate)
        return dest, folder

    def sync_face_audio(self):
        playing = hasattr(self, 'timer') and self.timer.isActive()
        speed = self.play_speed.currentData() if hasattr(self, 'play_speed') else 1.0
        time_ = getattr(self.motion_view, 'scene_time', 0.0)
        self.face_audio.sync(time_, playing, speed or 1.0)

    def close_face(self):
        self.face_live_timer.stop()
        self.face_audio.stop()
        if self.face_server is not None:
            self.face_server.close(); self.face_server = None
