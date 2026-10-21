"""Small authenticated HTTP service for a headless SmartScanATC receiver.

The server owns the SDR and recordings.  A desktop client sends scan settings,
controls the scanner and downloads completed audio files for local playback and
transcription.  It deliberately uses only Python's standard library for the
network boundary so the receiver does not need a second web stack.
"""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import queue
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import numpy as np
from PySide6.QtCore import QCoreApplication, QObject

from . import storage
from .config import CONFIG_PATH, AppConfig, atomic_write_text
from .models import BlacklistEntry, Channel
from .scanner_engine import AUDIO_RATE, ScannerEngine

# Live audio is streamed as raw mono 16-bit PCM at this rate (see
# scanner_engine.AUDIO_RATE) - fixed and hardcoded on both ends rather than
# negotiated, since there's exactly one producer and the format never
# changes. _AUDIO_QUEUE_SIZE bounds the per-subscriber backlog so a slow or
# stalled desktop client can never make the engine's own real-time thread
# block (see ReceiverController._on_live_audio) - old chunks are dropped,
# not queued forever, same "never let a consumer stall the producer"
# principle as AcquisitionThread's bounded queue.
_AUDIO_QUEUE_SIZE = 50
_AUDIO_SUFFIXES = {".wav", ".mp3"}


class ReceiverController(QObject):
    """Owns the one scanner instance and exposes thread-safe status snapshots."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._engine: ScannerEngine | None = None
        self._state = "IDLE"
        self._level_dbfs = -120.0
        self._frequencies: list[tuple[int, str]] = []
        self._recording = False
        self._recording_path = ""
        self._last_log = "Ready."
        self._cycle_time: float | None = None
        self._spectrum: dict[str, Any] | None = None
        self._audio_lock = threading.Lock()
        self._audio_subscribers: list[queue.Queue] = []
        # Only files known to be closed are ever listed/served for sync. At
        # startup nothing is recording yet, so everything already on disk
        # qualifies; after that, a file joins only via recording_finished
        # (after its metadata chunk and any mp3 transcode). Excluding just
        # the primary slot's path used to expose other simultaneous slots,
        # manual recordings and half-written mp3s - which delete_synced
        # then deleted mid-recording.
        root = Path(self.config.recording.output_dir).resolve()
        self._closed: set[Path] = {
            p.resolve() for p in root.glob("**/*") if p.is_file() and p.suffix.lower() in _AUDIO_SUFFIXES
        } if root.is_dir() else set()

    @property
    def config(self) -> AppConfig:
        return AppConfig.load()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._engine is not None,
                "state": self._state,
                "level_dbfs": self._level_dbfs,
                "frequencies": self._frequencies,
                "recording": self._recording,
                "recording_path": self._recording_path,
                "log": self._last_log,
                "cycle_time": self._cycle_time,
                "spectrum": self._spectrum,
            }

    def start(self, payload: dict[str, Any]) -> None:
        with self._lock:
            existing = self._engine
        if existing is not None and existing._stop_evt.is_set():
            # Start right after Stop: the old engine is still shutting down.
            # Reusing it would "apply" this Start to an engine about to exit.
            existing.join(timeout=5.0)
            if existing.is_alive():
                raise RuntimeError("Scanner is still stopping - try again")
            existing = None
        raw_config = payload.get("config")
        if isinstance(raw_config, dict):
            # Files remain on the receiver even when the desktop's local
            # recording folder is part of the submitted settings.
            current = self.config
            recording = dict(raw_config.get("recording", {}))
            recording["output_dir"] = current.recording.output_dir
            raw_config = dict(raw_config)
            raw_config["recording"] = recording
            # The receiver's own SDR (driver/serial) and remote settings,
            # never the desktop's - those only broke on the next reboot.
            raw_config["device"] = asdict(current.device)
            raw_config["remote_receiver"] = asdict(current.remote_receiver)
            raw_config["debug_mode"] = False
            atomic_write_text(CONFIG_PATH, json.dumps(raw_config, indent=2, ensure_ascii=False))
        config = self.config
        supplied_channels = payload.get("channels")
        channels = []
        for item in supplied_channels if isinstance(supplied_channels, list) else []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            modulation = row.get("modulation")
            if hasattr(modulation, "value"):
                row["modulation"] = modulation.value
            channels.append(Channel.from_row([
                str(row.get("freq_hz", 0)), str(row.get("name", "")), str(row.get("modulation", "AM")),
                "1" if row.get("enabled", True) else "0", str(row.get("folder", "")), str(row.get("weight", 0)),
            ]))
        if supplied_channels is None:
            channels = storage.load_channels()
        supplied_blacklist = payload.get("blacklist")
        blacklist = [BlacklistEntry(**item) for item in supplied_blacklist if isinstance(item, dict)] if isinstance(supplied_blacklist, list) else storage.load_blacklist()
        if supplied_channels is not None:
            storage.save_channels(channels)
        if supplied_blacklist is not None:
            storage.save_blacklist(blacklist)
        if existing is not None:
            # The receiver normally starts on boot.  A desktop Start then
            # applies its current band/channel edits without a needless SDR
            # reopen; device and capture-width controls are already locked
            # while the scanner is running.
            existing.config = config
            existing.update_channels(channels)
            existing.update_blacklist(blacklist)
            existing.request_rebuild()
            return
        engine = ScannerEngine(config, channels, blacklist)
        engine.signals.state_changed.connect(self._on_state)
        engine.signals.level_changed.connect(self._on_level)
        engine.signals.freq_changed.connect(self._on_frequencies)
        engine.signals.recording_changed.connect(self._on_recording)
        engine.signals.spectrum_ready.connect(self._on_spectrum)
        engine.signals.live_audio_ready.connect(self._on_live_audio)
        engine.signals.cycle_time_changed.connect(self._on_cycle_time)
        engine.signals.blacklisted.connect(self._on_blacklisted)
        engine.signals.log.connect(self._on_log)
        engine.signals.recording_finished.connect(self._on_recording_finished)
        engine.signals.finished.connect(self._on_finished)
        with self._lock:
            self._engine = engine
            self._state = "CALCULATE_NEXT_FREQ"
        engine.start()

    def stop(self) -> None:
        with self._lock:
            engine = self._engine
        if engine is not None:
            engine.request_stop()

    def command(self, name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            engine = self._engine
        if engine is None:
            raise RuntimeError("Scanner is not running")
        if name == "skip":
            engine.request_skip()
        elif name == "blacklist":
            engine.request_skip_and_blacklist()
        elif name == "mute":
            engine.set_live_muted(bool(payload.get("muted")))
        elif name == "manual_record":
            (engine.start_manual_recording if payload.get("enabled") else engine.stop_manual_recording)()
        elif name == "auto_recording":
            engine.config.recording.enabled = bool(payload.get("enabled"))
            engine.config.save()
        else:
            raise ValueError("Unknown command")

    def recordings(self) -> list[dict[str, Any]]:
        root = Path(self.config.recording.output_dir).resolve()
        with self._lock:
            closed = sorted(self._closed)
        result = []
        for path in closed:
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if root in path.parents:
                result.append({"path": path.relative_to(root).as_posix(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        return result

    def recording_path(self, relative_path: str) -> Path:
        root = Path(self.config.recording.output_dir).resolve()
        path = (root / relative_path).resolve()
        with self._lock:
            closed = path in self._closed
        if root not in path.parents or path.suffix.lower() not in _AUDIO_SUFFIXES or not closed or not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def delete_recording(self, relative_path: str) -> None:
        # Reuses recording_path()'s validation (within output_dir, real
        # .wav/.mp3 file) - the desktop client only ever calls this after
        # its own download of this exact file has already completed and
        # been renamed into place, so there's no window where this could
        # race a still-in-progress transfer of the same file.
        path = self.recording_path(relative_path)
        path.unlink()
        with self._lock:
            self._closed.discard(path)

    def _on_state(self, value: str) -> None:
        with self._lock:
            self._state = value

    def _on_level(self, value: float) -> None:
        with self._lock:
            self._level_dbfs = value

    def _on_frequencies(self, values: object) -> None:
        with self._lock:
            self._frequencies = [(int(freq), str(name)) for freq, name in values]

    def _on_cycle_time(self, seconds: float) -> None:
        with self._lock:
            self._cycle_time = float(seconds)

    def _on_spectrum(self, freqs: Any, db: Any, trigger_dbfs: float, custom: bool) -> None:
        # Already throttled to 10 fps at the source (ScannerEngine's own
        # _submit_spectrum) - just plain JSON lists, no need for a binary
        # encoding at 512 points per frame.
        with self._lock:
            self._spectrum = {
                "freqs": [float(x) for x in freqs],
                "db": [float(x) for x in db],
                "trigger_dbfs": float(trigger_dbfs),
                "custom": bool(custom),
            }

    def _on_recording(self, active: bool, path: str) -> None:
        with self._lock:
            self._recording, self._recording_path = active, path

    def _on_blacklisted(self, _freq_hz: int) -> None:
        with self._lock:
            engine = self._engine
        if engine is not None:
            storage.save_blacklist(engine.blacklist)

    def _on_log(self, message: str) -> None:
        with self._lock:
            self._last_log = message

    def _on_recording_finished(self, path: str) -> None:
        with self._lock:
            self._closed.add(Path(path).resolve())

    def _on_finished(self) -> None:
        with self._lock:
            if self._engine is None or self.sender() is not self._engine.signals:
                return  # an older engine finishing after a newer one already started
            self._engine = None
            self._state = "IDLE"
            self._recording = False
            self._spectrum = None

    def _on_live_audio(self, mono: Any) -> None:
        pcm16 = (np.clip(np.asarray(mono, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with self._audio_lock:
            for q in self._audio_subscribers:
                try:
                    q.put_nowait(pcm16)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(pcm16)
                    except queue.Full:
                        pass

    def subscribe_audio(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_AUDIO_QUEUE_SIZE)
        with self._audio_lock:
            self._audio_subscribers.append(q)
        return q

    def unsubscribe_audio(self, q: queue.Queue) -> None:
        with self._audio_lock:
            if q in self._audio_subscribers:
                self._audio_subscribers.remove(q)


def _handler(controller: ReceiverController, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SmartScanATCReceiver/1"

        def _authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

        def _json(self, status: HTTPStatus, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("JSON object required")
            return value

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return False

        def do_GET(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            path = urlparse(self.path).path
            try:
                if path == "/api/status":
                    self._json(HTTPStatus.OK, controller.status())
                elif path == "/api/recordings":
                    self._json(HTTPStatus.OK, controller.recordings())
                elif path.startswith("/api/recordings/"):
                    target = controller.recording_path(unquote(path.removeprefix("/api/recordings/")))
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(target.stat().st_size))
                    self.end_headers()
                    with target.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            self.wfile.write(chunk)
                elif path == "/api/audio":
                    # Stays open for as long as the client listens - raw
                    # mono 16-bit PCM at AUDIO_RATE, no framing/Content-
                    # Length (the connection itself is the delimiter).
                    # Silence (muted, or no primary slot open yet) just
                    # means no chunks for a while, not a reason to
                    # disconnect - only a real transport error ends this.
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.close_connection = True
                    self.end_headers()
                    q = controller.subscribe_audio()
                    try:
                        while True:
                            try:
                                chunk = q.get(timeout=5.0)
                            except queue.Empty:
                                continue
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        controller.unsubscribe_audio(q)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Recording not found"})
            except Exception as exc:  # noqa: BLE001
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            try:
                payload = self._payload()
                path = urlparse(self.path).path
                if path == "/api/start":
                    controller.start(payload)
                elif path == "/api/stop":
                    controller.stop()
                elif path.startswith("/api/command/"):
                    controller.command(path.rsplit("/", 1)[-1], payload)
                elif path == "/api/delete-recording":
                    controller.delete_recording(str(payload.get("path", "")))
                    self._json(HTTPStatus.OK, {"deleted": True}); return
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                self._json(HTTPStatus.OK, controller.status())
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartScanATC headless receiver")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("SMARTSCAN_REMOTE_TOKEN", ""))
    parser.add_argument("--start", action="store_true", help="start scanning from the persisted receiver configuration")
    args = parser.parse_args()
    if len(args.token) < 24:
        parser.error("Set a random SMARTSCAN_REMOTE_TOKEN of at least 24 characters")
    app = QCoreApplication([])
    controller = ReceiverController()
    if args.start:
        controller.start({})
    httpd = ThreadingHTTPServer((args.host, args.port), _handler(controller, args.token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
