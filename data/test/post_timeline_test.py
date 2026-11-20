"""Offline regression tests for subtitle ordering and timestamp normalization."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.pipeline.post import build_cues, format_timestamp, to_srt, to_vtt


def group(start, end, text, region=0, conf=.99):
    return {
        "start": start,
        "end": end,
        "region": region,
        "lines": [{"text": text, "conf": conf}],
    }


class TimelineRegression(unittest.TestCase):
    def test_close_order_does_not_change_global_time_order(self):
        cues = build_cues([group(5, 6, "upper", 1), group(0, 10, "lower", 0)], 12)
        self.assertEqual([cue["start"] for cue in cues], [0.0, 5.0])
        self.assertIn("00:00:00,000", to_srt(cues))
        self.assertTrue(to_vtt(cues).startswith("WEBVTT"))

    def test_same_region_overlap_is_fixed_even_when_another_roi_interleaves(self):
        cues = build_cues([
            group(0, 10, "lower one", 0),
            group(2, 3, "upper", 1),
            group(5, 6, "lower two", 0),
        ], 12)
        lower = [cue for cue in cues if cue["region"] == 0]
        self.assertEqual(len(lower), 2)
        self.assertLessEqual(lower[0]["end"], lower[1]["start"])
        self.assertEqual(lower[0]["end"], 5.0)
        self.assertTrue(any(cue["region"] == 1 for cue in cues))

    def test_nested_identical_text_merges_without_shrinking_end(self):
        cues = build_cues([group(0, 10, "same"), group(5, 6, "same")], 12)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["end"], 10.0)

    def test_identical_text_merges_across_other_roi(self):
        cues = build_cues([
            group(0, 1, "repeat", 0),
            group(.4, .8, "other", 1),
            group(1.2, 2, "repeat", 0),
        ], 4, merge_gap=.45)
        lower = [cue for cue in cues if cue["region"] == 0]
        self.assertEqual(len(lower), 1)
        self.assertEqual((lower[0]["start"], lower[0]["end"]), (0.0, 2.0))

    def test_distinct_same_region_text_is_not_merged_through_intervening_text(self):
        cues = build_cues([
            group(0, 1, "A", 0),
            group(1.1, 1.5, "B", 0),
            group(1.7, 2, "A", 0),
        ], 3, merge_gap=.8)
        self.assertEqual([cue["text"] for cue in cues], ["A", "B", "A"])

    def test_same_start_conflict_retains_both_observations(self):
        cues = build_cues([group(1, 2, "first"), group(1, 3, "later")], 4)
        self.assertEqual([cue["text"] for cue in cues], ["first", "later"])
        self.assertEqual([cue["start"] for cue in cues], [1.0, 1.0])

    def test_equal_start_conflicts_are_all_clipped_at_next_distinct_start(self):
        cues = build_cues([
            group(0, 10, "first"), group(0, 8, "conflict"), group(5, 6, "next"),
        ], 12)
        self.assertEqual([cue["end"] for cue in cues], [5.0, 5.0, 6.0])
        self.assertEqual([cue["text"] for cue in cues], ["first", "conflict", "next"])

    def test_rounding_tie_retains_both_texts(self):
        cues = build_cues([group(1.0001, 2, "one"), group(1.0004, 3, "two")], 4)
        self.assertEqual([cue["text"] for cue in cues], ["one", "two"])
        self.assertEqual([cue["start"] for cue in cues], [1.0, 1.0])

    def test_close_cues_never_get_a_synthetic_overlap(self):
        cues = build_cues([
            group(0, .1, "first"),
            group(.1, 1, "second"),
        ], 2, merge_gap=0, min_duration=.1)
        self.assertLessEqual(cues[0]["end"], cues[1]["start"])
        self.assertLess(cues[0]["start"], cues[0]["end"])

    def test_submillisecond_cue_near_eof_gets_valid_1ms_interval(self):
        cues = build_cues([group(.9996, 1, "last")], 1, min_duration=.001)
        self.assertEqual(len(cues), 1)
        self.assertEqual((cues[0]["start"], cues[0]["end"]), (.999, 1.0))
        self.assertGreater(cues[0]["end"], cues[0]["start"])

    def test_regions_can_overlap_and_legacy_none_is_separate_from_zero(self):
        cues = build_cues([
            group(0, 2, "legacy", None),
            group(0, 2, "roi zero", 0),
            group(0, 2, "roi one", 1),
        ], 3)
        self.assertEqual(len(cues), 3)
        self.assertEqual([cue["region"] for cue in cues], [None, 0, 1])
        self.assertTrue(all(cue["start"] == 0 and cue["end"] == 2 for cue in cues))

    def test_nonfinite_timestamps_and_confidence_are_ignored(self):
        cues = build_cues([
            group(float("nan"), 1, "bad"),
            group(0, float("inf"), "bad2"),
            group(1, 2, "bad confidence", conf=float("nan")),
        ], 5)
        self.assertEqual(cues, [])

    def test_confidence_boundary_unicode_and_multiline_are_preserved(self):
        cues = build_cues([{
            "start": 0, "end": 1, "region": 0,
            "lines": [{"text": "Nhãn", "conf": .25}, {"text": "卡片", "conf": 1}],
        }], 2, min_conf=.25)
        self.assertEqual(cues[0]["text"], "Nhãn\n卡片")
        self.assertIn("Nhãn\n卡片", to_srt(cues))
        self.assertIn("Nhãn\n卡片", to_vtt(cues))

    def test_format_timestamp_carries_minute_hour_and_unicode_export(self):
        self.assertEqual(format_timestamp(59.9996), "00:01:00,000")
        self.assertEqual(format_timestamp(3599.9996, "."), "01:00:00.000")

    def test_build_does_not_mutate_groups(self):
        groups = [group(0, 5, "one"), group(1, 2, "two")]
        before = copy.deepcopy(groups)
        build_cues(groups, 6)
        self.assertEqual(groups, before)

    def test_normalization_is_idempotent_for_canonical_cues(self):
        once = build_cues([group(0, 1, "A"), group(1.5, 2, "B")], 3)
        as_groups = [group(cue["start"], cue["end"], cue["text"], cue["region"])
                     for cue in once]
        twice = build_cues(as_groups, 3)
        self.assertEqual(twice, once)


if __name__ == "__main__":
    unittest.main(verbosity=2)
