"""Offline acceptance tests for audit API fixes.

Run: Python310/python.exe -B data/test/audit_api_test.py
All job files live in TemporaryDirectory; import never constructs a production
JobStore. External pipeline, encoder, probe, settings and handoff calls are mocked.
"""
from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from app.jobs import Job, JobStore

# Import can recover interrupted jobs, so patch BEFORE app.main is imported.
with patch("app.jobs.JobStore", return_value=Mock()):
    import app.main as api


def cue(cue_id="old", start=1.0, end=2.0, text="Original", trans="Translation"):
    return dict(id=cue_id, start=start, end=end, text=text, trans=trans, region=0)


def remote(remote_id="old-remote", status="done", **changes):
    return {"attempt_id": "attempt-" + remote_id, "base_url": "http://127.0.0.1:8088",
            "remote_job_id": remote_id, "status": status, "progress": 100,
            "stage": "done", "error": "", "result": None, "unreachable": "", **changes}


class ApiAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="subextract-audit-api-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "jobs"
        work = self.data / "offline"
        work.mkdir(parents=True)
        source = work / "source.mp4"
        source.write_bytes(b"offline video fixture")
        self.job = Job(id="offline", work_dir=work, filename="source.mp4", video_path=source,
                       status="done", cues=[cue()], params={"translate_to": "en", "old": True},
                       stats={"old": True, "handoff": remote()})
        Job.save(self.job)
        self.saved = []
        self.save = Mock(side_effect=self.persist)
        self.job.save = self.save
        self.store = Mock()
        self.store.get.side_effect = lambda job_id: self.job if job_id == self.job.id else None
        self.added = []
        self.store.add.side_effect = self.added.append
        self.pipeline = Mock(side_effect=AssertionError("Unexpected OCR/pipeline"))
        self.import_remote = Mock(return_value={"base_url": "http://127.0.0.1:8088",
                                               "render": {"job_id": "new-remote"}, "imported": {}})
        self.probe = Mock(return_value=SimpleNamespace(duration=10, width=1280, height=720,
                                                       fps=25, codec="h264"))
        for patcher in (
            patch.object(api, "DATA_DIR", self.data), patch.object(api, "store", self.store),
            patch.object(api, "load_settings", return_value={}),
            patch.object(api, "get_gemini_keys", return_value=["offline-fake-key"]),
            patch.object(api, "gpu_info", return_value={"available": False}),
            patch.object(api, "run_pipeline", self.pipeline),
            patch.object(api, "probe_video", self.probe),
            patch.object(api, "MAX_UPLOAD_BYTES", 1024),
            patch.object(api, "MAX_DATA_BYTES", 10 * 1024 * 1024),
            patch.object(api, "MIN_FREE_BYTES", 128),
            patch.object(api.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024 * 1024)),
            patch.object(api.handoff, "import_and_render", self.import_remote),
            patch.object(api.handoff, "status", side_effect=AssertionError("Unexpected remote poll")),
            patch.object(api.handoff, "cancel", side_effect=AssertionError("Unexpected remote cancel")),
            patch.object(api, "translate_cues", side_effect=AssertionError("Unexpected translation")),
            patch("subprocess.run", side_effect=AssertionError("Unexpected encoder/process")),
            patch.object(api, "_handoff_busy", set()),
            patch.object(api, "_handoff_recovery", {}),
            patch.object(api, "_upload_lock", threading.Lock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # The Windows asyncio portal needs its internal socketpair during startup.
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            patcher = patch(name, side_effect=AssertionError("Network is forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def persist(self):
        Job.save(self.job)
        self.saved.append(copy.deepcopy(self.job.snapshot(include_cues=True)))

    def metadata(self):
        return json.loads((self.job.work_dir / "job.json").read_text(encoding="utf-8"))

    def raw_request(self, method, path, value):
        return self.client.request(method, path, content=json.dumps(value),
                                   headers={"Content-Type": "application/json"})

    def reserve_run(self, **changes):
        with patch.object(api.threading, "Thread") as thread:
            result = api.start_job("offline", api.StartBody(**changes))
            self.assertTrue(result["ok"])
            thread.return_value.start.assert_called_once()
            return thread.call_args.kwargs["args"][1]

    def finish_run(self, **changes):
        params = self.reserve_run(**changes)
        api._run_job_thread(self.job, params)
        self.assertFalse(self.job.run_lock.locked())

    def assert_last_good(self, before):
        for name in ("cues", "stats", "params"):
            self.assertEqual(getattr(self.job, name), before[name], name)
            self.assertEqual(self.metadata()[name], before[name], "persisted " + name)

    def test_nonfinite_cue_times_are_client_errors_without_mutation(self):
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        for value in (float("inf"), float("-inf"), float("nan"), "Infinity", "NaN"):
            for field in ("start", "end"):
                with self.subTest(field=field, value=value):
                    changed = cue(**{field: value})
                    response = self.raw_request("PUT", "/api/jobs/offline/cues", {"cues": [changed]})
                    self.assertIn(response.status_code, (400, 422), response.text)
                    self.assert_last_good(before)
        self.save.assert_not_called()

    def test_nonfinite_start_parameters_are_client_errors_without_reservation(self):
        for field in ("sample_fps", "min_conf", "merge_gap", "min_duration"):
            for value in (float("inf"), float("nan"), "-Infinity", "NaN"):
                with self.subTest(field=field, value=value):
                    response = self.raw_request("POST", "/api/jobs/offline/start", {field: value})
                    self.assertIn(response.status_code, (400, 422), response.text)
                    self.assertFalse(self.job.run_lock.locked())
                    self.assertEqual(self.job.status, "done")
        self.save.assert_not_called()
        self.pipeline.assert_not_called()

    def test_nonfinite_roi_values_are_client_errors_without_reservation(self):
        for value in (float("inf"), float("-inf"), float("nan"), "Infinity", "NaN"):
            for index in range(4):
                with self.subTest(value=value, coordinate=index):
                    roi = [0, .5, 1, .5]
                    roi[index] = value
                    response = self.raw_request("POST", "/api/jobs/offline/start", {"roi": [roi]})
                    self.assertIn(response.status_code, (400, 422), response.text)
                    self.assertFalse(self.job.run_lock.locked())
        self.save.assert_not_called()

    def test_rerun_preserves_last_good_until_worker_publishes(self):
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        self.reserve_run(sample_fps=9)
        try:
            self.assert_last_good(before)
            self.assertEqual(self.client.get("/api/jobs/offline").json()["cues"], before["cues"])
            duplicate = self.client.post("/api/jobs/offline/start", json={})
            self.assertEqual(duplicate.status_code, 409)
            self.assert_last_good(before)
        finally:
            self.job.run_lock.release()

    def test_failed_rerun_keeps_last_good_and_persists_error(self):
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        self.pipeline.side_effect = RuntimeError("offline pipeline failure")
        self.finish_run(sample_fps=9)
        self.assertEqual(self.job.status, "error")
        self.assertIn("offline pipeline failure", self.metadata()["error"])
        self.assert_last_good(before)

    def test_cancelled_rerun_keeps_last_good_and_persists_cancelled(self):
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        def cancelled(**kwargs):
            kwargs["cancel_event"].set()
            raise api.Cancelled()
        self.pipeline.side_effect = cancelled
        self.finish_run(sample_fps=9)
        self.assertEqual(self.metadata()["status"], "cancelled")
        self.assert_last_good(before)

    def test_failed_and_cancelled_dub_attempts_clean_partial_output_and_serve_last_good(self):
        old_local = self.job.work_dir / "dubbed.mp4"
        old_local.write_bytes(b"last good local dub")
        external = Path(self.temp.name) / "exported-dubs"
        external.mkdir()
        old_external = external / "source.mp4"
        old_external.write_bytes(b"last good exported dub")
        self.job.stats = {**self.job.stats, "dub_local": str(old_local), "dub_output": str(old_external)}
        Job.save(self.job)
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        attempts, names = [], []
        for outcome in ("cancelled", "error"):
            with self.subTest(outcome=outcome):
                def partial_pipeline(**kwargs):
                    attempt = kwargs["work_dir"]
                    params = kwargs["params"]
                    self.assertEqual(attempt.parent, self.job.work_dir / "attempts")
                    self.assertTrue(attempt.is_dir())
                    self.assertTrue(params.source_name.startswith(attempt.name + "_"))
                    self.assertTrue(params.source_name.endswith(self.job.filename))
                    self.assertNotEqual(external / (Path(params.source_name).stem + ".mp4"), old_external)
                    attempts.append(attempt)
                    names.append(params.source_name)
                    (attempt / "dubbed.mp4").write_bytes(b"partial new dub")
                    if outcome == "cancelled":
                        kwargs["cancel_event"].set()
                        raise api.Cancelled()
                    raise RuntimeError("dub pipeline failed after writing partial output")
                self.pipeline.side_effect = partial_pipeline
                self.finish_run(dub=True)
                self.assertEqual(self.metadata()["status"], outcome)
                self.assert_last_good(before)
                self.assertFalse(attempts[-1].exists(), "failed dub attempt directory remains")
                self.assertEqual(old_local.read_bytes(), b"last good local dub")
                self.assertEqual(old_external.read_bytes(), b"last good exported dub")
                response = self.client.get("/api/jobs/offline/dubbed")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.content, b"last good local dub")
                restored = JobStore(self.data).get("offline")
                self.assertEqual(restored.stats["dub_local"], str(old_local))
                self.assertEqual(restored.stats["dub_output"], str(old_external))
        self.assertEqual(len(set(attempts)), 2)
        self.assertEqual(len(set(names)), 2)

    def test_cancel_after_pipeline_return_does_not_publish_result(self):
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        def result_after_cancel(**kwargs):
            kwargs["cancel_event"].set()
            return {"cues": [cue("replacement")], "stats": {"new": True}}
        self.pipeline.side_effect = result_after_cancel
        self.finish_run()
        self.assertEqual(self.job.status, "cancelled")
        self.assert_last_good(before)

    def test_publish_save_failure_rolls_back_result_params_and_handoff(self):
        before = copy.deepcopy(self.job.snapshot(include_cues=True))
        self.pipeline.side_effect = None
        self.pipeline.return_value = {"cues": [cue("replacement")], "stats": {"new": True}}
        count = 0
        def fail_publish_only():
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("disk full publishing replacement")
            self.persist()
        self.save.side_effect = fail_publish_only
        self.finish_run(sample_fps=9)
        self.assertEqual(self.job.status, "error")
        self.assertIn("disk full", self.job.error)
        self.assert_last_good(before)

    def test_successful_rerun_publishes_new_result_and_latest_handoff(self):
        updated_handoff = remote(status="done", result={"output": "new-output.mp4"})
        def result(**kwargs):
            self.assertEqual(self.job.cues, [cue()])
            self.job.stats = {**self.job.stats, "handoff": updated_handoff}
            return {"cues": [cue("replacement", text="New")], "stats": {"new": True}}
        self.pipeline.side_effect = result
        self.finish_run(sample_fps=9)
        self.assertEqual(self.job.status, "done")
        persisted = self.metadata()["cues"]
        self.assertEqual(len(persisted), 1)
        fresh_id = persisted[0]["id"]
        self.assertIsInstance(fresh_id, str)
        self.assertTrue(fresh_id)
        self.assertNotIn(fresh_id, ("old", "replacement"))
        self.assertEqual(persisted, [cue(fresh_id, text="New")])
        self.assertEqual(self.job.cues, persisted)
        self.assertEqual(self.job.params["sample_fps"], 9)
        self.assertNotIn("old", self.job.params)
        stats = self.metadata()["stats"]
        self.assertTrue(stats["new"])
        self.assertEqual(stats["handoff"], updated_handoff)
        self.assertNotIn("old", stats)
        self.assertEqual(self.job.stats, stats)

    def test_missing_translation_follows_cue_id_after_reorder_reload_and_legacy_translate(self):
        self.job.cues = [cue("A", 1, 2, "Source A", "Translated A"),
                         cue("B", 3, 4, "Source B", ""),
                         cue("C", 5, 6, "Source C", "Manual C")]
        # Simulate metadata written before stable untranslated_ids existed.
        self.job.stats = {"untranslated_indices": [1], "untranslated_cues": 1, "kept": True}
        reordered = [dict(self.job.cues[2]), dict(self.job.cues[0]),
                     {**self.job.cues[1], "start": .5, "end": .9}]
        response = self.client.put("/api/jobs/offline/cues", json={"cues": reordered})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()["cues"]], ["B", "A", "C"])
        restored = JobStore(self.data).get("offline")
        self.assertEqual(restored.stats["untranslated_ids"], ["B"])
        self.assertEqual(restored.stats["untranslated_indices"], [0])
        self.assertEqual(restored.stats["untranslated_cues"], 1)
        self.assertEqual(restored.cues[2]["trans"], "Manual C")
        self.store.get.side_effect = lambda job_id: restored if job_id == "offline" else None
        def translate(items, **kwargs):
            self.assertEqual(items[0]["text"], "Source B")
            items[0]["trans"] = "Translated B"
            return {"total": 1}
        with patch.object(api, "translate_cues", side_effect=translate):
            translated = self.client.post("/api/jobs/offline/retranslate", json={"index": 0})
        self.assertEqual(translated.status_code, 200, translated.text)
        self.assertTrue(translated.json()["persisted"])
        final = JobStore(self.data).get("offline")
        self.assertEqual(final.cues[0]["id"], "B")
        self.assertEqual(final.cues[0]["trans"], "Translated B")
        self.assertEqual(final.cues[2]["trans"], "Manual C")
        self.assertEqual(final.stats["untranslated_ids"], [])
        self.assertEqual(final.stats["untranslated_indices"], [])
        self.assertEqual(final.stats["untranslated_cues"], 0)
        self.assertTrue(final.stats["kept"])

    def test_missing_translation_marks_new_and_edited_sources_but_preserves_exempt_blank(self):
        self.job.cues = [cue("A", 1, 2, "Exempt same-language source", ""),
                         cue("B", 3, 4, "Known missing source", ""),
                         cue("C", 5, 6, "Source C", "Manual C"),
                         cue("E", 7, 8, "Previously exempt source", "")]
        self.job.stats = {"untranslated_ids": ["B"], "untranslated_indices": [1], "untranslated_cues": 1}
        edited = copy.deepcopy(self.job.cues)
        edited[3]["text"] = "Edited source needs translation"
        edited += [cue("N", 9, 10, "New source", ""), cue("empty", 11, 12, "   ", "")]
        response = self.client.put("/api/jobs/offline/cues", json={"cues": edited})
        self.assertEqual(response.status_code, 200, response.text)
        stats = self.metadata()["stats"]
        self.assertEqual(stats["untranslated_ids"], ["B", "E", "N"])
        self.assertEqual(stats["untranslated_indices"], [1, 3, 4])
        self.assertEqual(stats["untranslated_cues"], 3)
        by_id = {item["id"]: item for item in self.job.cues}
        self.assertEqual(by_id["A"]["trans"], "")
        self.assertEqual(by_id["C"]["trans"], "Manual C")
        self.assertNotIn("empty", by_id)
        # Deleting B and manually translating E must prune both identities.
        changed = [dict(item) for item in self.job.cues if item["id"] != "B"]
        next(item for item in changed if item["id"] == "E")["trans"] = "Manual E"
        response = self.client.put("/api/jobs/offline/cues", json={"cues": changed})
        self.assertEqual(response.status_code, 200, response.text)
        restored = JobStore(self.data).get("offline")
        self.assertEqual(restored.stats["untranslated_ids"], ["N"])
        self.assertEqual(restored.stats["untranslated_indices"], [3])
        self.assertEqual(restored.stats["untranslated_cues"], 1)
        self.assertEqual(next(item for item in restored.cues if item["id"] == "C")["trans"], "Manual C")

    def test_legacy_exports_sort_all_variants_without_mutating_job(self):
        self.job.cues = [cue("late", 8, 9, "Late original", "Late translated"),
                         cue("early-long", 1, 3, "Long original", "Long translated"),
                         cue("early-short", .5, 2, "Short original", "Short translated")]
        before = copy.deepcopy(self.job.cues)
        for fmt in ("srt", "vtt"):
            for variant in ("original", "translated", "bilingual"):
                with self.subTest(format=fmt, variant=variant):
                    response = self.client.get("/api/jobs/offline/export", params={"format": fmt, "variant": variant})
                    self.assertEqual(response.status_code, 200, response.text)
                    text = response.text
                    suffix = "original" if variant == "original" else "translated"
                    positions = [text.index(label + " " + suffix) for label in ("Short", "Long", "Late")]
                    self.assertEqual(positions, sorted(positions))
                    self.assertIn("00:00:01", text)
                    if variant == "bilingual":
                        self.assertIn("Short translated\nShort original", text)
                    self.assertEqual(self.job.cues, before)
        self.save.assert_not_called()

    def test_handoff_reservation_covers_durable_save_and_duplicate_is_rejected(self):
        entered, release = threading.Event(), threading.Event()
        def delayed_save():
            if self.job.stats.get("handoff", {}).get("remote_job_id") == "new-remote":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("save gate timed out")
            self.persist()
        self.save.side_effect = delayed_save
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(api.send_handoff, "offline", api.HandoffBody())
            try:
                self.assertTrue(entered.wait(5), "first handoff never reached save")
                self.assertIn("offline", api._handoff_busy, "reservation released before durable save")
                second = pool.submit(api.send_handoff, "offline", api.HandoffBody())
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5)["ok"])
            with self.assertRaises(HTTPException) as failure:
                second.result(timeout=5)
            self.assertEqual(failure.exception.status_code, 409)
        self.import_remote.assert_called_once()
        self.assertNotIn("offline", api._handoff_busy)
        self.assertEqual(self.metadata()["stats"]["handoff"]["remote_job_id"], "new-remote")

    def test_stale_handoff_poll_cannot_overwrite_new_remote_id(self):
        self._stale_handoff_request(api.get_handoff, "status", {"status": "error", "result": "old"})

    def test_stale_handoff_cancel_cannot_overwrite_new_remote_id(self):
        self._stale_handoff_request(api.cancel_handoff, "cancel", {})

    def _stale_handoff_request(self, endpoint, remote_method, response):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("remote gate timed out")
            return response
        with patch.object(api.handoff, remote_method, side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(endpoint, "offline")
            try:
                self.assertTrue(entered.wait(5))
                current = api.send_handoff("offline", api.HandoffBody())["handoff"]
                saved_count = self.save.call_count
            finally:
                release.set()
            self.assertEqual(old.result(timeout=5)["handoff"], current)
        self.assertEqual(self.save.call_count, saved_count)
        self.assertEqual(self.metadata()["stats"]["handoff"], current)

    def test_handoff_result_only_change_is_saved_and_identical_poll_is_not(self):
        record = remote(result={"file": "first.mp4"})
        self.job.stats["handoff"] = record
        Job.save(self.job)
        state = {key: record[key] for key in ("status", "progress", "stage", "error", "result")}
        state["result"] = {"file": "second.mp4"}
        with patch.object(api.handoff, "status", return_value=state):
            api.get_handoff("offline")
            self.assertEqual(self.save.call_count, 1)
            self.assertEqual(self.metadata()["stats"]["handoff"]["result"], state["result"])
            api.get_handoff("offline")
            self.assertEqual(self.save.call_count, 1)

    def test_failed_handoff_intent_save_never_calls_remote_or_changes_prior_record(self):
        before = copy.deepcopy(self.job.stats)
        self.save.side_effect = OSError("handoff metadata unavailable")
        with self.assertRaises(OSError):
            api.send_handoff("offline", api.HandoffBody())
        self.assertEqual(self.job.stats, before)
        self.assertEqual(self.metadata()["stats"], before)
        self.assertNotIn("offline", api._handoff_busy)
        self.import_remote.assert_not_called()

    def test_json_overflow_numbers_are_client_errors_without_save_or_worker(self):
        cases = [("PUT", "/api/jobs/offline/cues", '{"cues":[{"start":1e309,"end":2,"text":"Source"}]}'),
                 ("POST", "/api/jobs/offline/start", '{"sample_fps":1e309}'),
                 ("POST", "/api/jobs/offline/start", '{"min_duration":-1e309}'),
                 ("POST", "/api/jobs/offline/start", '{"roi":[[0,0,1e309,1]]}'),
                 ("POST", "/api/jobs/offline/handoff", '{"original_volume_db":1e309}'),
                 ("POST", "/api/jobs/offline/handoff", '{"dubbing_volume_db":NaN}')]
        for method, path, body in cases:
            with self.subTest(path=path, body=body):
                response = self.client.request(method, path, content=body,
                                               headers={"Content-Type": "application/json"})
                self.assertIn(response.status_code, (400, 422), response.text)
                self.assertFalse(self.job.run_lock.locked())
        self.save.assert_not_called()
        self.pipeline.assert_not_called()
        self.import_remote.assert_not_called()

    def test_get_and_export_wait_for_failed_publish_then_return_last_good(self):
        entered, release = threading.Event(), threading.Event()
        reader_started = [threading.Event(), threading.Event()]
        reader_finished = [threading.Event(), threading.Event()]
        calls = 0
        def blocked_publish():
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("publish gate timed out")
                raise OSError("replacement metadata cannot be committed")
            self.persist()
        def read(index, callback):
            reader_started[index].set()
            try:
                return callback()
            finally:
                reader_finished[index].set()
        self.save.side_effect = blocked_publish
        self.pipeline.side_effect = None
        self.pipeline.return_value = {"cues": [cue("new", text="Uncommitted replacement")], "stats": {}}
        params = self.reserve_run()
        with ThreadPoolExecutor(max_workers=3) as pool:
            worker = pool.submit(api._run_job_thread, self.job, params)
            try:
                self.assertTrue(entered.wait(5))
                get = pool.submit(read, 0, lambda: api.get_job("offline"))
                export = pool.submit(read, 1, lambda: api.export("offline"))
                self.assertTrue(all(event.wait(5) for event in reader_started))
                self.assertFalse(reader_finished[0].wait(.1), "GET escaped before publish save completed")
                self.assertFalse(reader_finished[1].wait(.1), "export escaped before publish save completed")
            finally:
                release.set()
            worker.result(timeout=5)
            self.assertEqual(get.result(timeout=5)["cues"], [cue()])
            exported = export.result(timeout=5).body.decode("utf-8")
            self.assertIn("Original", exported)
            self.assertNotIn("Uncommitted replacement", exported)
        self.assertFalse(self.job.run_lock.locked())

    def test_handoff_poll_cannot_revert_cancellation_of_same_remote_attempt(self):
        self.job.stats["handoff"] = remote(status="queued", progress=0)
        entered, release = threading.Event(), threading.Event()
        def delayed_status(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("status gate timed out")
            return {"status": "queued", "progress": 0}
        with patch.object(api.handoff, "status", side_effect=delayed_status), \
                patch.object(api.handoff, "cancel", return_value={}), ThreadPoolExecutor(max_workers=1) as pool:
            poll = pool.submit(api.get_handoff, "offline")
            try:
                self.assertTrue(entered.wait(5))
                cancelling = api.cancel_handoff("offline")["handoff"]
                self.assertEqual(cancelling["status"], "cancelling")
                saves = self.save.call_count
            finally:
                release.set()
            self.assertEqual(poll.result(timeout=5)["handoff"], cancelling)
        self.assertEqual(self.save.call_count, saves)

    def test_handoff_cancel_cannot_revert_terminal_result_of_same_attempt(self):
        self.job.stats["handoff"] = remote(status="running", progress=50)
        entered, release = threading.Event(), threading.Event()
        def delayed_cancel(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("cancel gate timed out")
            return {}
        with patch.object(api.handoff, "cancel", side_effect=delayed_cancel), \
                patch.object(api.handoff, "status", return_value={"status": "done", "progress": 100,
                                                               "result": {"file": "complete.mp4"}}), \
                ThreadPoolExecutor(max_workers=1) as pool:
            cancel = pool.submit(api.cancel_handoff, "offline")
            try:
                self.assertTrue(entered.wait(5))
                complete = api.get_handoff("offline")["handoff"]
                saves = self.save.call_count
            finally:
                release.set()
            self.assertEqual(cancel.result(timeout=5)["handoff"], complete)
        self.assertEqual(self.save.call_count, saves)
        self.assertEqual(self.metadata()["stats"]["handoff"]["status"], "done")

    def test_handoff_helper_rolls_back_failed_poll_and_cancel_saves(self):
        for endpoint in (api.get_handoff, api.cancel_handoff):
            with self.subTest(endpoint=endpoint.__name__):
                self.job.stats["handoff"] = remote(status="running", progress=50)
                Job.save(self.job)
                before = copy.deepcopy(self.job.stats)
                self.save.side_effect = OSError("handoff update cannot save")
                with patch.object(api.handoff, "status", return_value={"status": "done", "result": "output"}), \
                        patch.object(api.handoff, "cancel", return_value={}):
                    with self.assertRaises(OSError):
                        endpoint("offline")
                self.assertEqual(self.job.stats, before)
                self.assertEqual(self.metadata()["stats"], before)

    def test_handoff_commit_failure_after_remote_creation_keeps_durable_reservation(self):
        original_save = Job.save
        calls = 0
        def save_except_final_record():
            nonlocal calls
            calls += 1
            if self.job.stats.get("handoff", {}).get("remote_job_id") == "new-remote":
                raise OSError("remote was created but local result cannot save")
            original_save(self.job)
        self.save.side_effect = save_except_final_record
        response = self.client.post("/api/jobs/offline/handoff", json={})
        self.assertGreaterEqual(response.status_code, 500)
        self.import_remote.assert_called_once()
        self.assertGreaterEqual(calls, 2, "remote call lacked a durable pre-send reservation")
        pending = self.metadata()["stats"]["handoff"]
        self.assertEqual(pending["status"], "queued")
        self.assertFalse(pending.get("remote_job_id"))
        self.assertEqual(self.job.stats["handoff"], pending)
        self.assertTrue(pending.get("attempt_id"))
        duplicate = self.client.post("/api/jobs/offline/handoff", json={})
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.import_remote.assert_called_once()
        reload = JobStore(self.data).get("offline")
        self.store.get.side_effect = lambda job_id: reload if job_id == "offline" else None
        duplicate_after_reload = self.client.post("/api/jobs/offline/handoff", json={})
        self.assertEqual(duplicate_after_reload.status_code, 409, duplicate_after_reload.text)
        self.import_remote.assert_called_once()

    def test_handoff_connection_refused_restores_prior_record_and_allows_retry(self):
        before = copy.deepcopy(self.job.stats)
        success = self.import_remote.return_value
        self.import_remote.side_effect = [
            api.handoff.HandoffError("connection refused before request", safe_to_retry=True), success,
        ]
        refused = self.client.post("/api/jobs/offline/handoff", json={})
        self.assertEqual(refused.status_code, 502, refused.text)
        self.assertEqual(self.job.stats, before)
        self.assertEqual(self.metadata()["stats"], before)
        self.assertNotIn("offline", api._handoff_busy)
        retry = self.client.post("/api/jobs/offline/handoff", json={})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(self.import_remote.call_count, 2)
        self.assertEqual(self.metadata()["stats"]["handoff"]["remote_job_id"], "new-remote")

    def test_failed_preview_rename_preserves_old_file_but_never_marks_ready(self):
        self.job.preview_path.write_bytes(b"untrusted old preview")
        replace = api.os.replace
        def fail_preview_replace(src, dst):
            if Path(dst) == self.job.preview_path:
                raise OSError("preview rename failed")
            return replace(src, dst)
        def encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"valid new preview")
        with patch.object(api.os, "replace", side_effect=fail_preview_replace), \
                self.assertLogs(api._log, level="WARNING"):
            self.encode_preview(encode)
        self.assertEqual(self.job.preview_path.read_bytes(), b"untrusted old preview")
        self.assertFalse(self.job.preview_ready)
        self.assertEqual(self.metadata()["preview_status"], "error")

    def test_failed_preview_marker_write_removes_partial_marker(self):
        write_text = Path.write_text
        def partial_marker(path, text, **kwargs):
            if path == self.job.preview_ready_path:
                write_text(path, "partial", **kwargs)
                raise OSError("marker flush failed")
            return write_text(path, text, **kwargs)
        def encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"valid preview")
        with patch.object(Path, "write_text", partial_marker), self.assertLogs(api._log, level="WARNING"):
            self.encode_preview(encode)
        self.assertFalse(self.job.preview_ready_path.exists())
        self.assertFalse(self.job.preview_ready)
        self.assertEqual(self.client.get("/api/jobs/offline/video?preview=1").status_code, 404)

    def test_preview_reserves_before_worker_and_retries_after_thread_start_failure(self):
        with patch.object(api.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("thread unavailable")
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                api.create_preview("offline")
            self.assertFalse(self.job.preview_lock.locked())
            self.assertEqual(self.job.preview_status, "none")
            thread.return_value.start.side_effect = None
            self.assertEqual(api.create_preview("offline"), {"status": "running"})
            try:
                self.assertTrue(self.job.preview_lock.locked())
                self.assertEqual(api.create_preview("offline"), {"status": "running"})
                self.assertEqual(thread.call_count, 2, "duplicate preview spawned a second encoder")
            finally:
                self.job.preview_lock.release()

    def test_partial_or_legacy_preview_is_not_served_or_ready_after_reload(self):
        self.job.preview_path.write_bytes(b"partial encoder output")
        self.job.preview_status = "ready"
        Job.save(self.job)
        self.assertFalse(self.job.preview_ready)
        self.assertNotEqual(self.job.snapshot()["preview_status"], "ready")
        response = self.client.get("/api/jobs/offline/video", params={"preview": 1})
        self.assertEqual(response.status_code, 404)
        restored = JobStore(self.data).get("offline")
        self.assertIsNotNone(restored)
        self.assertFalse(restored.preview_ready)
        self.assertNotEqual(restored.preview_status, "ready")
        with patch.object(api.threading, "Thread") as thread:
            self.assertEqual(api.create_preview("offline"), {"status": "running"})
            try:
                thread.return_value.start.assert_called_once()
            finally:
                self.job.preview_lock.release()

    def encode_preview(self, encoder, probe=None):
        self.job.preview_lock.acquire()
        self.job.preview_status = "running"
        with patch("subprocess.run", side_effect=encoder), patch.object(api, "probe_video", probe or self.probe):
            api._make_preview(self.job)
        self.assertFalse(self.job.preview_lock.locked())
        self.assertEqual(list(self.job.work_dir.glob(".preview-*.mp4")), [])

    def test_preview_encodes_to_temporary_then_publishes_marker_after_validation(self):
        def encode(cmd, **kwargs):
            output = Path(cmd[-1])
            self.assertNotEqual(output, self.job.preview_path)
            self.assertEqual(output.parent, self.job.work_dir)
            self.assertFalse(self.job.preview_ready_path.exists())
            self.assertEqual(self.client.get("/api/jobs/offline/video?preview=1").status_code, 404)
            output.write_bytes(b"validated preview")
            return SimpleNamespace(returncode=0)
        self.encode_preview(encode)
        self.assertTrue(self.job.preview_ready)
        self.assertEqual(self.job.preview_status, "ready")
        self.assertEqual(self.metadata()["preview_status"], "ready")
        self.assertTrue(JobStore(self.data).get("offline").preview_ready)
        response = self.client.get("/api/jobs/offline/video?preview=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"validated preview")
        with patch.object(api.threading, "Thread") as thread:
            self.assertEqual(api.create_preview("offline"), {"status": "ready"})
            thread.assert_not_called()

    def test_failed_preview_cleans_temporary_and_marker_and_can_retry(self):
        self.job.preview_path.write_bytes(b"untrusted old preview")
        self.job.preview_ready_path.write_text("old marker", encoding="utf-8")
        def fail(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"partial new output")
            raise subprocess.CalledProcessError(1, cmd, stderr=b"encode failed")
        with self.assertLogs(api._log, level="WARNING"):
            self.encode_preview(fail)
        self.assertFalse(self.job.preview_ready_path.exists())
        self.assertFalse(self.job.preview_ready)
        self.assertEqual(self.job.preview_status, "error")
        self.assertEqual(self.metadata()["preview_status"], "error")
        self.assertEqual(self.client.get("/api/jobs/offline/video?preview=1").status_code, 404)
        with patch.object(api.threading, "Thread") as thread:
            self.assertEqual(api.create_preview("offline"), {"status": "running"})
            try:
                thread.return_value.start.assert_called_once()
            finally:
                self.job.preview_lock.release()

    def test_invalid_preview_probe_does_not_publish_or_mark_ready(self):
        def encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"not actually a video")
        with self.assertLogs(api._log, level="WARNING"):
            self.encode_preview(encode, Mock(side_effect=api.FFmpegError("invalid generated video")))
        self.assertFalse(self.job.preview_ready)
        self.assertFalse(self.job.preview_path.exists())
        self.assertEqual(self.metadata()["preview_status"], "error")

    def test_preview_releases_lock_even_when_metadata_save_fails(self):
        self.save.side_effect = OSError("cannot save preview status")
        self.job.preview_lock.acquire()
        def encode(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"valid preview")
        with patch("subprocess.run", side_effect=encode):
            with self.assertRaisesRegex(OSError, "cannot save preview status"):
                api._make_preview(self.job)
        self.assertFalse(self.job.preview_lock.locked())
        self.assertEqual(list(self.job.work_dir.glob(".preview-*.mp4")), [])

    def upload(self, content=b"small fixture", filename="video.mp4"):
        file = UploadFile(filename=filename, file=io.BytesIO(content))
        return file, lambda: self.client.portal.call(api.create_job, file)

    def assert_upload_cleaned(self, file):
        self.assertTrue(file.file.closed, "uploaded spool was not closed")
        self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["offline"])
        self.store.add.assert_not_called()
        self.assertTrue(api._upload_lock.acquire(blocking=False), "upload reservation leaked")
        api._upload_lock.release()
        self.assertEqual(self.job.video_path.read_bytes(), b"offline video fixture")

    def assert_upload_error(self, file, invoke, status):
        with self.assertRaises(HTTPException) as failure:
            invoke()
        self.assertEqual(failure.exception.status_code, status)
        self.assert_upload_cleaned(file)

    def test_upload_publishes_only_after_source_and_metadata_are_durable(self):
        file, invoke = self.upload()
        def add(job):
            self.assertTrue((job.work_dir / "job.json").is_file())
            self.assertEqual(job.video_path.read_bytes(), b"small fixture")
            persisted = json.loads((job.work_dir / "job.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["id"], job.id)
            self.added.append(job)
        self.store.add.side_effect = add
        result = invoke()
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(self.added), 1)
        self.assertTrue(file.file.closed)
        self.assertFalse(api._upload_lock.locked())
        self.assertEqual(JobStore(self.data).get(result["id"]).filename, "video.mp4")

    def test_upload_empty_file_is_rejected_and_cleaned(self):
        file, invoke = self.upload(b"")
        self.assert_upload_error(file, invoke, 400)
        self.probe.assert_not_called()

    def test_upload_file_limit_is_rejected_and_cleaned(self):
        file, invoke = self.upload(b"x" * 1025)
        self.assert_upload_error(file, invoke, 413)
        self.probe.assert_not_called()

    def test_upload_existing_storage_quota_is_rejected_and_cleaned(self):
        file, invoke = self.upload()
        with patch.object(api, "MAX_DATA_BYTES", 1):
            self.assert_upload_error(file, invoke, 413)
        self.probe.assert_not_called()

    def test_upload_incoming_bytes_cannot_cross_storage_quota(self):
        stored = sum(path.stat().st_size for path in self.data.rglob("*") if path.is_file())
        file, invoke = self.upload(b"x" * 32)
        with patch.object(api, "MAX_DATA_BYTES", stored + 31):
            self.assert_upload_error(file, invoke, 413)
        self.probe.assert_not_called()

    def test_upload_reserves_free_space_floor_before_writing(self):
        file, invoke = self.upload(b"x" * 32)
        with patch.object(api.shutil, "disk_usage", return_value=SimpleNamespace(free=159)):
            self.assert_upload_error(file, invoke, 507)
        self.probe.assert_not_called()

    def test_upload_probe_failure_cleans_source_and_spool(self):
        self.probe.side_effect = api.FFmpegError("invalid upload video")
        file, invoke = self.upload()
        self.assert_upload_error(file, invoke, 400)

    def test_upload_metadata_save_failure_never_adds_job_and_cleans_source(self):
        file, invoke = self.upload()
        with patch.object(Job, "save", side_effect=OSError("metadata full")):
            self.assert_upload_error(file, invoke, 507)

    def test_upload_partial_write_failure_cleans_every_created_file(self):
        file, invoke = self.upload()
        original_open = Path.open
        class FailingWriter:
            def __init__(self, target):
                self.target = target
            def __enter__(self):
                return self
            def write(self, value):
                self.target.write(value[:2])
                self.target.flush()
                raise OSError("disk full after partial write")
            def __exit__(self, *args):
                self.target.close()
        def open_file(path, *args, **kwargs):
            target = original_open(path, *args, **kwargs)
            if args and args[0] == "wb" and path.parent != self.job.work_dir:
                return FailingWriter(target)
            return target
        with patch.object(Path, "open", open_file):
            self.assert_upload_error(file, invoke, 507)
        self.probe.assert_not_called()

    def test_upload_read_failure_cleans_and_releases_reservation(self):
        file, invoke = self.upload()
        async def failed_read(size):
            raise OSError("cannot read uploaded spool")
        file.read = failed_read
        self.assert_upload_error(file, invoke, 507)

    def test_upload_busy_closes_rejected_file_without_releasing_other_reservation(self):
        file, invoke = self.upload()
        api._upload_lock.acquire()
        try:
            with self.assertRaises(HTTPException) as failure:
                invoke()
            self.assertEqual(failure.exception.status_code, 429)
            self.assertTrue(file.file.closed)
            self.assertTrue(api._upload_lock.locked())
            self.store.add.assert_not_called()
        finally:
            api._upload_lock.release()

    def raw_upload(self, chunks, headers=()):
        messages, received = [], []
        async def request():
            remaining = iter(enumerate(chunks))
            async def receive():
                received.append(True)
                try:
                    index, body = next(remaining)
                except StopIteration:
                    raise AssertionError("request body was read before admission") from None
                return {"type": "http.request", "body": body, "more_body": index < len(chunks) - 1}
            async def send(message):
                messages.append(message)
            await api.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                           "method": "POST", "scheme": "http", "path": "/api/jobs", "raw_path": b"/api/jobs",
                           "query_string": b"", "root_path": "", "server": ("testserver", 80),
                           "client": ("testclient", 123),
                           "headers": [(b"host", b"testserver"),
                                       (b"content-type", b"multipart/form-data; boundary=offline"), *headers]},
                          receive, send)
        self.client.portal.call(request)
        return [message["status"] for message in messages if message["type"] == "http.response.start"], received

    def test_preparse_busy_upload_is_429_before_receive_or_spool(self):
        api._upload_lock.acquire()
        try:
            with patch("starlette.formparsers.SpooledTemporaryFile") as spool:
                statuses, received = self.raw_upload([], [(b"content-length", b"512")])
            self.assertEqual(statuses, [429])
            self.assertEqual(received, [])
            spool.assert_not_called()
            self.assertTrue(api._upload_lock.locked(), "rejected request released another upload's reservation")
            self.store.add.assert_not_called()
            self.probe.assert_not_called()
        finally:
            api._upload_lock.release()

    def test_preparse_low_temp_space_is_507_and_midchunk_failure_closes_spool(self):
        import starlette.formparsers
        # Available bytes exceed the floor, but cannot cover this declared body.
        with patch.object(api.shutil, "disk_usage", return_value=SimpleNamespace(free=api.MIN_FREE_BYTES + 511)), \
                patch("starlette.formparsers.SpooledTemporaryFile") as spool:
            statuses, received = self.raw_upload([], [(b"content-length", b"512")])
        self.assertEqual(statuses, [507])
        self.assertEqual(received, [])
        spool.assert_not_called()
        self.assertFalse(api._upload_lock.locked())
        prefix = (b'--offline\r\nContent-Disposition: form-data; name="file"; filename="fixture.mp4"\r\n'
                  b'Content-Type: video/mp4\r\n\r\n')
        made = []
        spool_factory = starlette.formparsers.SpooledTemporaryFile
        def tracked_spool(*args, **kwargs):
            result = spool_factory(*args, **kwargs)
            made.append(result)
            return result
        free = [SimpleNamespace(free=10 * 1024 * 1024), SimpleNamespace(free=10 * 1024 * 1024),
                SimpleNamespace(free=api.MIN_FREE_BYTES + 511)]
        try:
            with patch.object(api.shutil, "disk_usage", side_effect=free), \
                    patch("starlette.formparsers.SpooledTemporaryFile", side_effect=tracked_spool):
                statuses, received = self.raw_upload([prefix + b"a" * 512, b"b" * 512,
                                                       b"\r\n--offline--\r\n"],
                                                      [(b"transfer-encoding", b"chunked")])
            self.assertEqual(statuses, [507])
            self.assertEqual(len(received), 2)
            self.assertTrue(made, "fixture never created a multipart spool")
            self.assertTrue(all(spool.closed for spool in made), "disk-full parser spool remained open")
            self.assertFalse(api._upload_lock.locked())
            self.store.add.assert_not_called()
            self.probe.assert_not_called()
            self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["offline"])
        finally:
            for spool in made:
                spool.close()
        # A subsequent admitted ASGI upload must not trip the route's own lock.
        retry = self.client.post("/api/jobs", files={"file": ("retry.mp4", b"valid retry", "video/mp4")})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.store.add.assert_called_once()
        self.assertFalse(api._upload_lock.locked())

    def test_declared_oversized_body_is_413_before_multipart_parser(self):
        with patch("starlette.formparsers.SpooledTemporaryFile", side_effect=AssertionError("parser must not spool")):
            response = self.client.post("/api/jobs", content=b"unused", headers={
                "content-type": "multipart/form-data; boundary=offline",
                "content-length": str(api.MAX_UPLOAD_BYTES + 1024 * 1024 + 1),
            })
        self.assertEqual(response.status_code, 413, response.text)
        self.store.add.assert_not_called()
        self.probe.assert_not_called()
        self.assertFalse(api._upload_lock.locked())

    def test_chunked_oversized_body_is_413_and_closes_parser_spool(self):
        import starlette.formparsers
        made = []
        spool_factory = starlette.formparsers.SpooledTemporaryFile
        def tracked_spool(*args, **kwargs):
            result = spool_factory(*args, **kwargs)
            made.append(result)
            return result
        prefix = (b'--offline\r\nContent-Disposition: form-data; name="file"; filename="fixture.mp4"\r\n'
                  b'Content-Type: video/mp4\r\n\r\n')
        chunks = [prefix + b"a" * 600000, b"b" * 600000, b"\r\n--offline--\r\n"]
        messages = []
        async def request():
            remaining = iter(enumerate(chunks))
            async def receive():
                index, body = next(remaining)
                return {"type": "http.request", "body": body, "more_body": index < len(chunks) - 1}
            async def send(message):
                messages.append(message)
            await api.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                           "method": "POST", "scheme": "http", "path": "/api/jobs", "raw_path": b"/api/jobs",
                           "query_string": b"", "root_path": "", "server": ("testserver", 80),
                           "client": ("testclient", 123),
                           "headers": [(b"host", b"testserver"),
                                       (b"content-type", b"multipart/form-data; boundary=offline"),
                                       (b"transfer-encoding", b"chunked")]}, receive, send)
        try:
            with patch("starlette.formparsers.SpooledTemporaryFile", side_effect=tracked_spool):
                self.client.portal.call(request)
            statuses = [message["status"] for message in messages if message["type"] == "http.response.start"]
            self.assertEqual(statuses, [413])
            self.assertTrue(made, "fixture did not reach the multipart spool")
            self.assertTrue(all(spool.closed for spool in made), "oversized parser upload leaked an open spool")
            self.store.add.assert_not_called()
            self.probe.assert_not_called()
            self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["offline"])
        finally:
            for spool in made:
                spool.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
