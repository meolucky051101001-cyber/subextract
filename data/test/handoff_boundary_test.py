"""Offline tests for the Tool V1 origin and HTTP request policy."""
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app import handoff


class ResolveUrlTests(unittest.TestCase):
    def test_default_and_valid_loopback_origins(self):
        cases = [
            (None, handoff.DEFAULT_TOOL_V1_URL),
            ("", handoff.DEFAULT_TOOL_V1_URL),
            ("   ", handoff.DEFAULT_TOOL_V1_URL),
            ("localhost:8088", "http://localhost:8088"),
            ("https://localhost", "https://localhost"),
            (" HTTP://LOCALHOST:08088/ ", "http://localhost:8088"),
            ("127.0.0.1:1", "http://127.0.0.1:1"),
            ("http://127.8.9.10:65535", "http://127.8.9.10:65535"),
            ("[::1]:8088", "http://[::1]:8088"),
            ("https://[0:0:0:0:0:0:0:1]:8088/", "https://[::1]:8088"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(handoff.resolve_url(value), expected)
                self.assertEqual(handoff.resolve_url(expected), expected)

    def test_rejects_non_loopback_or_ambiguous_hosts(self):
        cases = (
            "http://127.0.0.1.example.invalid:8088",
            "http://localhost.example.invalid:8088",
            "http://notlocalhost:8088", "http://localhost.:8088",
            "http://192.0.2.5:8088", "http://[2001:db8::1]:8088",
            "http://[::]:8088", "http://127.000.000.001:8088",
            "http://127.1:8088", "http://2130706433:8088",
            "http://[::1]suffix:8088", "http://[::1", "http://::1:8088",
            "http://[::1%25scope]:8088", "http://local\nhost:8088",
            "http://local host:8088", "http://localhost\\", "http://%6cocalhost:8088",
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(handoff.HandoffError):
                    handoff.resolve_url(value)

    def test_rejects_invalid_authority_ports_and_non_origin_parts(self):
        cases = (
            False, 42, "http://", "//localhost:8088", "ftp://localhost:8088",
            "http://user:pass@localhost:8088", "http://@localhost:8088",
            "http://localhost:8088/api", "http://localhost:8088//",
            "http://localhost:8088?x=1", "http://localhost:8088?",
            "http://localhost:8088/#section", "http://localhost:8088/#",
            "http://localhost:", "http://[::1]:", "http://localhost:0",
            "http://localhost:65536", "http://localhost:+80",
            "http://localhost:-80", "http://localhost:９９", "http://localhost:web",
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(handoff.HandoffError):
                    handoff.resolve_url(value)


class HandoffHttpTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.create_connection", side_effect=AssertionError("No network allowed"))
        network.start()
        self.addCleanup(network.stop)
        proxies = patch("urllib.request.getproxies", side_effect=AssertionError("No ambient proxies"))
        proxies.start()
        self.addCleanup(proxies.stop)

    def test_public_requests_reject_invalid_origin_before_opening(self):
        bad_origin = "https://localhost.example.invalid"
        with patch.object(urllib.request, "build_opener") as build:
            for call in (handoff.info, lambda base: handoff.import_and_render(base, {}),
                         lambda base: handoff.status(base, "test-id"),
                         lambda base: handoff.cancel(base, "test-id")):
                with self.assertRaises(handoff.HandoffError):
                    call(bad_origin)
            build.assert_not_called()

    def test_mocked_opener_preserves_requests_and_ignores_ambient_proxies(self):
        job = SimpleNamespace(
            id="source-id", filename="sample.mp4", video_path=Path("fixture.mp4"),
            stats={}, info={"width": 1920, "height": 1080, "duration": 4.5},
            params={}, cues=[{"text": "字幕", "trans": "Nhãn", "start": 0.5, "end": 1.5}],
        )
        payload = handoff.build_payload(job)
        opener = Mock()
        replies = [
            {"ok": True}, {"stem": "sample"}, {"job_id": "remote-id"},
            {"status": "done"}, {"status": "cancelling"},
        ]
        opener.open.side_effect = [io.BytesIO(json.dumps(reply).encode()) for reply in replies]
        with patch.object(urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(handoff.info("localhost:8088"), replies[0])
            self.assertEqual(handoff.import_and_render(
                "https://[::1]:8088/", payload, {"voice": "test-voice", "skip": None},
            ), {"imported": replies[1], "render": replies[2], "base_url": "https://[::1]:8088"})
            self.assertEqual(handoff.status("localhost:8088", "a/b?c+d"), replies[3])
            self.assertEqual(handoff.cancel("localhost:8088", "remote-id"), replies[4])

        self.assertEqual(build.call_count, 5)
        for call in build.call_args_list:
            self.assertIsInstance(call.args[0], urllib.request.ProxyHandler)
            self.assertEqual(call.args[0].proxies, {})
        expected = [
            ("http://localhost:8088/api/handoff/info", "GET", None, handoff.SHORT_TIMEOUT),
            ("https://[::1]:8088/api/handoff/import", "POST", payload, handoff.IMPORT_TIMEOUT),
            ("https://[::1]:8088/api/handoff/render", "POST", {"stem": "sample", "voice": "test-voice"},
             handoff.SHORT_TIMEOUT),
            ("http://localhost:8088/api/handoff/status?job_id=a%2Fb%3Fc%2Bd", "GET", None,
             handoff.SHORT_TIMEOUT),
            ("http://localhost:8088/api/handoff/cancel", "POST", {"job_id": "remote-id"},
             handoff.SHORT_TIMEOUT),
        ]
        self.assertEqual(opener.open.call_count, len(expected))
        for call, (url, method, body, timeout) in zip(opener.open.call_args_list, expected):
            request = call.args[0]
            self.assertEqual(request.full_url, url)
            self.assertEqual(request.get_method(), method)
            self.assertEqual(call.kwargs, {"timeout": timeout})
            if body is None:
                self.assertIsNone(request.data)
            else:
                self.assertEqual(request.get_header("Content-type"), "application/json")
                self.assertEqual(json.loads(request.data.decode("utf-8")), body)
        self.assertIn("Nhãn".encode("utf-8"), opener.open.call_args_list[1].args[0].data)

    def test_redirects_are_rejected_for_get_and_post_in_real_opener(self):
        # Run urllib's real handler chain with a fake HTTP transport, never a socket.
        for code in (301, 302, 303, 307, 308):
            for method in ("GET", "POST"):
                with self.subTest(code=code, method=method):
                    calls = []
                    headers = Message()
                    headers["Location"] = "http://redirect.example.invalid/not-used"
                    body = io.BytesIO(b"redirect")

                    def fake_http_open(_handler, req):
                        calls.append((req.get_method(), req.full_url))
                        self.assertEqual(len(calls), 1, "opener followed Location")
                        response = urllib.response.addinfourl(body, headers, req.full_url, code)
                        response.msg = "Redirect"
                        return response

                    with patch.object(urllib.request.HTTPHandler, "http_open", new=fake_http_open):
                        with self.assertRaisesRegex(handoff.HandoffError, f"HTTP \\({code}\\)"):
                            if method == "GET":
                                handoff.info("localhost:8088")
                            else:
                                handoff.cancel("localhost:8088", "remote-id")

                    endpoint = "info" if method == "GET" else "cancel"
                    self.assertEqual(calls, [(method, f"http://localhost:8088/api/handoff/{endpoint}")])
                    self.assertTrue(body.closed)

    def test_non_redirect_http_errors_preserve_detail_and_close_body(self):
        for body, detail in ((b'{"detail":"test detail"}', "test detail"), (b"[]", "Bad Request")):
            with self.subTest(body=body):
                stream = io.BytesIO(body)
                error = urllib.error.HTTPError(
                    "http://localhost:8088/api/handoff/info", 400, "Bad Request", Message(), stream,
                )
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(urllib.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(handoff.HandoffError, detail):
                        handoff.info("localhost:8088")
                self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
