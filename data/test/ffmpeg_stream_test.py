"""Offline FFmpeg stream lifecycle regressions using fake subprocess pipes.

No decoder, service, OCR model, network, or existing job data is used.
"""
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.pipeline import ffmpeg_io


FRAME = bytes(range(12))  # 2 x 2 RGB image.
PLAN = ffmpeg_io.StreamPlan((0, 0, 2, 2), 2, 2, 2, 100)


class ChunkedPipe:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False
        self.requests = []

    def read(self, size):
        self.requests.append(size)
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
        return chunk[:size]

    def close(self):
        self.closed = True


class BlockingPipe(ChunkedPipe):
    def __init__(self):
        super().__init__([])
        self.read_started = threading.Event()
        self.killed = threading.Event()

    def read(self, _size):
        self.read_started.set()
        if not self.killed.wait(2):
            raise AssertionError("cancellation did not unblock the fake decoder")
        return b""


class FakeProcess:
    def __init__(self, chunks=(), *, exitcode=0, stderr=b"", pipe=None, wait_timeouts=0):
        self.stdout = pipe if pipe is not None else ChunkedPipe(chunks)
        self.exitcode = exitcode
        self.stderr_bytes = stderr
        self.stderr_file = None
        self.returncode = None
        self.kills = 0
        self.waits = []
        self.wait_timeouts = wait_timeouts
        self.reaped = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        self.returncode = -9
        if isinstance(self.stdout, BlockingPipe):
            self.stdout.killed.set()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.wait_timeouts:
            self.wait_timeouts -= 1
            raise TimeoutExpired("mock ffmpeg", timeout)
        if self.returncode is None:
            self.returncode = self.exitcode
        self.reaped = True
        return self.returncode


class FFmpegStreamTests(unittest.TestCase):
    @contextmanager
    def fake_stream(self, proc, cancel=None):
        def spawn(_cmd, **kwargs):
            proc.stderr_file = kwargs["stderr"]
            proc.stderr_file.write(proc.stderr_bytes)
            return proc

        with patch.object(ffmpeg_io, "_need", return_value="mock-ffmpeg"), \
                patch.object(ffmpeg_io.subprocess, "Popen", side_effect=spawn):
            stream = ffmpeg_io.stream_roi_frames("mock.mp4", PLAN, cancel)
            try:
                yield stream
            finally:
                stream.close()

    def assert_clean(self, proc):
        self.assertTrue(proc.reaped, "child must be waited/reaped")
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr_file.closed)

    def test_clean_eof_waits_for_normal_exit_without_killing_decoder(self):
        proc = FakeProcess([FRAME, bytes([17]) * 12])
        with self.fake_stream(proc) as stream:
            frames = list(stream)
        self.assertEqual([t for t, _frame in frames], [0, .5])
        np.testing.assert_array_equal(frames[0][1], np.frombuffer(FRAME, np.uint8).reshape(2, 2, 3))
        self.assertTrue(np.all(frames[1][1] == 17))
        self.assertEqual(proc.kills, 0)
        self.assertEqual(proc.waits[0], 10)
        self.assert_clean(proc)

    def test_nonzero_exit_after_one_or_many_frames_is_reported(self):
        for count in (1, 5):
            with self.subTest(frames=count):
                proc = FakeProcess([FRAME] * count, exitcode=1, stderr=b"mock decode failure")
                frames = []
                with self.fake_stream(proc) as stream:
                    with self.assertRaisesRegex(ffmpeg_io.FFmpegError, "mock decode failure"):
                        frames.extend(stream)
                self.assertEqual(len(frames), count)
                self.assertEqual(proc.kills, 0)
                self.assert_clean(proc)

    def test_external_sigkill_is_not_treated_as_intentional_cancel(self):
        proc = FakeProcess([FRAME], exitcode=-9)
        with self.fake_stream(proc) as stream:
            with self.assertRaisesRegex(ffmpeg_io.FFmpegError, "-9"):
                list(stream)
        self.assert_clean(proc)

    def test_error_before_first_frame_includes_exit_code_without_stderr(self):
        proc = FakeProcess(exitcode=7)
        with self.fake_stream(proc) as stream:
            with self.assertRaisesRegex(ffmpeg_io.FFmpegError, "7"):
                list(stream)
        self.assert_clean(proc)

    def test_short_pipe_reads_are_accumulated_into_complete_frames(self):
        proc = FakeProcess([FRAME[:2], FRAME[2:7], FRAME[7:], FRAME[:1], FRAME[1:]])
        with self.fake_stream(proc) as stream:
            frames = list(stream)
        self.assertEqual(len(frames), 2)
        for _t, frame in frames:
            np.testing.assert_array_equal(frame.reshape(-1), np.frombuffer(FRAME, np.uint8))
        self.assertEqual(proc.stdout.requests[:3], [12, 10, 5])
        self.assert_clean(proc)

    def test_incomplete_first_or_final_raw_frame_is_an_error_even_on_exit_zero(self):
        for prefix in ([], [FRAME]):
            with self.subTest(complete_prefix=len(prefix)):
                proc = FakeProcess(prefix + [FRAME[:5]], exitcode=0)
                frames = []
                with self.fake_stream(proc) as stream:
                    with self.assertRaisesRegex(ffmpeg_io.FFmpegError, "truncated raw frame"):
                        frames.extend(stream)
                self.assertEqual(len(frames), len(prefix))
                self.assert_clean(proc)

    def test_nonzero_truncated_stream_preserves_decoder_diagnostic(self):
        proc = FakeProcess([FRAME, FRAME[:4]], exitcode=2, stderr=b"corrupt packet")
        with self.fake_stream(proc) as stream:
            with self.assertRaisesRegex(ffmpeg_io.FFmpegError, "corrupt packet"):
                list(stream)
        self.assert_clean(proc)

    def test_cancel_before_start_does_not_spawn_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(ffmpeg_io.subprocess, "Popen") as spawn:
            self.assertEqual(list(ffmpeg_io.stream_roi_frames("mock.mp4", PLAN, cancel)), [])
        spawn.assert_not_called()

    def test_cancel_after_frame_kills_and_reaps_without_decoder_error(self):
        cancel = threading.Event()
        proc = FakeProcess([FRAME, FRAME], exitcode=1, stderr=b"terminated")
        with self.fake_stream(proc, cancel) as stream:
            self.assertEqual(next(stream)[0], 0)
            cancel.set()
            self.assertEqual(list(stream), [])
        self.assertGreaterEqual(proc.kills, 1)
        self.assert_clean(proc)

    def test_cancel_unblocks_stalled_pipe_and_reaps_child(self):
        cancel = threading.Event()
        pipe = BlockingPipe()
        proc = FakeProcess(pipe=pipe)
        finished = threading.Event()
        errors = []
        with self.fake_stream(proc, cancel) as stream:
            def consume():
                try:
                    self.assertEqual(list(stream), [])
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            consumer = threading.Thread(target=consume)
            consumer.start()
            try:
                self.assertTrue(pipe.read_started.wait(2), "fake read never started")
                cancel.set()
                self.assertTrue(finished.wait(2), "cancellation left the consumer blocked")
            finally:
                cancel.set()
                pipe.killed.set()
                consumer.join(2)
            self.assertFalse(consumer.is_alive())
        self.assertEqual(errors, [])
        self.assertGreaterEqual(proc.kills, 1)
        self.assert_clean(proc)

    def test_consumer_close_kills_and_reaps_without_reporting_fake_eof(self):
        proc = FakeProcess([FRAME, FRAME], exitcode=1)
        with self.fake_stream(proc) as stream:
            next(stream)
            stream.close()
        self.assertEqual(proc.kills, 1)
        self.assert_clean(proc)

    def test_pipe_read_exception_preserves_error_and_cleans_up_process(self):
        proc = FakeProcess([FRAME, OSError("broken mock pipe")])
        with self.fake_stream(proc) as stream:
            with self.assertRaisesRegex(OSError, "broken mock pipe"):
                list(stream)
        self.assertEqual(proc.kills, 1)
        self.assert_clean(proc)

    def test_process_start_failure_closes_temporary_stderr_file(self):
        handles = []

        def fail_spawn(_cmd, **kwargs):
            handles.append(kwargs["stderr"])
            raise OSError("mock spawn failed")

        with patch.object(ffmpeg_io, "_need", return_value="mock-ffmpeg"), \
                patch.object(ffmpeg_io.subprocess, "Popen", side_effect=fail_spawn):
            with self.assertRaisesRegex(OSError, "mock spawn failed"):
                list(ffmpeg_io.stream_roi_frames("mock.mp4", PLAN))
        self.assertEqual(len(handles), 1)
        self.assertTrue(handles[0].closed)

    def test_eof_exit_timeout_is_error_and_child_is_killed_and_reaped(self):
        proc = FakeProcess([FRAME], wait_timeouts=1)
        with self.fake_stream(proc) as stream:
            with self.assertRaises(ffmpeg_io.FFmpegError):
                list(stream)
        self.assertEqual(proc.kills, 1)
        self.assert_clean(proc)

    def test_cleanup_wait_timeout_is_followed_by_final_reap(self):
        proc = FakeProcess([FRAME], wait_timeouts=1)
        with self.fake_stream(proc) as stream:
            next(stream)
            stream.close()
        self.assertEqual(proc.waits, [10, None])
        self.assert_clean(proc)


if __name__ == "__main__":
    unittest.main()
