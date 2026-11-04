"""Timeline-synced audio using the existing Qt multimedia add-on."""
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class FaceAudio:
    def __init__(self, parent=None):
        self.player = QMediaPlayer(parent)
        self.output = QAudioOutput(parent)
        self.player.setAudioOutput(self.output)
        self.path = None
        self.offset = 0.0
        self.duration = 0.0
        self.player.durationChanged.connect(self._duration)

    def _duration(self, ms):
        self.duration = max(0.0, ms/1000.0)

    def load(self, path, offset=0.0):
        self.offset = float(offset)
        path = str(path) if path else ''
        if path == self.path:
            return
        self.path = path
        if path:
            self.player.setSource(QUrl.fromLocalFile(path))
        else:
            self.player.stop()
            self.player.setSource(QUrl())
            self.duration = 0.0

    def sync(self, scene_time, playing, speed=1.0):
        if not self.path:
            self.player.pause()
            return
        t = float(scene_time) - self.offset
        if t < -0.05 or (self.duration and t > self.duration + 0.05):
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            return
        self.player.setPlaybackRate(max(0.25, min(4.0, float(speed))))
        position = max(0, int(t*1000))
        current = self.player.position()
        if abs(current - position) > 80:
            self.player.setPosition(position)
        if playing:
            if self.player.playbackState() != QMediaPlayer.PlayingState:
                self.player.setPosition(position)
                self.player.play()
        elif self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.player.setPosition(position)

    def stop(self):
        self.player.pause()
