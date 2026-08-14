"""Cienka warstwa nad QMediaPlayer do szybkiego odtwarzania nagrań."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer


class AudioPlayer(QObject):
    started = pyqtSignal(Path)
    stateChanged = pyqtSignal(QMediaPlayer.PlaybackState)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._output = QAudioOutput(self)
        self._output.setVolume(0.9)
        self._player.setAudioOutput(self._output)
        self._current_path: Path | None = None

        self._player.playbackStateChanged.connect(self.stateChanged.emit)

    # -- kontrola odtwarzania -------------------------------------------------
    def play_file(self, path: Path) -> None:
        self._current_path = path
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()
        self.started.emit(path)

    def toggle_pause(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        elif self._current_path is not None:
            self._player.play()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(position_ms)

    def set_volume(self, volume_0_1: float) -> None:
        self._output.setVolume(volume_0_1)

    @property
    def current_path(self) -> Path | None:
        return self._current_path

    @property
    def qt_player(self) -> QMediaPlayer:
        return self._player

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
