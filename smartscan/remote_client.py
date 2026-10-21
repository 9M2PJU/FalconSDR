"""Desktop-side client for :mod:`smartscan.remote_server`."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np
from PySide6.QtCore import QThread, Signal

from .audio_output import AudioOutput
from .config import AppConfig, RemoteReceiverConfig
from .models import BlacklistEntry, Channel
from .scanner_engine import AUDIO_RATE, ScannerSignals


class RemoteClient:
    def __init__(self, config: RemoteReceiverConfig):
        self.base_url = config.url.rstrip("/")
        self.token = config.token

    def _request(self, path: str, payload: dict[str, Any] | None = None, timeout: float = 8.0):
        if not self.base_url or not self.token:
            raise RuntimeError("Remote receiver URL or access token is missing")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path, data=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response, json.loads(response.read() or b"{}")
        except URLError as exc:
            raise RuntimeError(f"Remote receiver unavailable: {exc.reason}") from exc

    def status(self, timeout: float = 8.0) -> dict[str, Any]:
        _, value = self._request("/api/status", timeout=timeout)
        return value

    def start(self, config: AppConfig, channels: list[Channel], blacklist: list[BlacklistEntry]) -> dict[str, Any]:
        payload = {
            "config": asdict(config),
            "channels": [
                {**asdict(channel), "modulation": channel.modulation.value}
                for channel in channels
            ],
            "blacklist": [asdict(entry) for entry in blacklist],
        }
        _, value = self._request("/api/start", payload)
        return value

    def stop(self) -> dict[str, Any]:
        _, value = self._request("/api/stop", {})
        return value

    def command(self, name: str, **payload: Any) -> dict[str, Any]:
        _, value = self._request(f"/api/command/{name}", payload)
        return value

    def recordings(self) -> list[dict[str, Any]]:
        _, value = self._request("/api/recordings")
        return value if isinstance(value, list) else []

    def delete_recording(self, relative_path: str) -> None:
        self._request("/api/delete-recording", {"path": relative_path})

    def download(self, relative_path: str, destination: Path) -> None:
        encoded = quote(relative_path, safe="/")
        request = Request(self.base_url + "/api/recordings/" + encoded, headers={"Authorization": f"Bearer {self.token}"})
        temporary = destination.with_suffix(destination.suffix + ".part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urlopen(request, timeout=30) as response, temporary.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    target.write(chunk)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


class RemoteScannerProxy:
    """ScannerEngine-shaped proxy so the existing Listen panel stays intact."""

    def __init__(self, config: AppConfig, channels: list[Channel], blacklist: list[BlacklistEntry]):
        self.signals = ScannerSignals()
        self._client = RemoteClient(config.remote_receiver)
        self._config, self._channels, self._blacklist = config, list(channels), list(blacklist)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sync_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._audio_out: AudioOutput | None = None
        self._audio_response = None
        self._last_state: str | None = None
        self._last_frequencies: list[tuple[int, str]] = []
        self._last_log: str | None = None
        self._last_cycle_time: float | None = None

    # Polled 4x/s for live level/state/frequency/spectrum feedback - plain
    # status GETs against the simple stdlib HTTP server, cheap even on a
    # Pi-class receiver. Downloading finished recordings is on its own,
    # much slower cadence (_SYNC_INTERVAL_S) since that one walks the whole
    # recordings tree server-side - see _sync_loop.
    _POLL_INTERVAL_S = 0.25
    _SYNC_INTERVAL_S = 5.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="remote-receiver")
        self._thread.start()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="remote-receiver-sync")
        self._sync_thread.start()
        self._audio_out = AudioOutput(
            AUDIO_RATE, self._config.device.audio_output_device, debug=self._config.debug_mode,
        )
        self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True, name="remote-receiver-audio")
        self._audio_thread.start()

    def _run(self) -> None:
        try:
            self._client.start(self._config, self._channels, self._blacklist)
            self.signals.gain_range_changed.emit(0.0, 50.0)
            while not self._stop.is_set():
                status = self._client.status()
                state = str(status.get("state", "IDLE"))
                if state != self._last_state:
                    self._last_state = state
                    self.signals.state_changed.emit(state)
                self.signals.level_changed.emit(float(status.get("level_dbfs", -120.0)))
                self._last_frequencies = [(int(freq), str(name)) for freq, name in status.get("frequencies", [])]
                self.signals.freq_changed.emit(self._last_frequencies)
                self.signals.recording_changed.emit(bool(status.get("recording")), str(status.get("recording_path", "")))
                log_message = status.get("log")
                if log_message and log_message != self._last_log:
                    self._last_log = log_message
                    self.signals.log.emit(str(log_message))
                cycle_time = status.get("cycle_time")
                if cycle_time is not None and cycle_time != self._last_cycle_time:
                    self._last_cycle_time = cycle_time
                    self.signals.cycle_time_changed.emit(float(cycle_time))
                spectrum = status.get("spectrum")
                if spectrum:
                    self.signals.spectrum_ready.emit(
                        np.asarray(spectrum["freqs"], dtype=np.float64),
                        np.asarray(spectrum["db"], dtype=np.float64),
                        float(spectrum["trigger_dbfs"]),
                        bool(spectrum["custom"]),
                    )
                if not status.get("running") and state == "IDLE":
                    break
                self._stop.wait(self._POLL_INTERVAL_S)
        except Exception as exc:  # noqa: BLE001 - surfaced in the existing status bar
            self.signals.log.emit(str(exc))
        finally:
            self.signals.finished.emit()

    def _sync_loop(self) -> None:
        """Downloads newly-finished recordings in the background, the same
        way the "Download recordings" button does - so Odtwarzacz picks
        them up without the user remembering to click it after every
        transmission. Runs on its own thread/cadence (see
        _SYNC_INTERVAL_S), independent of the fast status-poll loop, since
        it walks the receiver's whole recordings tree per call."""
        while not self._stop.wait(self._SYNC_INTERVAL_S):
            try:
                for path in _sync_new_recordings(
                    self._client, self._config.recording.output_dir,
                    self._config.remote_receiver.delete_synced,
                ):
                    self.signals.recording_finished.emit(str(path))
            except Exception as exc:  # noqa: BLE001
                self.signals.log.emit(f"Remote sync: {exc}")

    def _audio_loop(self) -> None:
        """Streams the primary slot's live audio from the receiver to this
        machine's speaker - raw mono 16-bit PCM at AUDIO_RATE over one
        long-lived GET (see remote_server.py's /api/audio). A drop just
        means reconnecting; the server itself closes an idle connection
        after a long silence (nothing to send - not muted, nothing wrong),
        so reconnecting here is routine, not logged as an error."""
        url = self._client.base_url + "/api/audio"
        while not self._stop.is_set():
            try:
                request = Request(url, headers={"Authorization": f"Bearer {self._client.token}"})
                # Bounds worst-case shutdown latency even if closing
                # _audio_response from another thread doesn't unblock this
                # read promptly (join() relies on this, not just that
                # close()) - reconnecting this often during a genuinely
                # quiet scanner is a trivial cost on a LAN.
                with urlopen(request, timeout=5) as response:
                    self._audio_response = response
                    leftover = b""
                    while not self._stop.is_set():
                        chunk = response.read(4096)
                        if not chunk:
                            break
                        buf = leftover + chunk
                        usable = len(buf) - (len(buf) % 2)
                        if usable == 0:
                            leftover = buf
                            continue
                        samples = np.frombuffer(buf[:usable], dtype="<i2").astype(np.float32) / 32768.0
                        leftover = buf[usable:]
                        if self._audio_out is not None:
                            self._audio_out.write(samples)
            except Exception:
                pass
            finally:
                self._audio_response = None
            if not self._stop.is_set():
                self._stop.wait(1.0)

    def request_stop(self) -> None:
        """The real Stop button - also tells the receiver itself to stop
        scanning/recording, same as pressing Stop on a local engine."""
        self._stop_local_threads(also_stop_receiver=True)

    def detach(self) -> None:
        """Closing this desktop window, *not* pressing Stop - stops only
        this window's own polling/audio/sync threads. The receiver is
        meant to be always-on regardless of whether any desktop client is
        watching (see RemoteReceiverPanel's hint text); a real, reported
        case of request_stop() being called from MainWindow._shutdown()
        unconditionally turned the receiver itself off just by closing the
        window - indistinguishable from pressing Stop, which defeats the
        whole point of a headless receiver."""
        self._stop_local_threads(also_stop_receiver=False)

    def _stop_local_threads(self, also_stop_receiver: bool) -> None:
        # Called straight from the GUI thread (ScannerPanel's Stop button,
        # or window close) - must return immediately. AudioOutput.close()
        # alone measured over 1s (PortAudio/ALSA stream teardown), which is
        # exactly the kind of multi-hundred-ms block that makes Qt show
        # "window not responding" - so none of this runs here, only on the
        # background thread below.
        self._stop.set()

        def _cleanup() -> None:
            if self._audio_response is not None:
                try:
                    self._audio_response.close()
                except Exception:
                    pass
            if self._audio_out is not None:
                self._audio_out.close()
            if also_stop_receiver:
                self._client.stop()

        threading.Thread(target=_cleanup, daemon=True).start()

    def request_skip(self) -> None:
        threading.Thread(target=self._client.command, args=("skip",), daemon=True).start()

    def request_skip_and_blacklist(self) -> None:
        for freq_hz, _name in self._last_frequencies:
            self.signals.blacklisted.emit(freq_hz)
        threading.Thread(target=self._client.command, args=("blacklist",), daemon=True).start()

    def set_live_muted(self, muted: bool) -> None:
        threading.Thread(target=self._client.command, args=("mute",), kwargs={"muted": muted}, daemon=True).start()

    def start_manual_recording(self) -> None:
        threading.Thread(target=self._client.command, args=("manual_record",), kwargs={"enabled": True}, daemon=True).start()

    def stop_manual_recording(self) -> None:
        threading.Thread(target=self._client.command, args=("manual_record",), kwargs={"enabled": False}, daemon=True).start()

    def set_auto_recording(self, enabled: bool) -> None:
        threading.Thread(target=self._client.command, args=("auto_recording",), kwargs={"enabled": enabled}, daemon=True).start()

    def start_raw_iq_recording(self) -> None:
        self.signals.log.emit("Raw IQ recording is available only on a local receiver")

    def stop_raw_iq_recording(self) -> None:
        return

    def update_channels(self, channels: list[Channel]) -> None:
        self._channels = list(channels)

    def update_blacklist(self, blacklist: list[BlacklistEntry]) -> None:
        self._blacklist = list(blacklist)

    def request_rebuild(self) -> None:
        return

    def set_solo_band(self, _band) -> None:
        return

    def join(self, timeout: float | None = None) -> None:
        # 3 background threads now, not 1 - giving each the *full* timeout
        # (the old behaviour) let the GUI thread block for up to 3x longer
        # than whatever the caller actually asked for/expects (MainWindow's
        # own join(timeout=2.0) - a real, reported "window not responding"
        # on Stop). Split the caller's budget across all of them instead,
        # same total worst case as a single-threaded engine.
        threads = [t for t in (self._thread, self._sync_thread, self._audio_thread) if t is not None]
        per_thread = (timeout / len(threads)) if (timeout is not None and threads) else timeout
        for t in threads:
            t.join(per_thread)


def _sync_new_recordings(client: "RemoteClient", destination: str, delete_synced: bool = False) -> list[Path]:
    """Downloads whatever recordings aren't already present (by path+size)
    under ``destination``, returning the local paths just downloaded. Shared
    by the manual "Download recordings" button (RemoteSyncWorker) and
    RemoteScannerProxy's own background auto-sync, so both stay in sync.

    ``delete_synced`` removes the file from the receiver once *this*
    machine's own copy is confirmed complete - either just now (download()
    only renames its temp file into place after a full, uninterrupted
    read, so a crash mid-transfer simply leaves nothing to delete yet) or
    already, from an earlier sync that downloaded it but never got to
    delete it (e.g. the app closed right after) - the size check here
    re-confirms that before deleting rather than trusting the earlier run.
    Deleting only ever happens strictly after this process has its own
    verified-complete copy, never before or during a transfer."""
    root = Path(destination).resolve()
    downloaded: list[Path] = []
    for item in client.recordings():
        relative = Path(str(item["path"]))
        target = (root / relative).resolve()
        if root not in target.parents:
            continue
        expected_size = int(item["size"])
        if target.is_file() and target.stat().st_size == expected_size:
            if delete_synced:
                client.delete_recording(relative.as_posix())
            continue
        try:
            client.download(relative.as_posix(), target)
        except Exception:
            # One file failing (network hiccup) must not lose track of
            # every other file already downloaded earlier in this same
            # call - that used to mean the whole batch's return value was
            # lost, so already-downloaded-but-not-yet-emitted files were
            # silently skipped on the next cycle too (the "skip, already
            # matches" branch above never emits) - ingested only on the
            # next full rescan instead of right away.
            continue
        if target.is_file() and target.stat().st_size == expected_size:
            downloaded.append(target)
            if delete_synced:
                client.delete_recording(relative.as_posix())
    return downloaded


class RemoteSyncWorker(QThread):
    completed = Signal(int)
    failed = Signal(str)

    def __init__(self, config: RemoteReceiverConfig, destination: str, parent=None):
        super().__init__(parent)
        self._config = config
        self._destination = Path(destination)

    def run(self) -> None:
        try:
            client = RemoteClient(self._config)
            copied = len(_sync_new_recordings(client, str(self._destination), self._config.delete_synced))
            self.completed.emit(copied)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
