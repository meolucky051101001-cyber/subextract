"""Offline metadata recovery regressions; no real jobs or services are opened."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.jobs import Job, JobStore


class JobRecoveryRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="subextract-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cues = [{"id": "cue-a", "start": 1.5, "end": 3, "text": "Nguồn 卡片",
                      "trans": "Edited translation", "region": 1}]
        self.stats = {"handoff": {"job_id": "remote-original", "status": "rendering"},
                      "untranslated_indices": [0]}
        self.params = {"translate_to": "en", "roi": [[0, 0.5, 1, 0.5]]}

    def make_job(self, name="healthy", **updates):
        directory = self.root / name
        directory.mkdir()
        source = directory / "source.mp4"
        source.write_bytes(b"unchanged original video")
        values = dict(id=name, work_dir=directory, filename="Nguồn 卡片.mp4",
                      video_path=source, status="done", cues=copy.deepcopy(self.cues),
                      stats=copy.deepcopy(self.stats), params=copy.deepcopy(self.params),
                      info={"duration": 5.0, "width": 1920, "height": 1080, "fps": 24},
                      created_at=0.0)
        values.update(updates)
        job = Job(**values)
        job.save()
        return job

    def test_malformed_metadata_isolated_with_diagnostics_and_untouched_files(self):
        healthy = self.make_job()
        valid = json.loads((healthy.work_dir / "job.json").read_text(encoding="utf-8"))
        malformed = [b"[]", b"null", b"123", b"true", b'"text"', b"{not JSON", b"\xff"]
        updates = [
            {"id": []}, {"id": ""}, {"filename": 12}, {"status": []}, {"status": "unknown"},
            {"error": {}}, {"message": []}, {"created_at": "yesterday"},
            {"created_at": True}, {"created_at": float("inf")}, {"created_at": 10 ** 400},
            {"cues": {}}, {"cues": None}, {"cues": ["wrong"]}, {"cues": [{}]},
            {"cues": [{"start": "Infinity", "end": 2, "text": "bad"}]},
            {"cues": [{"start": 0, "end": True, "text": "bad"}]},
            {"cues": [{"start": 0, "end": 2, "text": []}]},
            {"cues": [{"start": 0, "end": 2, "text": "bad", "trans": []}]},
            {"cues": [{"start": 0, "end": 2, "text": "bad", "id": {}}]},
            {"cues": [{"start": 0, "end": 2, "text": "bad", "region": True}]},
            {"stats": []}, {"stats": None}, {"params": []}, {"info": []},
            {"info": {"width": "wide"}}, {"preview_status": []}, {"preview_status": "bad"},
            {"stats": {"nested": [{"value": float("nan")}]}},
            {"params": {"nested": [float("-inf")]}},
        ]
        for update in updates:
            data = copy.deepcopy(valid)
            data.update(update)
            malformed.append(json.dumps(data).encode("utf-8"))
        malformed.append(b'{"stats": {"number": 1e999}}')
        originals = []
        for index, content in enumerate(malformed):
            directory = self.root / f"bad-{index}"
            directory.mkdir()
            source = directory / "source.mp4"
            source.write_bytes(b"original bad metadata source")
            metadata = directory / "job.json"
            metadata.write_bytes(content)
            originals.append((source, source.read_bytes(), metadata, content))
        with self.assertLogs("app.jobs", level="WARNING") as logs:
            store = JobStore(self.root)
        self.assertEqual(set(store._jobs), {"healthy"})
        self.assertEqual(store.get("healthy").cues, self.cues)
        self.assertEqual(len(logs.output), len(malformed))
        for source, source_bytes, metadata, content in originals:
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(metadata.read_bytes(), content)
            self.assertTrue(any(str(metadata) in message for message in logs.output))
        self.assertEqual(list(self.root.glob("*/.job-*.tmp")), [])

    def test_terminal_and_uploaded_status_error_and_last_good_survive_two_restarts(self):
        for status in ("uploaded", "done", "error", "cancelled"):
            with self.subTest(status=status):
                job = self.make_job(status, status=status, error="original diagnostic",
                                    message="original message", preview_status="error")
                original = (job.work_dir / "job.json").read_bytes()
                for _ in range(2):
                    loaded = JobStore(self.root).get(status)
                    self.assertEqual(loaded.status, status)
                    self.assertEqual(loaded.error, "original diagnostic")
                    self.assertEqual(loaded.message, "original message")
                    self.assertEqual(loaded.preview_status, "error")
                    self.assertEqual(loaded.cues, self.cues)
                    self.assertEqual(loaded.stats, self.stats)
                    self.assertEqual(loaded.params, self.params)
                    self.assertEqual(loaded.created_at, 0.0)
                    self.assertEqual((job.work_dir / "job.json").read_bytes(), original)

    def test_running_recovery_preserves_last_good_and_diagnostic_after_two_restarts(self):
        job = self.make_job(status="running", message="rerun started")
        source_bytes = job.video_path.read_bytes()
        first = JobStore(self.root).get(job.id)
        self.assertEqual(first.status, "error")
        self.assertIn("gián đoạn", first.error)
        self.assertEqual(first.message, first.error)
        saved = json.loads((job.work_dir / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "error")
        second = JobStore(self.root).get(job.id)
        self.assertEqual(second.error, first.error)
        self.assertEqual(second.message, first.message)
        self.assertEqual(second.status, "error")
        self.assertEqual(second.cues, self.cues)
        self.assertEqual(second.stats, self.stats)
        self.assertEqual(second.params, self.params)
        self.assertEqual(second.video_path.read_bytes(), source_bytes)

    def test_recovery_save_failure_does_not_block_valid_jobs_or_change_original(self):
        running = self.make_job(status="running")
        self.make_job("other")
        original = (running.work_dir / "job.json").read_bytes()
        with patch("app.jobs.os.replace", side_effect=OSError("offline write failure")):
            with self.assertLogs("app.jobs", level="WARNING") as logs:
                store = JobStore(self.root)
        self.assertEqual(set(store._jobs), {"healthy", "other"})
        self.assertEqual(store.get("healthy").status, "error")
        self.assertEqual(store.get("healthy").cues, self.cues)
        self.assertTrue(any("Could not persist recovered job" in item for item in logs.output))
        self.assertEqual((running.work_dir / "job.json").read_bytes(), original)
        self.assertEqual(list(self.root.glob("*/.job-*.tmp")), [])

    def test_unreadable_metadata_isolated_from_other_jobs(self):
        healthy = self.make_job()
        unreadable = self.make_job("unreadable")
        original_read = Path.read_text

        def read_with_failure(path, *args, **kwargs):
            if path == unreadable.work_dir / "job.json":
                raise PermissionError("mocked unreadable metadata")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_with_failure):
            with self.assertLogs("app.jobs", level="WARNING"):
                store = JobStore(self.root)
        self.assertEqual(set(store._jobs), {healthy.id})
        self.assertTrue(unreadable.video_path.is_file())

    def test_missing_source_is_reported_without_modifying_metadata(self):
        directory = self.root / "missing-source"
        directory.mkdir()
        metadata = directory / "job.json"
        metadata.write_text('{"id": "missing-source"}', encoding="utf-8")
        original = metadata.read_bytes()
        self.make_job()
        with self.assertLogs("app.jobs", level="WARNING") as logs:
            store = JobStore(self.root)
        self.assertEqual(set(store._jobs), {"healthy"})
        self.assertTrue(any("source video is missing" in item for item in logs.output))
        self.assertEqual(metadata.read_bytes(), original)

    def test_duplicate_job_id_does_not_replace_an_already_loaded_job(self):
        first = self.make_job("first", id="duplicate")
        second = self.make_job("second", id="duplicate")
        with self.assertLogs("app.jobs", level="WARNING") as logs:
            store = JobStore(self.root)
        self.assertEqual(set(store._jobs), {"duplicate"})
        self.assertIn(store.get("duplicate").work_dir, (first.work_dir, second.work_dir))
        self.assertTrue(any("duplicate job id" in item for item in logs.output))
        self.assertTrue(first.video_path.is_file())
        self.assertTrue(second.video_path.is_file())

    def test_legacy_minimal_metadata_uses_safe_defaults(self):
        job = self.make_job()
        (job.work_dir / "job.json").write_text('{}', encoding="utf-8")
        loaded = JobStore(self.root).get("healthy")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "uploaded")
        self.assertEqual(loaded.cues, [])
        self.assertEqual(loaded.error, "")
        self.assertEqual(loaded.preview_status, "none")

    def test_preview_requires_both_regular_files_before_snapshot_save_or_reload_ready(self):
        for preview in (False, True):
            for marker in (False, True):
                for status in ("none", "running", "ready", "error"):
                    with self.subTest(preview=preview, marker=marker, status=status):
                        name = f"preview-{preview}-{marker}-{status}"
                        job = self.make_job(name, preview_status=status)
                        if preview:
                            job.preview_path.write_bytes(b"mock completed preview")
                        if marker:
                            job.preview_ready_path.write_bytes(b"")
                        self.assertEqual(job.preview_ready, preview and marker)
                        expected_saved = ("ready" if preview and marker else
                                          "none" if status == "ready" else status)
                        self.assertEqual(job.snapshot()["preview_status"], expected_saved)
                        job.save()
                        metadata = job.work_dir / "job.json"
                        data = json.loads(metadata.read_text(encoding="utf-8"))
                        self.assertEqual(data["preview_status"], expected_saved)
                        # Exercise stale ready and running values written by older versions.
                        data["preview_status"] = status
                        metadata.write_text(json.dumps(data), encoding="utf-8")
                        loaded = JobStore(self.root).get(name)
                        expected_loaded = ("ready" if preview and marker else
                                           "error" if status == "error" else "none")
                        self.assertEqual(loaded.preview_status, expected_loaded)

    def test_preview_directories_do_not_satisfy_readiness(self):
        job = self.make_job(preview_status="ready")
        job.preview_path.mkdir()
        job.preview_ready_path.mkdir()
        self.assertFalse(job.preview_ready)
        self.assertEqual(job.snapshot()["preview_status"], "none")
        job.save()
        self.assertEqual(JobStore(self.root).get(job.id).preview_status, "none")

    def test_preview_lock_is_per_job_and_separate_from_run_lock(self):
        first = self.make_job()
        second = self.make_job("second")
        self.assertEqual(first.preview_ready_path, first.work_dir / ".preview-ready")
        with first.preview_lock:
            self.assertFalse(first.preview_lock.acquire(blocking=False))
            self.assertTrue(second.preview_lock.acquire(blocking=False))
            second.preview_lock.release()
            self.assertTrue(first.run_lock.acquire(blocking=False))
            first.run_lock.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
