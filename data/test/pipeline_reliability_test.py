"""Offline regressions for full-duration OCR sampling and watermark safety.

All decoding, detection, image codecs, and OCR are mocked. Scratch crops live
only in TemporaryDirectory; no models, network, settings, or real jobs are used.
"""
import copy
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.pipeline import runner
from app.pipeline.ffmpeg_io import FFmpegError, VideoInfo


class InlineThread:
    """Run the mocked engine loader deterministically without loading a model."""

    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return False

    def join(self):
        pass


class TemporalSamplingTests(unittest.TestCase):
    def test_online_buffer_is_bounded_and_keeps_first_and_latest(self):
        for duration in (60, 120, 3600):
            with self.subTest(duration=duration):
                group = runner._Group(start=0, ref=None, last_t=0, best=[])
                frame = np.zeros((2, 2, 3), dtype=np.uint8)
                fps = 5 if duration < 3600 else 1
                for index in range(duration * fps):
                    t = index / fps
                    runner._add_spread(group, t, frame)
                    self.assertLessEqual(len(group.spread), 24)
                    self.assertEqual(group.spread[0][0], 0)
                    self.assertEqual(group.spread[-1][0], t)
                candidates = runner._temporal_candidates([
                    {"t": t, "path": str(index)}
                    for index, (t, _frame) in enumerate(group.spread)
                ])
                times = [candidate["t"] for candidate in candidates]
                self.assertLessEqual(len(times), 16)
                self.assertEqual(times, sorted(set(times)))
                self.assertEqual(times[0], 0)
                self.assertEqual(times[-1], duration - 1 / fps)
                for fraction in (.25, .5, .9):
                    self.assertLessEqual(min(abs(t - duration * fraction) for t in times), duration * .12)

    def test_retained_frames_are_copies(self):
        group = runner._Group(start=0, ref=None, last_t=0, best=[])
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        runner._add_spread(group, 0, frame)
        frame[:] = 1
        runner._add_spread(group, 1, frame)
        frame[:] = 2
        self.assertTrue(np.all(group.spread[0][1] == 0))
        self.assertTrue(np.all(group.spread[-1][1] == 1))

    def test_candidate_cap_deduplicates_without_discarding_tail(self):
        candidates = [{"t": 0, "path": "best"}, {"t": 1, "path": "best1"}]
        candidates += [{"t": index, "path": str(index)} for index in range(60)]
        snapshot = copy.deepcopy(candidates)
        selected = runner._temporal_candidates(candidates)
        times = [candidate["t"] for candidate in selected]
        self.assertEqual(len(times), 16)
        self.assertEqual(len(set(times)), 16)
        self.assertEqual((times[0], times[-1]), (0, 59))
        self.assertEqual(selected[0]["path"], "best")
        self.assertEqual(candidates, snapshot)


class PipelineReliabilityTests(unittest.TestCase):
    def fake_pipeline(self, duration, text_at, *, same_group=False, region_count=1,
                      logo=False, moving_logo=False, fail_at=None, sample_fps=1,
                      state_at=None, translation_result=None, min_duration=.25, langs=None):
        self.reads = []
        self.reports = []
        self.engine_requests = 0
        info = VideoInfo(duration, 80 * region_count, 64, 25, "mock")
        plan = SimpleNamespace(out_w=info.width, out_h=64, sample_fps=sample_fps)
        multi = SimpleNamespace(plan=plan, regions=[(ri * 80, 0, 80, 64) for ri in range(region_count)])
        passes = 0

        def image(index, ri=0, width=80):
            result = np.zeros((64, width, 3), dtype=np.uint8)
            result[:, :, 0] = index % 256
            result[:, :, 1] = ri
            result[:, :, 2] = index // 256
            return result

        def stream(_path, _plan, _cancel):
            nonlocal passes
            passes += 1
            if passes == 1:
                yield 0, image(0, width=info.width)
                return
            for index in range(round(duration * sample_fps)):
                t = index / sample_fps
                if fail_at is not None and t == fail_at:
                    raise FFmpegError("mock decoder failed after frames")
                yield t, np.concatenate([image(index, ri) for ri in range(region_count)], axis=1)

        def detect(frame, **_kwargs):
            t = (int(frame[0, 0, 0]) + int(frame[0, 0, 2]) * 256) / sample_fps
            x = int(t) if moving_logo else 0
            boxes = ([(x, 2, x + 50, 16)] if logo else []) + [(2, 35, 74, 51)]
            ri = int(frame[0, 0, 1])
            state = state_at(t, ri) if state_at is not None else t
            return SimpleNamespace(mask=np.zeros(frame.shape[:2], dtype=np.uint8),
                                   boxes=boxes, sharp=1000 - t, state=state)

        def encode(_extension, frame, _params):
            return True, frame[0, 0].copy()

        def read_image(path, _mode):
            raw = Path(path).read_bytes()
            return image(raw[0] + raw[2] * 256, raw[1])

        def prepare_line(frame, box):
            line = 0 if logo and box[1] == 2 else 1
            return np.array([*frame[0, 0], line], dtype=np.uint8)

        def read_line(crop):
            low, ri, high, line = map(int, crop)
            index = low + high * 256
            t = index if sample_fps == 1 else index / sample_fps
            self.reads.append((t, ri, line))
            text = "CHANNEL" if line == 0 else text_at(t, ri)
            return text, .99, "mock"

        def engine(*_args):
            self.engine_requests += 1
            return SimpleNamespace(read_line=read_line)

        mocks = {
            "probe_video": lambda _path: info,
            "make_multi_plan": lambda *_args: multi,
            "stream_roi_frames": stream,
            "detect_text": detect,
            "same_state": lambda a, b: same_group or a.state == b.state,
            "get_engine": engine,
            "prepare_line": prepare_line,
            "get_gemini_keys": lambda: ["mock-key"] if translation_result is not None else self.fail("unexpected settings read"),
            "load_settings": lambda: {} if translation_result is not None else self.fail("unexpected settings read"),
            "translate_cues": lambda *_args, **_kwargs: translation_result if translation_result is not None else self.fail("unexpected translation"),
            "log_usage": lambda _event: None,
        }
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for name, replacement in mocks.items():
                stack.enter_context(patch.object(runner, name, replacement))
            stack.enter_context(patch.object(runner.threading, "Thread", InlineThread))
            stack.enter_context(patch.object(runner.cv2, "imencode", encode))
            stack.enter_context(patch.object(runner.cv2, "imread", read_image))
            stack.enter_context(patch.object(runner.cv2, "cvtColor", lambda frame, _code: frame))
            params = runner.RunParams(sample_fps=sample_fps, langs=langs if langs is not None else ["vi", "en", "zh"],
                                      rois=tuple((0, 0, 1, 1) for _ in range(region_count)),
                                      translate_to="en" if translation_result is not None else "",
                                      min_duration=min_duration)
            try:
                return runner.run_pipeline(Path("mock.mp4"), Path(tmp), params,
                                           lambda *args: self.reports.append(args), threading.Event(), False)
            finally:
                self.assertFalse((Path(tmp) / "crops").exists(), "scratch crops must be cleaned")

    def test_long_groups_detect_changes_at_25_50_and_90_percent(self):
        for duration in (60, 120):
            for fraction in (.25, .5, .9):
                with self.subTest(duration=duration, fraction=fraction):
                    change = int(duration * fraction)
                    result = self.fake_pipeline(duration, lambda t, _ri: "opening message" if t < change else "CLOSING NOW",
                                                same_group=True)
                    windows = int(duration / runner.MAX_GROUP_SECONDS)
                    self.assertEqual(result["stats"]["detected_groups"], windows)
                    self.assertEqual([cue["text"] for cue in result["cues"]], ["opening message", "CLOSING NOW"])
                    tail = result["cues"][-1]
                    self.assertGreaterEqual(tail["start"], change)
                    self.assertLessEqual(tail["start"], change + duration * .12)
                    self.assertEqual(tail["end"], duration)
                    times = [t for t, _ri, _line in self.reads]
                    self.assertLessEqual(len(times), 16 * windows)
                    self.assertEqual((min(times), max(times)), (0, duration - 1))

    def test_twelve_second_windows_recover_brief_late_text_at_five_fps(self):
        result = self.fake_pipeline(120, lambda t, _ri: "URGENT ALERT" if 90 <= t < 94 else "steady dialogue",
                                    same_group=True, sample_fps=5)
        self.assertEqual(result["stats"]["detected_groups"], 10)
        self.assertEqual([cue["text"] for cue in result["cues"]], ["steady dialogue", "URGENT ALERT", "steady dialogue"])
        alert = result["cues"][1]
        self.assertEqual((alert["start"], alert["end"]), (90, 94))
        times = sorted(t for t, _ri, _line in self.reads)
        self.assertLessEqual(len(times), 16 * 10)
        self.assertAlmostEqual(times[-1], 119.8)
        self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), 1.01)

    def test_multiple_short_changes_do_not_need_to_persist_to_the_tail(self):
        bursts = [(8, 9.4, "DANGER"), (27.2, 28.6, "STOP NOW"), (45.4, 46.8, "URGENT ALERT")]

        def text_at(t, _ri):
            return next((text for start, end, text in bursts if start <= t < end), "steady dialogue")

        result = self.fake_pipeline(60, text_at, same_group=True, sample_fps=5)
        self.assertEqual(result["stats"]["detected_groups"], 5)
        self.assertEqual([cue["text"] for cue in result["cues"]],
                         ["steady dialogue", "DANGER", "steady dialogue", "STOP NOW",
                          "steady dialogue", "URGENT ALERT", "steady dialogue"])
        self.assertEqual(result["cues"][-1]["end"], 60)
        self.assertLessEqual(len(self.reads), 16 * 5)

    def test_window_boundary_frame_begins_the_next_ocr_window(self):
        result = self.fake_pipeline(24, lambda t, _ri: "BOUNDARY ALERT" if 12 <= t < 13.2 else "steady dialogue",
                                    same_group=True, sample_fps=5)
        self.assertEqual(result["stats"]["detected_groups"], 2)
        self.assertEqual([cue["text"] for cue in result["cues"]], ["steady dialogue", "BOUNDARY ALERT", "steady dialogue"])
        self.assertEqual(result["cues"][1]["start"], 12)
        self.assertIn(11.8, [t for t, _ri, _line in self.reads])
        self.assertIn(12, [t for t, _ri, _line in self.reads])
        self.assertIn(23.8, [t for t, _ri, _line in self.reads])

    def test_identical_window_text_remerges_independently_in_each_roi(self):
        result = self.fake_pipeline(60, lambda _t, ri: "upper line" if ri else "lower line",
                                    same_group=True, sample_fps=5, region_count=2)
        self.assertEqual(result["stats"]["detected_groups"], 10)
        self.assertEqual(len(result["cues"]), 2)
        self.assertEqual({cue["region"] for cue in result["cues"]}, {0, 1})
        self.assertTrue(all((cue["start"], cue["end"]) == (0, 60) for cue in result["cues"]))
        self.assertLessEqual(len(self.reads), 16 * 10)

    def test_subminimum_artificial_eof_tails_preserve_same_text_end(self):
        for duration in (12.2, 24.2, 60.2, 120.2):
            with self.subTest(duration=duration):
                result = self.fake_pipeline(duration, lambda _t, _ri: "steady dialogue",
                                            same_group=True, sample_fps=5)
                self.assertEqual([cue["text"] for cue in result["cues"]], ["steady dialogue"])
                self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (0, duration))

    def test_short_artificial_window_before_real_change_preserves_boundary(self):
        result = self.fake_pipeline(24, lambda t, _ri: "steady dialogue" if t < 12.2 else "NEXT",
                                    sample_fps=5, state_at=lambda t, _ri: t >= 12.2)
        self.assertEqual([(cue["text"], cue["start"], cue["end"]) for cue in result["cues"]],
                         [("steady dialogue", 0, 12.2), ("NEXT", 12.2, 24)])

    def test_different_subminimum_tail_is_not_painted_with_previous_text(self):
        result = self.fake_pipeline(12.2, lambda t, _ri: "steady dialogue" if t < 12 else "BRIEF ALERT",
                                    same_group=True, sample_fps=5)
        self.assertEqual([(cue["text"], cue["start"], cue["end"]) for cue in result["cues"]],
                         [("steady dialogue", 0, 12), ("BRIEF ALERT", 12, 12.2)])

    def test_nondefault_minimum_duration_does_not_discard_genuine_groups(self):
        result = self.fake_pipeline(12, lambda t, _ri: f"word{t}", min_duration=2)
        self.assertEqual(result["stats"]["detected_groups"], 12)
        self.assertEqual([cue["text"] for cue in result["cues"]], [f"word{t}" for t in range(12)])
        self.assertEqual([(cue["start"], cue["end"]) for cue in result["cues"]],
                         [(t, t + 1) for t in range(12)])

    def test_all_missing_translation_indices_are_preserved_above_two_hundred(self):
        missing = list(range(201))
        translation = {"translated": 0, "total": 201, "seconds": 0,
                       "error": "mock partial translation", "untranslated": missing}
        result = self.fake_pipeline(201, lambda t, _ri: f"word{t}", translation_result=translation)
        self.assertEqual(len(result["cues"]), 201)
        self.assertEqual(result["stats"]["untranslated_cues"], 201)
        self.assertEqual(result["stats"]["untranslated_indices"], missing)

    def test_alternating_repeated_dialogue_is_retained(self):
        expected = ["YES" if t % 2 == 0 else f"word{t}" for t in range(12)]
        result = self.fake_pipeline(12, lambda t, _ri: expected[t])
        self.assertEqual([cue["text"] for cue in result["cues"]], expected)
        self.assertEqual(sum(cue["text"] == "YES" for cue in result["cues"]), 6)

    def test_long_repeated_reply_followed_by_other_dialogue_is_retained(self):
        result = self.fake_pipeline(20, lambda t, _ri: "YES" if t < 18 else f"goodbye{t}" if t == 18 else "done19")
        self.assertEqual([cue["text"] for cue in result["cues"]], ["YES", "goodbye18", "done19"])
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (0, 18))

    def test_continuous_logo_is_removed_while_changing_dialogue_remains(self):
        result = self.fake_pipeline(12, lambda t, _ri: f"word{t}", logo=True)
        self.assertEqual([cue["text"] for cue in result["cues"]], [f"word{t}" for t in range(12)])

    def test_repetition_is_not_counted_across_rois(self):
        result = self.fake_pipeline(12, lambda t, ri: "YES" if (t + ri) % 2 == 0 else f"word{ri}x{t}",
                                    region_count=2)
        self.assertEqual(len(result["cues"]), 24)
        for ri in range(2):
            self.assertEqual(sum(cue["text"] == "YES" and cue["region"] == ri for cue in result["cues"]), 6)

    def test_moving_text_is_not_a_static_watermark(self):
        result = self.fake_pipeline(12, lambda t, _ri: f"word{t}", logo=True, moving_logo=True)
        self.assertEqual(len(result["cues"]), 12)
        self.assertTrue(all("CHANNEL" in cue["text"] for cue in result["cues"]))

    def test_identical_dialogue_without_changing_neighbour_is_retained(self):
        result = self.fake_pipeline(12, lambda _t, _ri: "YES")
        self.assertEqual([cue["text"] for cue in result["cues"]], ["YES"])
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (0, 12))

    def test_decoder_error_does_not_return_partial_success_or_load_ocr(self):
        with self.assertRaisesRegex(FFmpegError, "failed after frames"):
            self.fake_pipeline(12, lambda t, _ri: f"word{t}", fail_at=3)
        self.assertEqual(self.engine_requests, 0)
        self.assertFalse(any(stage == "done" for stage, _value, _message in self.reports))


class WatermarkContinuityTests(unittest.TestCase):
    def make_groups(self, gap=0):
        groups = []
        for index in range(12):
            start = index + (gap if index >= 6 else 0)
            groups.append({"region": 0, "start": start, "end": start + 1, "lines": [
                {"text": "CHANNEL", "conf": .99, "box": (0, 0, 50, 14)},
                {"text": f"word{index}", "conf": .99, "box": (0, 30, 70, 44)},
            ]})
        return groups

    def test_varying_dialogue_width_still_proves_a_continuous_logo(self):
        for centered in (False, True):
            with self.subTest(centered=centered):
                groups = self.make_groups()
                for index, group in enumerate(groups):
                    group["lines"][1]["box"] = (index if centered else 0, 30, 60 + index, 44)
                runner._filter_static_lines(groups, 12, .28, .2)
                self.assertEqual([[line["text"] for line in group["lines"]] for group in groups],
                                 [[f"word{index}"] for index in range(12)])

    def test_static_columns_on_one_row_do_not_fake_changing_neighbours(self):
        groups = self.make_groups()
        for group in groups:
            group["lines"][1].update(text="STATIC A", box=(0, 30, 30, 44))
            group["lines"].append({"text": "STATIC B", "conf": .99, "box": (40, 30, 70, 44)})
        snapshot = copy.deepcopy(groups)
        runner._filter_static_lines(groups, 12, .28, .2)
        self.assertEqual(groups, snapshot)

    def test_multiple_static_neighbours_are_not_changing_line_evidence(self):
        groups = self.make_groups()
        for group in groups:
            group["lines"][1]["text"] = "STATIC A"
            group["lines"].append({"text": "STATIC B", "conf": .99, "box": (0, 50, 70, 64)})
        snapshot = copy.deepcopy(groups)
        runner._filter_static_lines(groups, 12, .28, .2)
        self.assertEqual(groups, snapshot)

    def test_overlapping_duplicate_boxes_are_not_distinct_neighbours(self):
        groups = self.make_groups()
        for group in groups:
            group["lines"][1]["box"] = (1, 0, 51, 14)
        snapshot = copy.deepcopy(groups)
        runner._filter_static_lines(groups, 12, .28, .2)
        self.assertEqual(groups, snapshot)

    def test_temporal_gap_prevents_removal_despite_high_total_coverage(self):
        groups = self.make_groups(gap=.4)
        runner._filter_static_lines(groups, 12.4, .28, .2)
        self.assertTrue(all(group["lines"][0]["text"] == "CHANNEL" for group in groups))

    def test_short_clip_fragment_is_not_full_video_static_evidence(self):
        groups = self.make_groups()
        runner._filter_static_lines(groups, 120, .28, .2)
        self.assertTrue(all(group["lines"][0]["text"] == "CHANNEL" for group in groups))

    def test_low_confidence_occurrences_cannot_make_a_watermark(self):
        groups = self.make_groups()
        for group in groups[6:]:
            group["lines"][0]["conf"] = .1
        runner._filter_static_lines(groups, 12, .28, .2)
        self.assertTrue(all(any(line["text"] == "CHANNEL" for line in group["lines"]) for group in groups))

    def test_geometry_is_required_in_addition_to_text_and_y_position(self):
        groups = self.make_groups()
        for group in groups:
            del group["lines"][0]["box"]
            group["lines"][0]["ny"] = .1
        runner._filter_static_lines(groups, 12, .28, .2)
        self.assertTrue(all(group["lines"][0]["text"] == "CHANNEL" for group in groups))


if __name__ == "__main__":
    unittest.main()
