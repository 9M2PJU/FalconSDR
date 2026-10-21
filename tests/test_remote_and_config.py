"""Run: .venv/bin/python -m unittest tests/test_remote_and_config.py

Covers the data-touching branches that once broke for real: config.json with
unknown keys, the receiver's path/closed-file checks, and the sync rule that
may delete files on the receiver.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smartscan import config as config_mod


class _TempConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.recordings = self.tmp / "recordings"
        self.recordings.mkdir()
        self.config_path = self.tmp / "config.json"
        patcher = mock.patch.object(config_mod, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class ConfigLoadTest(_TempConfig):
    def test_unknown_keys_are_ignored(self) -> None:
        self.config_path.write_text(json.dumps({
            "device": {"driver": "rtlsdr", "from_a_newer_version": 1},
            "recording": {"output_dir": str(self.recordings), "mode": "split"},
            "bands": [{"start_hz": 1, "stop_hz": 2, "future_field": True}],
        }))
        cfg = config_mod.AppConfig.load()
        self.assertEqual(cfg.device.driver, "rtlsdr")
        self.assertEqual(cfg.recording.output_dir, str(self.recordings))
        self.assertEqual(cfg.bands[0].stop_hz, 2)

    def test_save_leaves_no_temp_file(self) -> None:
        cfg = config_mod.AppConfig()
        cfg.trigger_level_dbfs = -42.0
        cfg.save()
        self.assertEqual(config_mod.AppConfig.load().trigger_level_dbfs, -42.0)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.suffix == ".tmp"], [])


class ReceiverPathTest(_TempConfig):
    def setUp(self) -> None:
        super().setUp()
        from smartscan.remote_server import ReceiverController
        (self.recordings / "old.wav").write_bytes(b"x")
        self.config_path.write_text(json.dumps({"recording": {"output_dir": str(self.recordings)}}))
        self.controller = ReceiverController()

    def test_traversal_and_open_files_are_refused(self) -> None:
        (self.tmp / "outside.wav").write_bytes(b"x")
        (self.recordings / "still_recording.wav").write_bytes(b"x")
        for bad in ("../outside.wav", "still_recording.wav", "missing.wav"):
            with self.assertRaises(FileNotFoundError):
                self.controller.recording_path(bad)
        self.assertEqual([r["path"] for r in self.controller.recordings()], ["old.wav"])

    def test_finished_file_becomes_listed(self) -> None:
        new = self.recordings / "new.wav"
        new.write_bytes(b"xy")
        self.controller._on_recording_finished(str(new))
        self.assertEqual(self.controller.recording_path("new.wav"), new.resolve())
        self.controller.delete_recording("new.wav")
        self.assertFalse(new.exists())
        self.assertEqual([r["path"] for r in self.controller.recordings()], ["old.wav"])


class SyncDeleteTest(unittest.TestCase):
    class FakeClient:
        def __init__(self, listed_size: int, served: bytes):
            self.listed_size, self.served, self.deleted = listed_size, served, []

        def recordings(self):
            return [{"path": "2026-01-01/a.wav", "size": self.listed_size}]

        def download(self, _relative, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.served)

        def delete_recording(self, relative: str) -> None:
            self.deleted.append(relative)

    def test_deletes_only_after_size_match(self) -> None:
        from smartscan.remote_client import _sync_new_recordings
        with tempfile.TemporaryDirectory() as tmp:
            short = self.FakeClient(listed_size=10, served=b"12345")
            self.assertEqual(_sync_new_recordings(short, tmp, delete_synced=True), [])
            self.assertEqual(short.deleted, [])
        with tempfile.TemporaryDirectory() as tmp:
            full = self.FakeClient(listed_size=5, served=b"12345")
            self.assertEqual(len(_sync_new_recordings(full, tmp, delete_synced=True)), 1)
            self.assertEqual(full.deleted, ["2026-01-01/a.wav"])


if __name__ == "__main__":
    unittest.main()
