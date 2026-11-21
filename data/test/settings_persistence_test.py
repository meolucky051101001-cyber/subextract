"""Settings transactions tested only against isolated temporary files."""
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app import settings


class SettingsPersistenceRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="subextract-settings-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config" / "settings.json"
        self.path_patch = patch.object(settings, "SETTINGS_PATH", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        settings.save_settings({"existing": "Nhãn 卡片", "translate_model": "original"})
        self.original = self.path.read_bytes()

    def assert_original_survived(self):
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_unicode_merge_null_omission_and_empty_values_roundtrip(self):
        settings.save_settings({"existing": None, "new": "Tiếng Việt 中文", "clear": "", "off": False})
        self.assertEqual(settings.load_settings(), {
            "existing": "Nhãn 卡片", "translate_model": "original", "new": "Tiếng Việt 中文",
            "clear": "", "off": False,
        })
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_serialization_failure_keeps_original_without_temporary(self):
        with self.assertRaises(TypeError):
            settings.save_settings({"invalid": object()})
        self.assert_original_survived()

    def test_nonfinite_values_keep_original_without_temporary(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settings.save_settings({"invalid": {"nested": [value]}})
                self.assert_original_survived()

    def test_creation_failure_preserves_original(self):
        with patch("app.settings.tempfile.NamedTemporaryFile", side_effect=OSError("cannot create")):
            with self.assertRaises(OSError):
                settings.save_settings({"new": "value"})
        self.assert_original_survived()

    def test_write_failure_preserves_original_and_cleans_temporary(self):
        factory = tempfile.NamedTemporaryFile

        def failing_file(*args, **kwargs):
            output = factory(*args, **kwargs)
            output.write = lambda content: (_ for _ in ()).throw(OSError("cannot write"))
            return output

        with patch("app.settings.tempfile.NamedTemporaryFile", side_effect=failing_file):
            with self.assertRaises(OSError):
                settings.save_settings({"new": "value"})
        self.assert_original_survived()

    def test_flush_failure_preserves_original_and_cleans_temporary(self):
        factory = tempfile.NamedTemporaryFile

        def failing_file(*args, **kwargs):
            output = factory(*args, **kwargs)
            output.flush = lambda: (_ for _ in ()).throw(OSError("cannot flush"))
            return output

        with patch("app.settings.tempfile.NamedTemporaryFile", side_effect=failing_file):
            with self.assertRaises(OSError):
                settings.save_settings({"new": "value"})
        self.assert_original_survived()

    def test_fsync_failure_preserves_original_and_cleans_temporary(self):
        with patch("app.settings.os.fsync", side_effect=OSError("cannot sync")):
            with self.assertRaises(OSError):
                settings.save_settings({"new": "value"})
        self.assert_original_survived()

    def test_replace_failure_preserves_original_and_cleans_temporary(self):
        with patch("app.settings.os.replace", side_effect=OSError("cannot replace")):
            with self.assertRaises(OSError):
                settings.save_settings({"new": "value"})
        self.assert_original_survived()
        # The failed transaction released its lock and later updates can succeed.
        settings.save_settings({"retry": "works"})
        self.assertEqual(settings.load_settings()["retry"], "works")

    def test_unique_same_directory_temporary_is_flushed_and_synced_before_replace(self):
        factory = tempfile.NamedTemporaryFile
        original_replace = os.replace
        original_fsync = os.fsync
        created = []
        events = []

        def observed_file(*args, **kwargs):
            output = factory(*args, **kwargs)
            created.append(Path(output.name))
            original_flush = output.flush

            def flush():
                events.append("flush")
                return original_flush()

            output.flush = flush
            return output

        def fsync(fd):
            events.append("fsync")
            return original_fsync(fd)

        def replace(source, destination):
            events.append("replace")
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8"))["existing"], "Nhãn 卡片")
            return original_replace(source, destination)

        with patch("app.settings.tempfile.NamedTemporaryFile", side_effect=observed_file):
            with patch("app.settings.os.fsync", side_effect=fsync):
                with patch("app.settings.os.replace", side_effect=replace):
                    settings.save_settings({"one": 1})
                    settings.save_settings({"two": 2})
        self.assertEqual(len(set(created)), 2)
        self.assertEqual(events, ["flush", "fsync", "replace", "flush", "fsync", "replace"])
        self.assertTrue(all(not path.exists() for path in created))

    def test_two_independent_concurrent_fields_merge_without_lost_update(self):
        first_at_replace = threading.Event()
        release_first = threading.Event()
        second_attempt = threading.Event()
        original_replace = os.replace
        original_load = settings.load_settings
        reads = []

        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.guard = threading.Lock()
                self.attempts = 0

            def __enter__(self):
                with self.guard:
                    self.attempts += 1
                    if self.attempts == 2:
                        second_attempt.set()
                self.lock.acquire()
                return self

            def __exit__(self, *args):
                self.lock.release()

        def load():
            data = original_load()
            reads.append(dict(data))
            return data

        def replace(source, destination):
            if not first_at_replace.is_set():
                first_at_replace.set()
                if not release_first.wait(3):
                    raise AssertionError("test release timed out")
            return original_replace(source, destination)

        with patch.object(settings, "_settings_lock", ObservedLock()):
            with patch("app.settings.load_settings", side_effect=load):
                with patch("app.settings.os.replace", side_effect=replace):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(settings.save_settings, {"translate_model": "model-new"})
                        try:
                            self.assertTrue(first_at_replace.wait(3))
                            second = pool.submit(settings.save_settings, {"dub_output_dir": "new-folder"})
                            self.assertTrue(second_attempt.wait(3))
                            self.assertEqual(len(reads), 1, "second writer read before the first committed")
                        finally:
                            release_first.set()
                        first.result(timeout=3)
                        second.result(timeout=3)
        loaded = settings.load_settings()
        self.assertEqual(loaded["translate_model"], "model-new")
        self.assertEqual(loaded["dub_output_dir"], "new-folder")
        self.assertEqual(loaded["existing"], "Nhãn 卡片")
        self.assertEqual(reads[1]["translate_model"], "model-new")
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
