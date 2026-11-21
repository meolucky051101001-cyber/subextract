"""Offline regression tests for Tool V1 pairing, handshake, and blur handoff."""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from fastapi.testclient import TestClient
from app import handoff
from app.jobs import Job

# Constructing a real JobStore at import can recover production jobs.
with patch("app.jobs.JobStore", return_value=Mock()):
    import app.main as api


BASE = "http://127.0.0.1:8088"
TOKEN = "offline-synthetic-token-one"
TOKEN_HEADER = "X-local-control-token"
INFO = {"ok": True, "version": 1}
STATUS_PATH = "/api/tool-v1/status"
SEND_PATH = "/api/jobs/offline/handoff"


def reply(value):
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def http_error(code, detail="Rejected"):
    return urllib.error.HTTPError(BASE, code, "Rejected", Message(), reply({"detail": detail}))


class IsolatedToolV1TestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="subextract-handoff-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.token_file = self.work / "control-token"
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        self.settings = {
            "tool_v1_url": BASE,
            "tool_v1_token_origin": BASE,
            "tool_v1_token_file": str(self.token_file),
        }
        self.opener = Mock()
        self.opener.open.side_effect = AssertionError("Unexpected Tool V1 request")
        self.mock_patch(handoff, "load_settings", return_value=self.settings)
        self.build_opener = self.mock_patch(urllib.request, "build_opener", return_value=self.opener)

    def mock_patch(self, obj, attr, *args, **kwargs):
        patcher = patch.object(obj, attr, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def guard_network(self):
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            patcher = patch(name, side_effect=AssertionError("Real network is forbidden"))
            self.addCleanup(patcher.stop)
            patcher.start()

    def requests(self):
        return [call.args[0] for call in self.opener.open.call_args_list]


class ToolV1ConnectionTests(IsolatedToolV1TestCase):
    def setUp(self):
        super().setUp()
        self.guard_network()

    def test_token_is_sent_on_paired_info_import_render_status_and_cancel(self):
        self.opener.open.side_effect = [reply(INFO), reply({"stem": "fixture"}),
                                        reply({"job_id": "remote"}), reply({"status": "running"}),
                                        reply({"status": "cancelling"})]
        base = "HTTP://127.0.0.1:08088/"
        self.assertEqual(handoff.info(base), INFO)
        handoff.import_and_render(base, {"stem": "fixture"})
        handoff.status(base, "remote")
        handoff.cancel(base, "remote")
        self.assertEqual([(req.selector, req.get_method(), req.get_header(TOKEN_HEADER))
                          for req in self.requests()], [
            ("/api/handoff/info", "GET", TOKEN),
            ("/api/handoff/import", "POST", TOKEN),
            ("/api/handoff/render", "POST", TOKEN),
            ("/api/handoff/status?job_id=remote", "GET", TOKEN),
            ("/api/handoff/cancel", "POST", TOKEN),
        ])

    def test_changing_preferred_url_does_not_change_credential_binding(self):
        other_origins = ("http://127.0.0.1:8090", "http://localhost:8088", "https://127.0.0.1:8088")
        self.opener.open.side_effect = [reply(INFO) for _ in range(len(other_origins) + 1)]
        for origin in other_origins:
            self.settings["tool_v1_url"] = origin
            handoff.info(origin)
        handoff.info(BASE)
        self.assertEqual([req.full_url for req in self.requests()],
                         [origin + "/api/handoff/info" for origin in (*other_origins, BASE)])
        self.assertEqual([req.get_header(TOKEN_HEADER) for req in self.requests()], [None, None, None, TOKEN])

    def test_unpaired_token_file_is_not_opened(self):
        self.settings.pop("tool_v1_token_origin")
        self.settings["tool_v1_token_file"] = str(self.work / "missing")
        self.opener.open.side_effect = [reply(INFO)]
        self.assertEqual(handoff.info(BASE), INFO)
        self.assertIsNone(self.requests()[0].get_header(TOKEN_HEADER))

    def test_rotation_reloads_token_from_disk(self):
        rotated = "offline-synthetic-token-two"
        self.opener.open.side_effect = [reply(INFO), reply(INFO)]
        handoff.info(BASE)
        self.token_file.write_text(rotated + "\n", encoding="utf-8")
        handoff.info(BASE)
        self.assertEqual([req.get_header(TOKEN_HEADER) for req in self.requests()], [TOKEN, rotated])

    def test_invalid_token_file_fails_before_transport(self):
        for contents in (None, b"short", b"synthetic token with internal spaces",
                         b"\xff" * 20, "synthetic-token-☃".encode("utf-8")):
            with self.subTest(contents=contents):
                if contents is None:
                    self.token_file.unlink()
                else:
                    self.token_file.write_bytes(contents)
                with self.assertRaises(handoff.HandoffError) as error:
                    handoff.info(BASE)
                self.assertTrue(error.exception.safe_to_retry)
        self.build_opener.assert_not_called()

    def test_malformed_info_is_rejected_without_posting(self):
        for info in (None, [], {}, {"ok": False, "version": 1}, {"ok": True, "version": 2},
                     {"ok": True, "version": "1"}, {"ok": True, "version": True}):
            with self.subTest(info=info):
                self.opener.open.reset_mock(side_effect=True)
                self.opener.open.side_effect = [reply(info)]
                with self.assertRaises(handoff.HandoffError):
                    handoff.connection(BASE)
                self.assertEqual([req.get_method() for req in self.requests()], ["GET"])

    def test_unexpected_successful_probe_response_is_a_connection_error(self):
        for value in ([], None, {"ok": True}):
            with self.subTest(value=value):
                self.opener.open.reset_mock(side_effect=True)
                self.opener.open.side_effect = [reply(INFO), reply(value)]
                with self.assertRaises(handoff.HandoffError):
                    handoff.connection(BASE)
                self.assertEqual([req.get_method() for req in self.requests()], ["GET", "POST"])

    def test_authenticated_404_probes_are_unique_and_read_only(self):
        self.opener.open.side_effect = [reply(INFO), http_error(404), reply(INFO), http_error(404)]
        self.assertEqual(handoff.connection(BASE), INFO)
        self.assertEqual(handoff.connection(BASE), INFO)
        info_one, probe_one, info_two, probe_two = self.requests()
        for req in (info_one, info_two):
            self.assertEqual((req.get_method(), req.selector), ("GET", "/api/handoff/info"))
        for req in (probe_one, probe_two):
            self.assertEqual(req.get_method(), "POST")
            self.assertRegex(req.selector, r"^/api/handoff/_connection_probe_[0-9a-f]{32}$")
            self.assertEqual(json.loads(req.data), {})
            self.assertEqual(req.get_header(TOKEN_HEADER), TOKEN)
        self.assertNotEqual(probe_one.selector, probe_two.selector)

    def test_import_denials_are_definite_and_redact_token(self):
        for code in (401, 403, 404):
            with self.subTest(code=code):
                self.opener.open.reset_mock(side_effect=True)
                self.opener.open.side_effect = [http_error(code, "Rejected token " + TOKEN)]
                with self.assertRaises(handoff.HandoffError) as error:
                    handoff.import_and_render(BASE, {"stem": "fixture"})
                self.assertTrue(error.exception.safe_to_retry)
                self.assertIn(f"({code})", str(error.exception))
                self.assertIn("[redacted]", str(error.exception))
                self.assertNotIn(TOKEN, str(error.exception))
                self.assertEqual([req.selector for req in self.requests()], ["/api/handoff/import"])


class ToolV1ApiTests(IsolatedToolV1TestCase):
    def setUp(self):
        super().setUp()
        self.job = Job(
            id="offline", work_dir=self.work, filename="fixture.mp4", video_path=self.work / "fixture.mp4",
            status="done", params={"translate_to": "vi"}, info={"width": 1280, "height": 720, "duration": 6},
            cues=[
                {"id": "one", "text": "First", "trans": "Một", "start": 1, "end": 2, "region": 0},
                {"id": "two", "text": "Second", "trans": "Hai", "start": 3, "end": 4, "region": 1},
            ],
            stats={"rois": [[.1, .2, .3, .4], [.4, .5, .2, .3]]},
        )
        self.saved = []
        self.job.save = Mock(side_effect=lambda: self.saved.append(copy.deepcopy(self.job.stats)))
        self.mock_patch(api, "store", Mock(get=Mock(return_value=self.job)))
        self.mock_patch(api, "load_settings", return_value=self.settings)
        self.mock_patch(api, "_handoff_busy", set())
        self.mock_patch(api, "_handoff_recovery", {})
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # TestClient starts the Windows event loop's internal socket pair on entry.
        self.guard_network()

    def test_status_reports_canonical_dashboard_after_authorized_probe(self):
        self.settings["tool_v1_url"] = "127.0.0.1:8088/"
        self.opener.open.side_effect = [reply(INFO), http_error(404)]
        response = self.client.get(STATUS_PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"ok": True, "authorized": True, "base_url": BASE,
                                          "dashboard_url": BASE + "/", "version": 1})
        self.assertEqual([req.get_method() for req in self.requests()], ["GET", "POST"])
        self.job.save.assert_not_called()

    def test_status_reports_offline_without_posting(self):
        self.opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError("offline fixture"))
        response = self.client.get(STATUS_PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], False)
        self.assertEqual(response.json()["base_url"], BASE)
        self.assertTrue(response.json()["detail"])
        self.assertEqual([req.get_method() for req in self.requests()], ["GET"])
        self.job.save.assert_not_called()

    def test_status_reports_malformed_info_without_posting(self):
        self.opener.open.side_effect = [io.BytesIO(b"not JSON")]
        response = self.client.get(STATUS_PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], False)
        self.assertTrue(response.json()["detail"])
        self.assertEqual([req.get_method() for req in self.requests()], ["GET"])
        self.job.save.assert_not_called()

    def test_status_reports_probe_403_even_after_successful_info(self):
        self.opener.open.side_effect = [reply(INFO), http_error(403, "Rejected token " + TOKEN)]
        response = self.client.get(STATUS_PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], False)
        self.assertIn("403", response.json()["detail"])
        self.assertIn("[redacted]", response.json()["detail"])
        self.assertNotIn(TOKEN, response.text)
        self.assertEqual([req.get_method() for req in self.requests()], ["GET", "POST"])
        self.job.save.assert_not_called()

    def assert_blur_handoff(self, body, expected_radius):
        def open_remote(request, timeout):
            self.assertEqual(len(self.saved), 1, "send intent must be saved before remote calls")
            intent = self.saved[0]["handoff"]
            self.assertEqual(intent["blur_radius"], expected_radius)
            self.assertEqual(intent["sub_cover_style"], "localized_blur")
            self.assertIsNone(intent["remote_job_id"])
            if request.selector == "/api/handoff/import":
                return reply({"stem": "imported-fixture"})
            self.assertEqual(request.selector, "/api/handoff/render")
            return reply({"job_id": "remote", "status": "queued", "queue_position": 3})

        self.opener.open.side_effect = open_remote
        response = self.client.post(SEND_PATH, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([req.selector for req in self.requests()], ["/api/handoff/import", "/api/handoff/render"])
        imported, rendered = [json.loads(req.data) for req in self.requests()]
        self.assertEqual([cue["box"] for cue in imported["cues"]], [[.1, .2, .4, .6], [.4, .5, .6, .8]])
        self.assertEqual(rendered, {"stem": "imported-fixture", "sub_cover_style": "localized_blur",
                                    "sub_blur_radius": expected_radius, "sub_blur_padding_x": .015,
                                    "sub_blur_padding_y": .008})
        record = response.json()["handoff"]
        self.assertEqual(len(self.saved), 2)
        self.assertEqual(record, self.saved[1]["handoff"])
        self.assertEqual((record["status"], record["queue_position"]), ("queued", 3))
        self.assertEqual((record["remote_job_id"], record["sub_cover_style"], record["blur_radius"]),
                         ("remote", "localized_blur", expected_radius))

    def test_handoff_defaults_to_localized_blur_with_radius_16(self):
        self.assert_blur_handoff({}, 16)

    def test_handoff_applies_requested_radius_24_and_forces_localized_blur(self):
        self.assert_blur_handoff({"blur_radius": 24, "sub_cover_style": "solid"}, 24)

    def test_out_of_range_and_nonfinite_radius_fail_validation_without_effects(self):
        before = copy.deepcopy(self.job.stats)
        for radius in (1, 33, float("nan"), float("inf")):
            with self.subTest(radius=radius):
                response = self.client.post(SEND_PATH, content=json.dumps({"blur_radius": radius}),
                                            headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"][0]["loc"], ["body", "blur_radius"])
        self.assertEqual(self.job.stats, before)
        self.assertNotIn(self.job.id, api._handoff_busy)
        self.job.save.assert_not_called()
        self.opener.open.assert_not_called()

    def test_import_403_clears_active_intent_and_allows_retry(self):
        self.job.stats["handoff"] = {"remote_job_id": "prior-render", "status": "done"}
        before = copy.deepcopy(self.job.stats)
        self.opener.open.side_effect = [http_error(403, "Rejected token " + TOKEN)]
        failed = self.client.post(SEND_PATH, json={})
        self.assertEqual(failed.status_code, 502, failed.text)
        self.assertIn("403", failed.json()["detail"])
        self.assertNotIn(TOKEN, failed.text)
        self.assertEqual(self.saved[0]["handoff"]["status"], "queued")
        self.assertEqual(self.saved[1], before)
        self.assertEqual(self.job.stats, before)
        self.assertNotIn(self.job.id, api._handoff_busy)
        self.assertEqual([req.selector for req in self.requests()], ["/api/handoff/import"])

        self.opener.open.side_effect = [reply({"stem": "fixture"}), reply({"job_id": "retry-render"})]
        retried = self.client.post(SEND_PATH, json={})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["handoff"]["remote_job_id"], "retry-render")

    def test_full_queue_rolls_back_intent_and_allows_retry(self):
        self.opener.open.side_effect = [reply({"stem": "fixture"}), http_error(429, "Queue full")]
        failed = self.client.post(SEND_PATH, json={})
        self.assertEqual(failed.status_code, 429, failed.text)
        self.assertNotIn("handoff", self.job.stats)
        self.opener.open.side_effect = [reply({"stem": "new-fixture"}), reply({"job_id": "retry"})]
        self.assertEqual(self.client.post(SEND_PATH, json={}).status_code, 200)

    def test_queue_position_decreases_and_disappears_after_cancel(self):
        self.opener.open.side_effect = [
            reply({"stem": "fixture"}), reply({"job_id": "remote", "queue_position": 3}),
            reply({"status": "queued", "queue_position": 1}), reply({"status": "cancelled"}),
        ]
        self.assertEqual(self.client.post(SEND_PATH, json={}).status_code, 200)
        updated = self.client.get(SEND_PATH).json()["handoff"]
        self.assertEqual(updated["queue_position"], 1)
        cancelled = self.client.post(SEND_PATH + "/cancel").json()["handoff"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn("queue_position", cancelled)

    def test_render_403_keeps_intent_and_blocks_duplicate_import(self):
        self.opener.open.side_effect = [reply({"stem": "fixture"}), http_error(403, "Rejected token " + TOKEN)]
        failed = self.client.post(SEND_PATH, json={})
        self.assertEqual(failed.status_code, 502, failed.text)
        self.assertIn("403", failed.json()["detail"])
        self.assertNotIn(TOKEN, failed.text)
        record = self.saved[-1]["handoff"]
        self.assertEqual(record["status"], "queued")
        self.assertIsNone(record["remote_job_id"])
        self.assertNotIn(TOKEN, record["error"])
        self.assertNotIn(self.job.id, api._handoff_busy)
        self.assertEqual(self.client.post(SEND_PATH, json={}).status_code, 409)
        self.assertEqual([req.selector for req in self.requests()], ["/api/handoff/import", "/api/handoff/render"])


if __name__ == "__main__":
    unittest.main()
