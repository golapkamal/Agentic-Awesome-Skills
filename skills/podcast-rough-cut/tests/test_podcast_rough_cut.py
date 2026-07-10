import importlib.util
import json
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "podcast_rough_cut.py"


def load_module():
    spec = importlib.util.spec_from_file_location("podcast_rough_cut", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PodcastRoughCutTests(unittest.TestCase):
    def test_suggest_cuts_marks_restart_and_fillers_as_candidates(self):
        mod = load_module()
        entries = [
            mod.TranscriptEntry(0.0, 5.0, "我们先讲第一遍内容很重要"),
            mod.TranscriptEntry(5.0, 6.0, "321"),
            mod.TranscriptEntry(6.0, 10.0, "我们先讲第一遍内容很重要"),
            mod.TranscriptEntry(10.0, 14.0, "就是就是就是这个地方有点重复"),
        ]

        suggestions = mod.suggest_cuts_from_entries(
            entries,
            restart_delete_after=1.0,
            restart_search_before=10.0,
            restart_search_after=10.0,
            restart_similarity_threshold=0.35,
            max_filler_suggestions=10,
        )

        reasons = [item.reason for item in suggestions]
        self.assertTrue(any("321" in reason and "候选" in reason for reason in reasons))
        self.assertTrue(any("口头禅候选" in reason for reason in reasons))
        self.assertTrue(all(item.end > item.start for item in suggestions))

    def test_write_transcript_outputs_srt_txt_and_json(self):
        mod = load_module()
        entries = [
            mod.TranscriptEntry(0.0, 1.2, "你好"),
            mod.TranscriptEntry(1.2, 2.5, "世界"),
        ]

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            srt_path, txt_path, json_path = mod.write_transcript_outputs(tmp_path, "sample", entries, "你好 世界")

            self.assertIn("00:00:00,000 --> 00:00:01,200", srt_path.read_text(encoding="utf-8"))
            self.assertEqual(txt_path.read_text(encoding="utf-8"), "你好 世界\n")
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["text"], "你好 世界")
            self.assertEqual(data["segments"][1]["start"], 1.2)

    def test_entries_from_direct_asr_result_applies_chunk_offset(self):
        mod = load_module()
        result = {
            "text": "第二段",
            "segments": [
                {"start": 0.5, "end": 1.5, "text": "第二段"},
            ],
        }

        entries = mod.entries_from_direct_asr_result(result, offset=3000.0, fallback_duration=10.0)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].start, 3000.5)
        self.assertEqual(entries[0].end, 3001.5)

    def test_entries_from_direct_asr_result_estimates_plain_text_timestamps(self):
        mod = load_module()
        result = {"text": "第一句。第二句很长一点！第三句"}

        entries = mod.entries_from_direct_asr_result(result, offset=60.0, fallback_duration=30.0)

        self.assertEqual([entry.text for entry in entries], ["第一句。", "第二句很长一点！", "第三句"])
        self.assertEqual(entries[0].start, 60.0)
        self.assertAlmostEqual(entries[-1].end, 90.0)
        self.assertTrue(all(entry.end > entry.start for entry in entries))

    def test_silence_insert_markers_add_gap_rows_and_filter_inputs(self):
        mod = load_module()
        keeps = [
            mod.Segment(0.0, 10.0),
            mod.Segment(20.0, 30.0),
            mod.Segment(40.0, 55.0),
        ]

        rows = mod.silence_marker_rows(keeps, silence_duration=2.0)
        filter_complex = mod.build_filter(
            keeps,
            process=False,
            target_lufs=-16.0,
            true_peak=-1.5,
            lra=11.0,
            splice_marker_mode="silence-insert",
            splice_silence_duration=2.0,
        )

        self.assertEqual(rows[0], (10.0, 12.0, 10.0, 20.0, 2.0))
        self.assertEqual(rows[1], (22.0, 24.0, 30.0, 40.0, 4.0))
        self.assertIn("anullsrc=r=48000:cl=mono:d=2.000[sil0]", filter_complex)
        self.assertIn("concat=n=5:v=0:a=1[joined]", filter_complex)

    def test_write_manifest_records_silence_gap_offsets(self):
        mod = load_module()
        keeps = [mod.Segment(0.0, 10.0), mod.Segment(20.0, 30.0)]
        rows = mod.silence_marker_rows(keeps, silence_duration=2.0)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.txt"
            mod.write_manifest(
                manifest_path,
                Path("/tmp/in.m4a"),
                Path("/tmp/out.m4a"),
                30.0,
                None,
                [mod.Cut(10.0, 20.0, "测试", "00:00:10", "00:00:20")],
                [mod.Cut(11.0, 19.0, "测试", "00:00:10", "00:00:20")],
                keeps,
                1.0,
                False,
                splice_marker_mode="silence-insert",
                silence_rows=rows,
                splice_silence_duration=2.0,
            )

            text = manifest_path.read_text(encoding="utf-8")
            self.assertIn("剪辑点声音标记：静音缺口，插入 2 秒", text)
            self.assertIn("预计成片时长：00:00:24", text)
            self.assertIn("静音缺口标记点", text)
            self.assertIn("累计偏移 +00:00:02", text)

    def test_black_video_output_path_defaults_to_jianying_sidecar(self):
        mod = load_module()
        output = Path("/tmp/内容审查粗剪_压限.m4a")

        sidecar = mod.black_video_output_path(output, None)

        self.assertEqual(sidecar, Path("/tmp/内容审查粗剪_压限_剪映黑场.mp4"))

    def test_validate_black_video_settings_requires_even_dimensions(self):
        mod = load_module()

        with self.assertRaises(RuntimeError):
            mod.validate_black_video_settings(15, 16, 25, 35)
        with self.assertRaises(RuntimeError):
            mod.validate_black_video_settings(16, 16, 0, 35)


if __name__ == "__main__":
    unittest.main()
