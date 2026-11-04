"""Import, live-receive, and play face-mocap captures with project audio."""
import socket
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (QFileDialog, QMessageBox, QInputDialog, QFrame, QLabel,
    QPushButton, QVBoxLayout, QHBoxLayout, QProgressBar, QWidget, QLineEdit)
from .face_capture import (ARKIT_NAMES, audio_path, folder, import_audio,
    import_files, load_take, read_meta, save_recording, set_offset,
    weights_for, weights_from_sample)
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
        self._bars = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel('Face')
        self.title.setObjectName('faceLiveTitle')
        bar.addWidget(self.title, 1)
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
        self.setFixedWidth(248)
        self.hide()

    def _toggle(self):
        self.collapsed = not self.collapsed
        self.body.setVisible(not self.collapsed)
        self.url.setVisible(not self.collapsed)
        self.detail.setVisible(not self.collapsed)
        self.toggle.setText('+' if self.collapsed else '–')
        self.adjustSize()
        self.place()

    def show_live(self, url, status, sample):
        connected = bool(status and status.get('connected'))
        if not connected:
            self.hide()
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
        if sample is not None:
            for key, (meter, value) in self._bars.items():
                weight = float(sample[ARKIT_NAMES.index(key)]) if key in ARKIT_NAMES else 0.0
                meter.setValue(int(max(0.0, min(1.0, weight)) * 100))
                value.setText('%.2f' % weight)
        self.body.setVisible(not self.collapsed)
        self.url.setVisible(not self.collapsed)
        self.detail.setVisible(not self.collapsed)
        self.adjustSize()
        self.show()
        self.raise_()
        self.place()

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
        self.face_audio.player.durationChanged.connect(lambda *_: self.update_face_status())
        self.import_face_action.triggered.connect(self.choose_face_capture)
        self.import_face_audio_action.triggered.connect(self.choose_face_audio)
        self.listen_face_action.triggered.connect(self.toggle_face_listen)
        self.save_face_listen_action.triggered.connect(self.save_face_listen)
        self.save_face_listen_action.setEnabled(False)
        self.face_offset_action.triggered.connect(self.edit_face_offset)
        self.copy_face_url.clicked.connect(self.copy_face_listen_url)
        self.face_url.mousePressEvent = self._select_face_url
        self.face_panel = FaceLivePanel(self.motion_view)
        self.motion_view.installEventFilter(self.face_panel)
        self.reload_face()
        self.start_face_listen(DEFAULT_PORT)

    def face_root(self):
        if self.workspace_root:
            return Path(self.workspace_root)
        session = getattr(self, 'session_dir', None)
        if session:
            return Path(session)
        return Path(self.project or self.character_session)

    def face_listen_url(self):
        return 'https://%s:%s/frame' % (self.lan_address(), self.face_port)

    def reload_face(self, force=False):
        root = self.face_root()
        if not force and self.face_loaded_root==root and (self.face_take is not None or self.face_meta is not None or not (folder(root)/'meta.json').is_file()):
            self.update_face_status();return
        self.face_loaded_root=root
        try:
            self.face_take, self.face_meta = load_take(root)
        except (OSError, ValueError) as exc:
            self.face_take = None
            self.face_meta = read_meta(root)
            self.status.setText('Cannot load face capture: '+str(exc))
        audio = audio_path(root, self.face_meta)
        self.face_audio.load(audio, float((self.face_meta or {}).get('offset', 0)))
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
            self.slider.face_span=(origin,self.face_take.duration) if self.face_take is not None else None
            self.slider.audio_span=(origin,self.face_audio.duration) if self.face_audio.path else None
            self.slider.update()
        self._refresh_face_url(status)

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
        url = self.face_listen_url()
        if self.face_url.text() != url:
            self.face_url.setText(url)
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
        url = self.face_listen_url()
        self.status.setText('Face Mocap on the Quest connects on its own. Browser fallback: '+url)
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

    def _tick_face_live(self):
        status = self.face_server.hub.status() if self.face_server is not None else None
        if status is not None:
            self.save_face_listen_action.setEnabled(bool(status['frames'] or status['latest']))
            self._refresh_face_url(status)
            sample = None
            live = self.face_server.hub.latest_values()
            if live is not None:
                import numpy as np
                sample = np.zeros(len(ARKIT_NAMES), np.float32)
                n = min(len(ARKIT_NAMES), max(0, len(live)-1))
                if n:
                    sample[:n] = np.asarray(live[1:1+n], np.float32)
            self.face_panel.show_live(self.face_listen_url(), status, sample)
            self._show_phone_preview()
        if hasattr(self, 'motion_view'):
            self.motion_view.update()

    def _show_phone_preview(self):
        if not hasattr(self, 'video_view') or (hasattr(self, 'timer') and self.timer.isActive()):
            return
        recording = self._session_active() if hasattr(self, '_session_active') else False
        if getattr(self, 'video_path', None) and not recording:
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
        url = self.face_listen_url()
        QGuiApplication.clipboard().setText(url)
        self.status.setText('Copied Quest URL: '+url)

    def save_face_listen(self):
        if self.face_server is None:
            return
        frames = self.face_server.hub.stop()
        if not frames:
            QMessageBox.information(self, 'Save Face Capture', 'No frames were received yet.'); return
        try:
            save_recording(self.face_root(), frames)
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

    def face_weights(self, names, scene_time):
        if self.face_server is not None:
            status = self.face_server.hub.status()
            live = self.face_server.hub.latest_values() if status.get('connected') else None
            if live is not None:
                import numpy as np
                sample = np.zeros(len(ARKIT_NAMES), np.float32)
                n = min(len(ARKIT_NAMES), max(0, len(live)-1))
                if n:
                    sample[:n] = np.asarray(live[1:1+n], np.float32)
                return weights_from_sample(sample, names)
        if self.face_take is None:
            return None
        return weights_for(self.face_take, names, scene_time)

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
