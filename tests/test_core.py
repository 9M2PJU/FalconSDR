"""Run: python -m unittest tests/test_core.py

Covers the branches that touch data or access: config load/save, which
recording files the library exposes (and to whom), retention, the worker
job/copy protocol, and the HTTP layer's password/CSRF checks.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Every path the app derives from ~ (config, caches) must land in a scratch
# home - set before importing smartscan.
_HOME = tempfile.mkdtemp(prefix="smartscan-test-")
os.environ["HOME"] = _HOME

from smartscan import config as config_mod  # noqa: E402
from smartscan.config import AppConfig  # noqa: E402
from smartscan.playback.index import Library  # noqa: E402

NAME = "2026-01-01_10-00-00_{khz}.wav"


def _wav(root: Path, rel: str, size: int = 100) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


class _LibCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=_HOME))
        self.root = self.tmp / "rec"
        self.root.mkdir()
        self.cfg = AppConfig()
        self.cfg.recording.output_dir = str(self.root)
        self.open: set[Path] = set()
        self.lib = Library(self.cfg, lambda: self.open, lambda: {})


class ConfigTest(unittest.TestCase):
    def test_unknown_keys_are_ignored_and_save_is_atomic(self) -> None:
        config_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config_mod.CONFIG_PATH.write_text(json.dumps({
            "device": {"driver": "rtlsdr", "from_a_newer_version": 1},
            "recording": {"output_dir": "/x", "mode": "split"},
            "bands": [{"start_hz": 1, "stop_hz": 2, "future_field": True}],
        }))
        cfg = AppConfig.load()
        self.assertEqual((cfg.device.driver, cfg.recording.output_dir, cfg.bands[0].stop_hz), ("rtlsdr", "/x", 2))
        cfg.trigger_level_dbfs = -42.0
        cfg.save()
        self.assertEqual(AppConfig.load().trigger_level_dbfs, -42.0)
        self.assertFalse(list(config_mod.CONFIG_DIR.glob("*.tmp")))


class LibraryTest(_LibCase):
    def test_open_files_and_paths_outside_the_library_are_never_exposed(self) -> None:
        _wav(self.root, "2026-01-01/" + NAME.format(khz=121500))
        writing = _wav(self.root, "2026-01-01/" + NAME.format(khz=118000))
        _wav(self.tmp, NAME.format(khz=1))  # outside the root
        self.open = {writing}
        self.lib.rescan()
        self.assertEqual([f["id"] for f in self.lib.files()], ["2026-01-01/" + NAME.format(khz=121500)])
        for bad in ("../" + NAME.format(khz=1), "2026-01-01/" + NAME.format(khz=118000)):
            with self.assertRaises(FileNotFoundError):
                self.lib.path_of(bad)
        for bad in ("../" + NAME.format(khz=1), "2026-01-01/notes.txt", "/etc/x.wav"):
            with self.assertRaises(ValueError):
                self.lib.upload_target(bad)
        self.assertEqual(self.lib.upload_target("a/" + NAME.format(khz=5)), (self.root / "a" / NAME.format(khz=5)).resolve())

    def test_retention_keeps_favorites_and_unmirrored_copies(self) -> None:
        old = ["2020-01-0%d/2020-01-0%d_10-00-00_12150%d.wav" % (i, i, i) for i in range(1, 4)]
        for rel in old:
            _wav(self.root, rel)
        self.lib.rescan()
        self.cfg.recording.retention_days = 30
        self.lib.set_favorite([old[0]], True)
        self.lib.mark_mirrored([old[1]])
        self.cfg.recording.retention_require_mirrored = True
        self.assertEqual(self.lib.retention_victims(), [old[1]])
        self.cfg.recording.retention_require_mirrored = False
        self.assertEqual(sorted(self.lib.retention_victims()), [old[1], old[2]])
        self.assertEqual(self.lib.apply_retention(), 2)
        self.assertTrue((self.root / old[0]).exists())

    def test_jobs_are_leased_and_stale_results_rejected(self) -> None:
        rel = "2026-01-01/" + NAME.format(khz=121500)
        _wav(self.root, rel)
        self.lib.rescan()
        job = self.lib.claim_job("pc")
        self.assertEqual(job["id"], rel)
        self.assertIsNone(self.lib.claim_job("server"))  # leased to "pc"
        result = {"text": "hi", "language": "en", "language_probability": 1.0, "status": "ok",
                  "engine": "x", "transcribed_at": "now"}
        self.assertFalse(self.lib.submit_result(rel, "stale-key", result))
        self.assertTrue(self.lib.submit_result(rel, job["key"], result))
        self.assertEqual(self.lib.get(rel).transcript.text, "hi")

    def test_import_matches_by_size_and_never_overwrites(self) -> None:
        rel = "2026-01-01/" + NAME.format(khz=121500)
        _wav(self.root, rel, size=100)
        self.lib.rescan()
        tr = {"text": "a", "language": "pl", "language_probability": 1, "status": "ok", "engine": "x", "transcribed_at": "t"}
        self.assertEqual(self.lib.import_metadata([{"id": rel, "size": 99, "transcript": tr}]), 0)
        self.assertEqual(self.lib.import_metadata([{"id": rel, "size": 100, "transcript": tr, "favorite": True}]), 1)
        self.assertEqual(self.lib.import_metadata([{"id": rel, "size": 100, "transcript": {**tr, "text": "b"}}]), 0)
        rec = self.lib.get(rel)
        self.assertEqual((rec.transcript.text, rec.is_favorite), ("a", True))


class WorkerMirrorTest(unittest.TestCase):
    def test_reports_only_size_verified_copies(self) -> None:
        from smartscan.worker import Worker

        class FakeApi:
            base = "http://x"
            mirrored: list[str] = []

            def get(self, _path):
                return [{"id": "d/" + NAME.format(khz=1), "size": 5}, {"id": "d/" + NAME.format(khz=2), "size": 9}]

            def download(self, rel, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"12345")  # the second file arrives short

            def post(self, _path, body, timeout=30):
                self.mirrored += body["ids"]

        root = Path(tempfile.mkdtemp(dir=_HOME))
        w = Worker("http://x", "", local_root=root)
        w.api = FakeApi()
        w._settings = {"mirror": True, "mirror_dir": ""}
        w._mirror()
        self.assertEqual(w.api.mirrored, ["d/" + NAME.format(khz=1)])


class HttpTest(unittest.TestCase):
    def _serve(self, mode: str, token: str):
        from smartscan.web.server import App, make_handler
        from http.server import ThreadingHTTPServer

        class Ctl:  # only what the tested routes touch
            library = None

            def live(self, since_log=0):
                return {"ok": True}

            def stop(self):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(AppConfig(), Ctl(), token, mode)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def _status(self, url: str, method="GET", headers=None) -> int:
        try:
            with urlopen(Request(url, method=method, data=b"{}" if method == "POST" else None, headers=headers or {}), timeout=5) as r:
                return r.status
        except HTTPError as e:
            e.close()
            return e.code

    def test_password_is_optional_and_enforced_when_set(self) -> None:
        open_url = self._serve("server", "")
        self.assertEqual(self._status(open_url + "/api/live"), 200)
        locked = self._serve("server", "secret")
        self.assertEqual(self._status(locked + "/api/live"), 401)
        self.assertEqual(self._status(locked + "/api/live", headers={"Authorization": "Bearer nope"}), 401)
        self.assertEqual(self._status(locked + "/api/live", headers={"Authorization": "Bearer secret"}), 200)

    def test_csrf_and_dns_rebinding_are_refused(self) -> None:
        url = self._serve("local", "")
        self.assertEqual(self._status(url + "/api/scanner/stop", "POST"), 403)
        self.assertEqual(self._status(url + "/api/scanner/stop", "POST", {"X-SmartScan": "1"}), 200)
        self.assertEqual(self._status(url + "/api/live", headers={"Host": "evil.example:80"}), 403)


if __name__ == "__main__":
    unittest.main()
