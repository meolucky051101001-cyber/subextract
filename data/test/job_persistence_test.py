"""Offline persistence regressions; all files live in a temporary directory."""
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.jobs import Job, JobStore


class JobPersistenceRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="subextract-job-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "offline"
        self.directory.mkdir()
        source = self.directory / "source.mp4"
        source.write_bytes(b"offline fixture")
        self.job = Job(id="offline", work_dir=self.directory, filename="Nhãn 卡片.mp4",
                       video_path=source, status="done",
                       cues=[{"id": "cue-a", "start": 0, "end": 1, "text": "Nhãn 卡片"}])
        self.job.save()
        self.path = self.directory / "job.json"
        self.original = self.path.read_bytes()

    def assert_original_survived(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.directory.glob(".job-*.tmp")), [])
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_unicode_schema_and_ids_survive_reload(self):
        loaded = JobStore(self.root).get("offline")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.cues, self.job.cues)
        self.assertEqual(loaded.filename, "Nhãn 卡片.mp4")
        self.assertEqual(loaded.status, "done")
        self.assertEqual(list(self.directory.glob(".job-*.tmp")), [])

    def test_serialization_failure_keeps_original_file(self):
        self.job.stats = {"invalid": object()}
        with self.assertRaises(TypeError):
            self.job.save()
        self.assert_original_survived()

    def test_nonfinite_numbers_never_replace_valid_metadata(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            for field in ("cues", "stats", "info", "params", "created_at"):
                with self.subTest(value=value, field=field):
                    original = getattr(self.job, field)
                    if field == "cues":
                        invalid = [{"start": 0, "end": value, "text": "invalid"}]
                    elif field == "created_at":
                        invalid = value
                    else:
                        invalid = {"nested": [{"number": value}]}
                    setattr(self.job, field, invalid)
                    try:
                        with self.assertRaises(ValueError):
                            self.job.save()
                        self.assert_original_survived()
                    finally:
                        setattr(self.job, field, original)

    def test_replace_failure_keeps_original_and_removes_temporary(self):
        self.job.message = "new contents"
        with patch("app.jobs.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.job.save()
        self.assert_original_survived()

    def test_write_failure_keeps_original_and_removes_temporary(self):
        original_factory = tempfile.NamedTemporaryFile

        def failing_file(*args, **kwargs):
            output = original_factory(*args, **kwargs)
            output.write = lambda content: (_ for _ in ()).throw(OSError("disk write failed"))
            return output

        with patch("app.jobs.tempfile.NamedTemporaryFile", side_effect=failing_file):
            with self.assertRaises(OSError):
                self.job.save()
        self.assert_original_survived()

    def test_fsync_failure_keeps_original_and_removes_temporary(self):
        with patch("app.jobs.os.fsync", side_effect=OSError("flush failed")):
            with self.assertRaises(OSError):
                self.job.save()
        self.assert_original_survived()

    def test_file_creation_failure_preserves_original(self):
        with patch("app.jobs.tempfile.NamedTemporaryFile", side_effect=OSError("cannot create")):
            with self.assertRaises(OSError):
                self.job.save()
        self.assert_original_survived()

    def test_concurrent_saves_are_serialized_and_leak_no_temporary_files(self):
        original_replace = __import__("os").replace
        observed = []
        entered = threading.Event()
        release = threading.Event()

        def replace_and_inspect(source, destination):
            if not entered.is_set():
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test release timed out")
            observed.append(json.loads(Path(source).read_text(encoding="utf-8")))
            original_replace(source, destination)

        with patch("app.jobs.os.replace", side_effect=replace_and_inspect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.job.save)
                self.assertTrue(entered.wait(3))
                second = pool.submit(self.job.save)
                try:
                    self.assertEqual(len(list(self.directory.glob(".job-*.tmp"))), 1)
                finally:
                    release.set()
                first.result(timeout=3)
                second.result(timeout=3)
        self.assertEqual(len(observed), 2)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["cues"], self.job.cues)
        self.assertEqual(list(self.directory.glob(".job-*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
