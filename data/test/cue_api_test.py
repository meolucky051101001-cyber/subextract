"""Offline cue API regressions: no listener, network, OCR, or production data writes.

Run with Python 3.10: python -B data/test/cue_api_test.py
"""
from __future__ import annotations

import copy
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from fastapi.testclient import TestClient
from app.jobs import Job

# Importing the real store can recover and save interrupted production jobs.
# Replace its constructor before importing the application.
with patch("app.jobs.JobStore", return_value=Mock()):
    import app.main as api


def cue(cue_id="a", text="Source", start=1.0, end=2.0, trans="old", region=0):
    return {"id": cue_id, "start": start, "end": end, "text": text,
            "trans": trans, "region": region}


class CueApiRegression(unittest.TestCase):
    def setUp(self):
        self.job = Job(id="offline", work_dir=ROOT / "unused-offline-job",
                       filename="source.mp4", video_path=ROOT / "unused-source.mp4",
                       status="done", cues=[cue(), cue("b", "Other", 3, 4)],
                       params={"translate_to": "en"}, stats={"kept": True})
        self.saved = []
        self.save = Mock(side_effect=lambda: self.saved.append(copy.deepcopy(self.job.cues)))
        self.job.save = self.save
        self.store = Mock()
        self.store.get.side_effect = lambda job_id: self.job if job_id == self.job.id else None
        self.translation = Mock(side_effect=self.translate)
        for patcher in (
            patch.object(api, "store", self.store),
            patch.object(api, "get_gemini_keys", return_value=["offline-fake-key"]),
            patch.object(api, "load_settings", return_value={"glossary": [{"src": "A", "dst": "B"}]}),
            patch.object(api, "translate_cues", self.translation),
            patch.object(api, "run_pipeline", side_effect=AssertionError("Unexpected OCR")),
            patch.object(api, "gpu_info", return_value={"available": False}),
            patch.object(Path, "write_text", side_effect=AssertionError("Data writes are forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app, raise_server_exceptions=False)
        # Windows creates an internal socketpair when starting an asyncio loop.
        # Start this in-process ASGI portal first, then forbid all connections.
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        for patcher in (
            patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")),
            patch("socket.socket.connect_ex", side_effect=AssertionError("Network is forbidden")),
            patch("socket.create_connection", side_effect=AssertionError("Network is forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def translate(cues, **kwargs):
        cues[0]["trans"] = "  Translation  "
        return {"total": 1}

    def post(self, **body):
        return self.client.post("/api/jobs/offline/retranslate", json=body)

    def put(self, cues):
        return self.client.put("/api/jobs/offline/cues", json={"cues": cues})

    def assert_error(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        self.assertTrue(response.json()["detail"])

    def test_put_preserves_ids_across_sort_and_normalizes_cues(self):
        response = self.put([cue("b", " Later ", 3.12349, 2, " T ", 1),
                             cue("a", " First ", -1, -.5), cue("blank", "  ")])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["count"], 2)
        self.assertEqual([c["id"] for c in result["cues"]], ["a", "b"])
        self.assertEqual(result["cues"][0]["start"], 0)
        self.assertEqual(result["cues"][0]["end"], .05)
        self.assertEqual(result["cues"][1], cue("b", "Later", 3.123, 3.173, "T", 1))
        self.assertEqual(self.saved, [result["cues"]])
        self.assertEqual(self.client.get("/api/jobs/offline").json()["cues"], result["cues"])

    def test_put_generates_unique_ids_for_legacy_duplicate_and_invalid_ids(self):
        legacy = cue()
        legacy.pop("id")
        response = self.put([cue("kept"), cue("kept", start=3), legacy,
                             cue("  ", start=4), cue("x" * 129, start=5)])
        self.assertEqual(response.status_code, 200, response.text)
        ids = [c["id"] for c in response.json()["cues"]]
        self.assertEqual(ids[0], "kept")
        self.assertEqual(len(set(ids)), 5)
        self.assertTrue(all(0 < len(value) <= 128 for value in ids))

    def test_put_rolls_back_memory_when_save_fails(self):
        previous = self.job.cues
        self.save.side_effect = OSError("disk full")
        self.assertEqual(self.put([cue("new")]).status_code, 500)
        self.assertIs(self.job.cues, previous)
        self.assertEqual(self.job.cues[0]["trans"], "old")

    def test_legacy_index_persists_by_default_without_changing_source(self):
        self.job.cues[0].pop("id")
        response = self.post(index=0, text="  Override source  ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "index": 0, "cue_id": None,
                                          "trans": "Translation", "persisted": True})
        self.assertEqual(self.job.cues[0]["trans"], "Translation")
        self.assertEqual(self.job.cues[0]["text"], "Source")
        args, kwargs = self.translation.call_args
        self.assertEqual(args[0][0]["text"], "Override source")
        self.assertEqual(kwargs["target"], "en")
        self.assertEqual(kwargs["glossary"], [{"src": "A", "dst": "B"}])
        self.save.assert_called_once_with()

    def test_cue_id_is_authoritative_even_with_stale_index(self):
        response = self.post(cue_id=" b ", index=0, target=" VI ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["index"], 1)
        self.assertEqual(response.json()["cue_id"], "b")
        self.assertEqual(self.job.cues[0]["trans"], "old")
        self.assertEqual(self.job.cues[1]["trans"], "Translation")
        self.assertEqual(self.translation.call_args.kwargs["target"], "vi")

    def test_preview_returns_translation_without_mutation_or_save(self):
        original = self.job.cues
        before = copy.deepcopy(original)
        self.save.side_effect = AssertionError("Preview must not save")
        response = self.post(cue_id="a", persist=False, text=" Source ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "index": 0, "cue_id": "a",
                                          "trans": "Translation", "persisted": False})
        self.assertIs(self.job.cues, original)
        self.assertEqual(self.job.cues, before)
        self.save.assert_not_called()

    def test_preview_does_not_overwrite_a_put_during_translation(self):
        def translate(cues, **kwargs):
            self.assertEqual(self.put([cue("a", "Edited", trans="manual")]).status_code, 200)
            return self.translate(cues, **kwargs)
        self.translation.side_effect = translate
        response = self.post(cue_id="a", persist=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["persisted"])
        self.assertEqual(self.job.cues, [cue("a", "Edited", trans="manual")])
        self.save.assert_called_once_with()

    def test_invalid_selectors_are_400_without_translation(self):
        for body in ({}, {"index": -1}, {"index": 2}, {"cue_id": "missing"},
                     {"cue_id": "", "index": 0}, {"cue_id": "missing", "index": 0}):
            with self.subTest(body=body):
                self.assert_error(self.post(**body), 400)
        self.job.cues[1]["id"] = "a"
        self.assert_error(self.post(cue_id="a"), 400)
        self.translation.assert_not_called()
        self.save.assert_not_called()

    def test_missing_job_empty_cues_text_target_and_key_errors(self):
        self.assert_error(self.client.post("/api/jobs/missing/retranslate", json={"index": 0}), 404)
        self.assert_error(self.post(index=0, text="  "), 400)
        self.assert_error(self.post(index=0, target="xx"), 400)
        self.job.params = {}
        self.assert_error(self.post(index=0), 400)
        with patch.object(api, "get_gemini_keys", return_value=[]):
            self.assert_error(self.post(index=0, target="en"), 400)
        self.job.cues = []
        self.assert_error(self.post(index=0, target="en"), 400)
        self.translation.assert_not_called()
        self.save.assert_not_called()

    def test_translation_failures_do_not_persist(self):
        for result, status in (({"total": 0}, 400), ({"total": 1, "error": "Mock failure"}, 502)):
            with self.subTest(result=result):
                self.translation.side_effect = lambda *args, **kwargs: result
                self.assert_error(self.post(index=0), status)
        self.assertEqual(self.job.cues[0]["trans"], "old")
        self.save.assert_not_called()

    def test_retranslate_save_failure_rolls_back_original_cue_object(self):
        previous = self.job.cues[0]
        self.save.side_effect = OSError("disk full")
        self.assertEqual(self.post(index=0).status_code, 500)
        self.assertIs(self.job.cues[0], previous)
        self.assertEqual(previous["trans"], "old")

    def test_any_put_during_legacy_translation_is_stale_even_if_equal(self):
        def translate(cues, **kwargs):
            self.assertEqual(self.put(copy.deepcopy(self.job.cues)).status_code, 200)
            return self.translate(cues, **kwargs)
        self.translation.side_effect = translate
        self.assert_error(self.post(index=0), 409)
        self.assertEqual(self.job.cues[0]["trans"], "old")
        self.save.assert_called_once_with()

    def test_id_translation_is_stale_after_put_reorders_or_deletes(self):
        for replacement in ([cue("b", start=0), cue("a", start=3)], [cue("b")]):
            with self.subTest(replacement=replacement):
                self.job.cues = [cue(), cue("b", "Other", 3, 4)]
                self.save.reset_mock()
                def translate(cues, **kwargs):
                    self.assertEqual(self.put(replacement).status_code, 200)
                    return self.translate(cues, **kwargs)
                self.translation.side_effect = translate
                self.assert_error(self.post(cue_id="a"), 409)
                self.assertTrue(all(c["trans"] == "old" for c in self.job.cues))
                self.save.assert_called_once_with()

    def test_in_place_edit_or_other_translation_wins_over_pending_result(self):
        for field, value in (("text", "edit"), ("trans", "manual"), ("start", 9)):
            with self.subTest(field=field):
                self.job.cues = [cue()]
                def translate(cues, **kwargs):
                    with api._cue_write_lock:
                        self.job.cues[0][field] = value
                    return self.translate(cues, **kwargs)
                self.translation.side_effect = translate
                self.assert_error(self.post(cue_id="a"), 409)
                self.assertEqual(self.job.cues[0][field], value)
        self.save.assert_not_called()

    def test_concurrent_put_commits_while_gemini_is_pending(self):
        entered, release = threading.Event(), threading.Event()
        def translate(cues, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Translation was not released")
            return self.translate(cues, **kwargs)
        self.translation.side_effect = translate
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.retranslate_cue, "offline", api.RetranslateBody(cue_id="a"))
            try:
                self.assertTrue(entered.wait(5), "Translation did not start")
                api.update_cues("offline", api.CuesBody(cues=[cue("a", "Edited", trans="manual")]))
            finally:
                release.set()
            with self.assertRaises(api.HTTPException) as raised:
                pending.result(timeout=5)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.job.cues[0]["trans"], "manual")
        self.save.assert_called_once_with()

    def test_put_waits_for_translation_commit_and_save(self):
        saving, release, put_started, second_save = (threading.Event() for _ in range(4))
        saved = 0
        def save():
            nonlocal saved
            saved += 1
            if saved == 1:
                saving.set()
                if not release.wait(5):
                    raise AssertionError("Save was not released")
            else:
                second_save.set()
        self.save.side_effect = save
        def put():
            put_started.set()
            return api.update_cues("offline", api.CuesBody(cues=[cue("a", "Edited", trans="manual")]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            translation = pool.submit(api.retranslate_cue, "offline", api.RetranslateBody(cue_id="a"))
            pending_put = None
            try:
                self.assertTrue(saving.wait(5), "Commit did not reach save")
                pending_put = pool.submit(put)
                self.assertTrue(put_started.wait(5), "PUT did not start")
                self.assertFalse(second_save.wait(.05), "PUT entered before translation save finished")
                self.assertFalse(pending_put.done())
            finally:
                release.set()
            self.assertTrue(translation.result(timeout=5)["persisted"])
            self.assertTrue(pending_put.result(timeout=5)["ok"])
        self.assertTrue(second_save.is_set())
        self.assertEqual(self.job.cues[0]["text"], "Edited")
        self.assertEqual(self.job.cues[0]["trans"], "manual")

    def test_running_and_reserved_jobs_reject_put_and_translation(self):
        before = copy.deepcopy(self.job.cues)
        self.job.status = "running"
        self.assert_error(self.put([cue("new")]), 409)
        self.assert_error(self.post(index=0), 409)
        self.job.status = "done"
        self.job.run_lock.acquire()
        try:
            self.assert_error(self.put([cue("new")]), 409)
            self.assert_error(self.post(index=0, persist=False), 409)
        finally:
            self.job.run_lock.release()
        self.assertEqual(self.job.cues, before)
        self.translation.assert_not_called()
        self.save.assert_not_called()

    def test_start_reserves_before_worker_runs_and_duplicate_start_keeps_state(self):
        with patch.object(api.threading, "Thread") as thread:
            response = self.client.post("/api/jobs/offline/start", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job"]["status"], "running")
            self.assertTrue(self.job.run_lock.locked())
            self.assertEqual(self.job.cues, [])
            params, stats, cues = self.job.params, self.job.stats, self.job.cues
            try:
                self.assert_error(self.client.post("/api/jobs/offline/start", json={"sample_fps": 9}), 409)
                self.assertIs(self.job.params, params)
                self.assertIs(self.job.stats, stats)
                self.assertIs(self.job.cues, cues)
                self.assert_error(self.put([cue("new")]), 409)
                thread.assert_called_once()
            finally:
                self.job.run_lock.release()

    def test_start_guard_does_not_clear_cues_if_lock_is_already_reserved(self):
        before = self.job.cues
        self.job.run_lock.acquire()
        try:
            self.assert_error(self.client.post("/api/jobs/offline/start", json={}), 409)
        finally:
            self.job.run_lock.release()
        self.assertIs(self.job.cues, before)
        self.assertEqual(self.job.stats, {"kept": True})

    def test_start_rechecks_guard_after_parameter_validation(self):
        original = self.job.cues
        reads = 0
        def settings():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.job.run_lock.acquire()
            return {}
        with patch.object(api, "load_settings", side_effect=settings):
            try:
                self.assert_error(self.client.post("/api/jobs/offline/start", json={}), 409)
                self.assertEqual(reads, 2)
            finally:
                if self.job.run_lock.locked():
                    self.job.run_lock.release()
        self.assertIs(self.job.cues, original)

    def test_start_thread_failure_restores_prior_job_and_cancel_flag(self):
        previous = self.job.snapshot(include_cues=True)
        original = self.job.cues
        self.job.cancel_event.set()
        with patch.object(api.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("Cannot start thread")
            self.assertEqual(self.client.post("/api/jobs/offline/start", json={}).status_code, 500)
        self.assertEqual(self.job.snapshot(include_cues=True), previous)
        self.assertIs(self.job.cues, original)
        self.assertTrue(self.job.cancel_event.is_set())
        self.assertFalse(self.job.run_lock.locked())

    def test_rerun_while_translation_pending_rejects_late_commit(self):
        def translate(cues, **kwargs):
            with patch.object(api.threading, "Thread"):
                started = api.start_job("offline", api.StartBody())
            self.assertTrue(started["ok"])
            return self.translate(cues, **kwargs)
        self.translation.side_effect = translate
        try:
            self.assert_error(self.post(cue_id="a"), 409)
            self.assertEqual(self.job.cues, [])
            self.save.assert_not_called()
        finally:
            if self.job.run_lock.locked():
                self.job.run_lock.release()

    def test_worker_releases_run_lock_even_if_final_save_fails(self):
        self.job.run_lock.acquire()
        self.save.side_effect = [None, OSError("disk full on final save")]
        with patch.object(api, "run_pipeline", return_value={"cues": [cue("new")], "stats": {}}):
            with self.assertRaisesRegex(OSError, "final save"):
                api._run_job_thread(self.job, api.RunParams())
        self.assertFalse(self.job.run_lock.locked())
        self.assertEqual(self.job.status, "done")

    def test_worker_releases_run_lock_when_initial_save_fails(self):
        self.job.run_lock.acquire()
        self.save.side_effect = [OSError("disk full on initial save"), None]
        with patch.object(Path, "write_text") as log:
            api._run_job_thread(self.job, api.RunParams())
        self.assertFalse(self.job.run_lock.locked())
        self.assertEqual(self.job.status, "error")
        self.assertIn("initial save", self.job.error)
        log.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
