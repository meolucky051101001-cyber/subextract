"""Chinese-source line filtering; pure/mocked OCR, no models, service or job writes."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.pipeline.ocr import OcrEngine
from app.pipeline.post import build_cues, clean_ocr_lines
import pipeline_reliability_test as pipeline_fixture


class ChineseSourceTests(unittest.TestCase):
    def test_standalone_digits_latin_and_punctuation_are_excluded(self):
        for text in ("2", "7", "2026", "１２３", "2.5", "20%", "-7", "MUJI", "English", "...", "  "):
            with self.subTest(text=text):
                self.assertEqual(clean_ocr_lines(text, chinese_only=True), [])

    def test_digits_on_the_same_chinese_line_are_preserved(self):
        for text in ("第2集", "价格20元", "2个", "２０元", "MUJI卡片盒2个", "介", "繁體中文第3集"):
            with self.subTest(text=text):
                self.assertEqual(clean_ocr_lines(text, chinese_only=True), [text])

    def test_numeric_row_is_not_whitelisted_by_an_adjacent_chinese_row(self):
        self.assertEqual(clean_ocr_lines("2\n这是第3层\r\n7\n价格20元", chinese_only=True),
                         ["这是第3层", "价格20元"])

    def test_other_explicit_source_languages_keep_their_text(self):
        self.assertEqual(clean_ocr_lines("English 2\nGiá 20 đồng\n7"),
                         ["English 2", "Giá 20 đồng", "7"])

    def test_extended_han_is_retained_in_final_cue(self):
        text = chr(0x20000) + "2"
        cues = build_cues([{"start": 0, "end": 1, "lines": [{"text": text, "conf": .99}]}],
                          2, chinese_only=True)
        self.assertEqual([cue["text"] for cue in cues], [text])

    def test_build_filters_each_row_and_each_roi_without_mutating_groups(self):
        groups = [
            {"start": 0, "end": 1, "region": 0, "lines": [{"text": "2", "conf": .99}]},
            {"start": 0, "end": 1, "region": 1, "lines": [{"text": "第2集\n7\n价格20元", "conf": .99}]},
        ]
        original = copy.deepcopy(groups)
        cues = build_cues(groups, 2, chinese_only=True)
        self.assertEqual([(cue["region"], cue["text"]) for cue in cues], [(1, "第2集\n价格20元")])
        self.assertEqual(groups, original)

    @staticmethod
    def reader(langs):
        engine = object.__new__(OcrEngine)
        engine.langs = langs
        engine.readers = {"zh": object()}
        return engine

    def test_numeric_high_confidence_cannot_preempt_chinese_binary_candidate(self):
        engine = self.reader(["zh"])
        engine._read_with_recognizer = Mock(side_effect=[("7", .99), ("第2集", .8)])
        with patch("app.pipeline.ocr._binarize", return_value=np.zeros((20, 60), np.uint8)):
            result = engine._read_crop(np.zeros((20, 60, 3), np.uint8))
        self.assertEqual(result, ("第2集", .8, "zh"))
        self.assertEqual(engine._read_with_recognizer.call_count, 2)

    def test_numeric_only_recognizer_returns_no_chinese_candidate(self):
        engine = self.reader(["zh"])
        engine._read_with_recognizer = Mock(return_value=("7", .99))
        with patch("app.pipeline.ocr._binarize", return_value=None):
            result = engine._read_crop(np.zeros((20, 60, 3), np.uint8))
        self.assertEqual(result, ("", 0.0, ""))

    def test_fallback_keeps_digits_in_same_visual_row_not_other_rows(self):
        engine = self.reader(["zh"])
        def box(x, y, w=40, h=18):
            return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        recognizer = Mock()
        recognizer.readtext.return_value = [
            (box(0, 0), "2", .99),
            (box(0, 30), "价格", .8),
            (box(45, 31), "20", .9),
            (box(90, 30), "元", .8),
            (box(0, 65), "7", .99),
        ]
        text, conf = engine._read_fallback(recognizer, np.zeros((90, 140, 3), np.uint8))
        self.assertEqual(text, "价格 20 元")
        self.assertAlmostEqual(conf, (.8 + .9 + .8) / 3)

    def test_chinese_multiline_prefers_row_filtered_read_over_numeric_whole_read(self):
        engine = self.reader(["zh"])
        engine._read_crop = Mock(return_value=("中文2 7", .99, "zh"))
        engine._read_segments = Mock(return_value=("中文2", .8))
        with patch("app.pipeline.ocr._ink_segments", return_value=[(4, 20), (28, 44)]):
            self.assertEqual(engine._read_line_unlocked(np.zeros((50, 60, 3), np.uint8)),
                             ("中文2", .8, "multi"))

    def test_pipeline_rejects_screenshot_numeric_cues_but_keeps_mixed_line(self):
        fixture = pipeline_fixture.PipelineReliabilityTests()
        values = ["这个是单层的", "2", "熟悉的配方熟悉的味道", "7", "价格20元", "English"]
        result = fixture.fake_pipeline(len(values), lambda t, _ri: values[int(t)], langs=["zh"])
        self.assertEqual([cue["text"] for cue in result["cues"]],
                         ["这个是单层的", "熟悉的配方熟悉的味道", "价格20元"])

    def test_pipeline_filters_multiline_output_before_comparing_read_states(self):
        fixture = pipeline_fixture.PipelineReliabilityTests()
        result = fixture.fake_pipeline(3, lambda t, _ri: "这是第2层\n" + str(int(t)),
                                       langs=["zh"], same_group=True)
        self.assertEqual([cue["text"] for cue in result["cues"]], ["这是第2层"])
        self.assertEqual((result["cues"][0]["start"], result["cues"][0]["end"]), (0, 3))

    def test_mixed_source_selection_is_not_forced_to_chinese(self):
        fixture = pipeline_fixture.PipelineReliabilityTests()
        result = fixture.fake_pipeline(3, lambda t, _ri: ("English", "Giá 20 đồng", "第2集")[int(t)],
                                       langs=["vi", "en", "zh"])
        self.assertEqual([cue["text"] for cue in result["cues"]], ["English", "Giá 20 đồng", "第2集"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
