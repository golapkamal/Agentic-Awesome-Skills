#!/usr/bin/env python3
"""Podcast rough cut, ASR handoff, and loudness processing helper.

This script intentionally keeps editorial judgment outside the code. It consumes
explicit delete ranges, applies a repair buffer at both sides of each cut, then
uses ffmpeg to rebuild a stable M4A plus reports.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?$")
SRT_TIME_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
RESTART_MARK_RE = re.compile(r"(?:3\s*2\s*1|三\s*二\s*一|三二一|321|three\s*two\s*one)", re.I)
SENTENCE_END_RE = re.compile(r"[。！？.!?…]")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
BASE_URL = "https://api.siliconflow.cn/v1"
ASR_MODEL = "TeleAI/TeleSpeechASR"
ASR_TIMEOUT_SECONDS = 120
FILLER_WORDS = ("就是", "然后", "那个")


@dataclass
class Cut:
    start: float
    end: float
    reason: str
    raw_start: str
    raw_end: str


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TranscriptEntry:
    start: float
    end: float
    text: str


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def parse_time(value: str) -> float:
    value = value.strip()
    match = TIME_RE.match(value)
    if not match:
        raise ValueError(f"Invalid timecode: {value}")
    hour_s, minute_s, second_s, frac_s = match.groups()
    hours = int(hour_s or 0)
    minutes = int(minute_s)
    seconds = int(second_s)
    frac = 0.0
    if frac_s:
        frac = int(frac_s.ljust(3, "0")[:3]) / 1000
    return hours * 3600 + minutes * 60 + seconds + frac


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    millis = int(round((seconds - math.floor(seconds)) * 1000))
    total = int(math.floor(seconds))
    if millis >= 1000:
        total += 1
        millis -= 1000
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if millis:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def fmt_srt_time(seconds: float) -> str:
    return fmt_time(seconds).replace(".", ",") if "." in fmt_time(seconds) else fmt_time(seconds) + ",000"


def duration(path: Path) -> float:
    result = run(["ffmpeg", "-i", str(path)], check=False)
    text = result.stderr
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        raise RuntimeError(f"Could not determine duration for {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def read_cuts(path: Path) -> list[Cut]:
    cuts: list[Cut] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 2:
                fields = re.split(r"\s{2,}", line, maxsplit=2)
            if len(fields) < 2:
                raise ValueError(f"{path}:{line_no}: expected start/end/reason")
            if line_no == 1 and fields[0].lower() == "start":
                continue
            start_s, end_s = fields[0].strip(), fields[1].strip()
            reason = fields[2].strip() if len(fields) >= 3 else ""
            start, end = parse_time(start_s), parse_time(end_s)
            if end <= start:
                raise ValueError(f"{path}:{line_no}: end must be after start")
            cuts.append(Cut(start, end, reason, start_s, end_s))
    return sorted(cuts, key=lambda item: item.start)


def write_cuts(path: Path, cuts: list[Cut]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("start\tend\treason\n")
        for cut in cuts:
            handle.write(f"{fmt_time(cut.start)}\t{fmt_time(cut.end)}\t{cut.reason}\n")


def read_srt(path: Path) -> list[TranscriptEntry]:
    text = path.read_text(encoding="utf-8-sig")
    entries: list[TranscriptEntry] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_line_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_line_index is None:
            continue
        match = SRT_TIME_RE.search(lines[time_line_index])
        if not match:
            continue
        body = " ".join(lines[time_line_index + 1 :]).strip()
        if not body:
            continue
        entries.append(TranscriptEntry(parse_time(match.group(1)), parse_time(match.group(2)), body))
    return entries


def write_transcript_outputs(
    transcript_dir: Path,
    basename: str,
    entries: list[TranscriptEntry],
    text: str,
) -> tuple[Path, Path, Path]:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    srt_path = transcript_dir / f"{basename}_完整转写.srt"
    txt_path = transcript_dir / f"{basename}_完整转写.txt"
    json_path = transcript_dir / f"{basename}_完整转写.json"

    with srt_path.open("w", encoding="utf-8") as handle:
        for index, entry in enumerate(entries, 1):
            handle.write(f"{index}\n")
            handle.write(f"{fmt_srt_time(entry.start)} --> {fmt_srt_time(entry.end)}\n")
            handle.write(f"{entry.text.strip()}\n\n")

    txt_path.write_text(text.strip() + "\n", encoding="utf-8")
    payload = {
        "text": text.strip(),
        "segments": [
            {"start": entry.start, "end": entry.end, "text": entry.text}
            for entry in entries
        ],
        "language": "zh",
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return srt_path, txt_path, json_path


def normalize_for_similarity(text: str) -> str:
    text = RESTART_MARK_RE.sub("", text.lower())
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text))


def char_ngrams(text: str, size: int = 2) -> set[str]:
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def containment_similarity(candidate: str, target: str) -> float:
    candidate_grams = char_ngrams(candidate)
    target_grams = char_ngrams(target)
    if not candidate_grams or not target_grams:
        return 0.0
    return len(candidate_grams & target_grams) / len(target_grams)


def collect_text_after(entries: list[TranscriptEntry], start_index: int, max_seconds: float, min_chars: int = 40) -> str:
    parts: list[str] = []
    limit_time = entries[start_index].end + max_seconds
    for entry in entries[start_index + 1 :]:
        if entry.start > limit_time:
            break
        parts.append(entry.text)
        if len(normalize_for_similarity("".join(parts))) >= min_chars:
            break
    return normalize_for_similarity("".join(parts))


def detect_restart_cuts(
    entries: list[TranscriptEntry],
    delete_after: float,
    search_before: float,
    search_after: float,
    similarity_threshold: float,
) -> list[Cut]:
    cuts: list[Cut] = []
    for index, entry in enumerate(entries):
        if not RESTART_MARK_RE.search(entry.text):
            continue
        after_text = collect_text_after(entries, index, search_after)
        if not after_text:
            continue
        best_score = 0.0
        best_start: float | None = None

        previous: list[TranscriptEntry] = []
        for candidate in reversed(entries[:index]):
            if candidate.end < entry.start - search_before:
                break
            previous.insert(0, candidate)

        for start_index in range(len(previous)):
            candidate_text = normalize_for_similarity("".join(item.text for item in previous[start_index:]))
            score = containment_similarity(candidate_text, after_text)
            if score > best_score:
                best_score = score
                best_start = previous[start_index].start

        if best_start is None or best_score < similarity_threshold:
            continue

        reason = f"321 重启标记自动识别，剪掉标记前重复 take，相似度 {best_score:.2f}"
        raw_start = best_start
        raw_end = entry.end + delete_after
        if raw_end > raw_start:
            cuts.append(Cut(raw_start, raw_end, reason, fmt_time(raw_start), fmt_time(raw_end)))
    return cuts


def is_filler_heavy(text: str) -> bool:
    return any(text.count(word) >= 3 for word in FILLER_WORDS) or "呃呃" in text


def suggest_cuts_from_entries(
    entries: list[TranscriptEntry],
    restart_delete_after: float,
    restart_search_before: float,
    restart_search_after: float,
    restart_similarity_threshold: float,
    max_filler_suggestions: int,
) -> list[Cut]:
    suggestions: list[Cut] = []
    for cut in detect_restart_cuts(
        entries,
        restart_delete_after,
        restart_search_before,
        restart_search_after,
        restart_similarity_threshold,
    ):
        reason = f"321 重启候选：{cut.reason}；需人工复核，不自动删除"
        suggestions.append(Cut(cut.start, cut.end, reason, cut.raw_start, cut.raw_end))

    filler_count = 0
    for entry in entries:
        if filler_count >= max_filler_suggestions:
            break
        if not is_filler_heavy(entry.text):
            continue
        reason = f"口头禅候选：{entry.text.strip()}；需人工复核，不自动删除"
        suggestions.append(Cut(entry.start, entry.end, reason, fmt_time(entry.start), fmt_time(entry.end)))
        filler_count += 1

    return sorted(suggestions, key=lambda item: item.start)


def write_suggestions_json(path: Path, suggestions: list[Cut]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "delete_candidates": [
            {
                "start": fmt_time(cut.start),
                "end": fmt_time(cut.end),
                "start_seconds": cut.start,
                "end_seconds": cut.end,
                "reason": cut.reason,
            }
            for cut in suggestions
        ],
        "note": "候选剪辑点只供人工复核；脚本不会自动把这些候选段加入实际剪辑。",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def buffered_delete_segments(cuts: list[Cut], media_duration: float, buffer_seconds: float) -> list[Cut]:
    adjusted: list[Cut] = []
    for cut in cuts:
        start = max(0.0, cut.start + buffer_seconds)
        end = min(media_duration, cut.end - buffer_seconds)
        if end <= start:
            continue
        adjusted.append(Cut(start, end, cut.reason, cut.raw_start, cut.raw_end))
    if not adjusted:
        return []

    merged: list[Cut] = []
    for cut in adjusted:
        if not merged or cut.start > merged[-1].end:
            merged.append(cut)
            continue
        prev = merged[-1]
        prev.end = max(prev.end, cut.end)
        if cut.reason and cut.reason not in prev.reason:
            prev.reason = (prev.reason + "；" + cut.reason).strip("；")
        prev.raw_end = cut.raw_end
    return merged


def keep_segments(delete_segments: list[Cut], media_duration: float) -> list[Segment]:
    keeps: list[Segment] = []
    cursor = 0.0
    for cut in delete_segments:
        if cut.start > cursor:
            keeps.append(Segment(cursor, cut.start))
        cursor = max(cursor, cut.end)
    if cursor < media_duration:
        keeps.append(Segment(cursor, media_duration))
    return [seg for seg in keeps if seg.duration >= 0.02]


def splice_markers(keeps: list[Segment]) -> list[tuple[float, float, float]]:
    """Return marker rows as (new_time, source_left_end, source_right_start)."""
    markers: list[tuple[float, float, float]] = []
    cursor = 0.0
    for index, seg in enumerate(keeps[:-1]):
        cursor += seg.duration
        markers.append((cursor, seg.end, keeps[index + 1].start))
    return markers


def silence_marker_rows(keeps: list[Segment], silence_duration: float) -> list[tuple[float, float, float, float, float]]:
    """Return rows as (gap_start, gap_end, source_left_end, source_right_start, cumulative_offset)."""
    rows: list[tuple[float, float, float, float, float]] = []
    cursor = 0.0
    cumulative_offset = 0.0
    for index, seg in enumerate(keeps[:-1]):
        cursor += seg.duration
        gap_start = cursor + cumulative_offset
        gap_end = gap_start + silence_duration
        cumulative_offset += silence_duration
        rows.append((gap_start, gap_end, seg.end, keeps[index + 1].start, cumulative_offset))
    return rows


def default_sidecar(output: Path, suffix: str) -> Path:
    return output.with_name(output.stem + suffix)


def validate_black_video_settings(width: int, height: int, fps: float, crf: int) -> None:
    if width <= 0 or height <= 0:
        raise RuntimeError("--black-video-width and --black-video-height must be greater than 0")
    if width % 2 or height % 2:
        raise RuntimeError("--black-video-width and --black-video-height must be even numbers for yuv420p")
    if fps <= 0:
        raise RuntimeError("--black-video-fps must be greater than 0")
    if not 0 <= crf <= 51:
        raise RuntimeError("--black-video-crf must be between 0 and 51")


def black_video_output_path(audio_output: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return default_sidecar(audio_output, "_剪映黑场.mp4")


def ffmpeg_black_video(
    audio_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    audio_bitrate: str,
    crf: int,
) -> None:
    if output_path.resolve() == audio_path.resolve():
        raise RuntimeError("Refusing to overwrite audio output with black video")

    media_duration = duration(audio_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={media_duration:.6f}",
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-t",
        f"{media_duration:.6f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])


def build_filter(
    keeps: list[Segment],
    process: bool,
    target_lufs: float,
    true_peak: float,
    lra: float,
    splice_marker_mode: str = "none",
    splice_silence_duration: float = 2.0,
) -> str:
    parts: list[str] = []
    labels: list[str] = []
    for index, seg in enumerate(keeps):
        label = f"a{index}"
        parts.append(f"[0:a]atrim=start={seg.start:.3f}:end={seg.end:.3f},asetpts=PTS-STARTPTS[{label}]")
        labels.append(f"[{label}]")
    if not labels:
        raise ValueError("No audio remains after cuts")

    if splice_marker_mode == "silence-insert" and len(labels) > 1:
        interleaved_labels: list[str] = []
        for index, label in enumerate(labels):
            interleaved_labels.append(label)
            if index < len(labels) - 1:
                silence_label = f"sil{index}"
                parts.append(f"anullsrc=r=48000:cl=mono:d={splice_silence_duration:.3f}[{silence_label}]")
                interleaved_labels.append(f"[{silence_label}]")
        labels = interleaved_labels

    concat_label = "joined"
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[{concat_label}]")
    processed_label = "processed"
    if process:
        limiter = db_to_linear(true_peak)
        chain = (
            f"[{concat_label}]"
            "dynaudnorm=f=250:g=8:p=0.85:m=8:s=9,"
            "acompressor=threshold=-18dB:ratio=2.2:attack=15:release=250:makeup=1.5,"
            f"alimiter=limit={limiter:.6f},"
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}:print_format=summary[{processed_label}]"
        )
    else:
        chain = f"[{concat_label}]anull[{processed_label}]"
    parts.append(chain)
    parts.append(f"[{processed_label}]anull[outa]")
    return ";".join(parts)


def db_to_linear(db_value: float) -> float:
    return 10 ** (db_value / 20)


def ffmpeg_cut(
    input_path: Path,
    output_path: Path,
    keeps: list[Segment],
    process: bool,
    target_lufs: float,
    true_peak: float,
    lra: float,
    splice_marker_mode: str = "none",
    splice_silence_duration: float = 2.0,
) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = build_filter(
        keeps,
        process,
        target_lufs,
        true_peak,
        lra,
        splice_marker_mode,
        splice_silence_duration,
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[outa]",
        "-vn",
        "-ar",
        "48000",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return result.stderr


def loudness_json(path: Path, target_lufs: float, true_peak: float, lra: float) -> dict:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    result = run(cmd, check=False)
    match = re.search(r"\{\s*\"input_i\".*?\}", result.stderr, re.S)
    if not match:
        return {"error": "loudnorm json not found", "stderr_tail": result.stderr[-1000:]}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"error": str(exc), "raw": match.group(0)}


def validate_boundaries(output_path: Path, keeps: list[Segment], sample_seconds: float = 2.4) -> tuple[int, list[str]]:
    failures: list[str] = []
    cursor = 0.0
    boundaries: list[float] = []
    for seg in keeps[:-1]:
        cursor += seg.duration
        boundaries.append(cursor)
    for index, boundary in enumerate(boundaries, 1):
        start = max(0.0, boundary - sample_seconds / 2)
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{sample_seconds:.3f}",
            "-i",
            str(output_path),
            "-f",
            "null",
            "-",
        ]
        result = run(cmd, check=False)
        if result.returncode != 0:
            failures.append(f"boundary {index} at {fmt_time(boundary)}: {result.stderr.strip()}")
    return len(boundaries), failures


def validate_times(output_path: Path, boundary_times: list[float], sample_seconds: float = 2.4) -> tuple[int, list[str]]:
    failures: list[str] = []
    for index, boundary in enumerate(boundary_times, 1):
        start = max(0.0, boundary - sample_seconds / 2)
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{sample_seconds:.3f}",
            "-i",
            str(output_path),
            "-f",
            "null",
            "-",
        ]
        result = run(cmd, check=False)
        if result.returncode != 0:
            failures.append(f"boundary {index} at {fmt_time(boundary)}: {result.stderr.strip()}")
    return len(boundary_times), failures


def write_manifest(
    manifest_path: Path,
    input_path: Path,
    output_path: Path | None,
    media_duration: float,
    output_duration: float | None,
    cuts: list[Cut],
    deletes: list[Cut],
    keeps: list[Segment],
    buffer_seconds: float,
    process: bool,
    boundary_count: int | None = None,
    boundary_failures: list[str] | None = None,
    splice_marker_mode: str = "none",
    silence_rows: list[tuple[float, float, float, float, float]] | None = None,
    splice_silence_duration: float = 2.0,
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    total_removed = sum(cut.end - cut.start for cut in deletes)
    silence_extra = len(silence_rows or []) * splice_silence_duration
    expected = media_duration - total_removed + silence_extra
    with manifest_path.open("w", encoding="utf-8") as handle:
        handle.write("播客粗剪清单\n")
        handle.write("=" * 40 + "\n\n")
        handle.write(f"输入文件：{input_path}\n")
        if output_path:
            handle.write(f"输出文件：{output_path}\n")
        handle.write(f"原始时长：{fmt_time(media_duration)}\n")
        handle.write(f"预计成片时长：{fmt_time(expected)}\n")
        if output_duration is not None:
            handle.write(f"实际成片时长：{fmt_time(output_duration)}\n")
        handle.write(f"切口缓冲：每段前后各保留 {buffer_seconds:.3g} 秒\n")
        handle.write(f"音频处理：{'动态均衡 + 压缩 + 限幅 + 响度标准化' if process else '仅剪辑拼接'}\n")
        if splice_marker_mode == "silence-insert":
            handle.write(
                f"剪辑点声音标记：静音缺口，插入 {splice_silence_duration:.3g} 秒，"
                f"累计增加 {fmt_time(silence_extra)}；仅用于后期定位\n"
            )
        else:
            handle.write("剪辑点声音标记：关闭\n")
        handle.write(f"原始删除段数：{len(cuts)}\n")
        handle.write(f"实际删除段数：{len(deletes)}\n")
        handle.write(f"预计节省时长：{fmt_time(total_removed)}\n\n")

        handle.write("实际删除段\n")
        handle.write("-" * 40 + "\n")
        if not deletes:
            handle.write("无。\n")
        for index, cut in enumerate(deletes, 1):
            handle.write(
                f"{index}. 原标注 {cut.raw_start}-{cut.raw_end}；"
                f"实际删除 {fmt_time(cut.start)}-{fmt_time(cut.end)}；"
                f"节省 {fmt_time(cut.end - cut.start)}；理由：{cut.reason or '未填写'}\n"
            )

        handle.write("\n保留段\n")
        handle.write("-" * 40 + "\n")
        for index, seg in enumerate(keeps, 1):
            handle.write(f"{index}. {fmt_time(seg.start)}-{fmt_time(seg.end)}；时长 {fmt_time(seg.duration)}\n")

        if splice_marker_mode == "silence-insert":
            rows = silence_rows or []
            handle.write("\n静音缺口标记点\n")
            handle.write("-" * 40 + "\n")
            if not rows:
                handle.write("无拼接点。\n")
            for index, (gap_start, gap_end, left_end, right_start, cumulative_offset) in enumerate(rows, 1):
                handle.write(
                    f"{index}. 新音频静音 {fmt_time(gap_start)}-{fmt_time(gap_end)}；"
                    f"原音频左侧结束 {fmt_time(left_end)}；"
                    f"原音频右侧开始 {fmt_time(right_start)}；"
                    f"累计偏移 +{fmt_time(cumulative_offset)}\n"
                )

        if boundary_count is not None:
            handle.write("\n拼接点解码检查\n")
            handle.write("-" * 40 + "\n")
            handle.write(f"检查拼接点：{boundary_count} 个\n")
            handle.write(f"失败：{len(boundary_failures or [])} 个\n")
            for failure in boundary_failures or []:
                handle.write(f"- {failure}\n")


def write_loudness_report(
    report_path: Path,
    input_path: Path,
    output_path: Path,
    target_lufs: float,
    true_peak: float,
    lra: float,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    original = loudness_json(input_path, target_lufs, true_peak, lra)
    processed = loudness_json(output_path, target_lufs, true_peak, lra)
    with report_path.open("w", encoding="utf-8") as handle:
        handle.write("播客响度报告\n")
        handle.write("=" * 40 + "\n\n")
        handle.write(f"输入文件：{input_path}\n")
        handle.write(f"输出文件：{output_path}\n")
        handle.write(f"目标响度：{target_lufs} LUFS\n")
        handle.write(f"目标 True Peak：{true_peak} dBTP\n")
        handle.write(f"目标 LRA：{lra}\n\n")
        handle.write("原始音频\n")
        handle.write("-" * 40 + "\n")
        write_loudness_block(handle, original)
        handle.write("\n输出音频\n")
        handle.write("-" * 40 + "\n")
        write_loudness_block(handle, processed)
        handle.write("\n说明\n")
        handle.write("-" * 40 + "\n")
        handle.write("单声道素材无法真正分离两位主讲人；本报告反映整体动态处理结果。\n")


def write_loudness_block(handle, data: dict) -> None:
    if "error" in data:
        handle.write(f"读取失败：{data}\n")
        return
    keys = [
        ("input_i", "Integrated LUFS"),
        ("input_tp", "True Peak dBTP"),
        ("input_lra", "LRA"),
        ("input_thresh", "Threshold"),
        ("target_offset", "Target Offset"),
    ]
    for key, label in keys:
        if key in data:
            handle.write(f"{label}：{data[key]}\n")


def find_asr_script(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.exists():
            return path
        raise FileNotFoundError(f"ASR script not found: {path}")
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "scripts" / "siliconflow_asr.py"
        if candidate.exists():
            return candidate
    default_candidates = [
        Path("~/.agents/skills/shownotes/scripts/siliconflow_asr.py").expanduser(),
        Path("~/.claude/skills/shownotes/scripts/siliconflow_asr.py").expanduser(),
        Path("$HOME/workspace/media/scripts/siliconflow_asr.py"),
    ]
    for default in default_candidates:
        if default.exists():
            return default
    raise FileNotFoundError("Could not find scripts/siliconflow_asr.py")


def extract_audio(input_path: Path, audio_output: Path) -> Path:
    audio_output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(audio_output),
    ]
    result = run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:])
    return audio_output


def transcribe(
    input_path: Path,
    transcript_dir: Path,
    asr_script: Path,
    basename: str,
) -> tuple[Path, Path]:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    srt_path = transcript_dir / f"{basename}_完整转写.srt"
    txt_path = transcript_dir / f"{basename}_完整转写.txt"
    for fmt, output in [("srt", srt_path), ("text", txt_path)]:
        cmd = [sys.executable, str(asr_script), str(input_path), str(output), "--format", fmt]
        result = run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
    return srt_path, txt_path


def direct_asr_api_key() -> str:
    key = os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("SF_KEY")
    if key:
        return key
    key_file = Path("~/.config/siliconflow/api_key").expanduser()
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise RuntimeError(
        "未找到硅基流动 API key。请设置 SILICONFLOW_API_KEY / SF_KEY，"
        "或写入 ~/.config/siliconflow/api_key"
    )


def split_audio_for_direct_asr(input_path: Path, segment_seconds: float, workspace: Path) -> list[tuple[Path, float]]:
    media_duration = duration(input_path)
    if media_duration <= segment_seconds:
        return [(input_path, 0.0)]

    chunks: list[tuple[Path, float]] = []
    start = 0.0
    index = 0
    while start < media_duration:
        chunk_duration = min(segment_seconds, media_duration - start)
        chunk_path = workspace / f"direct_asr_segment_{index:03d}.mp3"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{chunk_duration:.3f}",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "96k",
            str(chunk_path),
        ]
        result = run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-4000:])
        chunks.append((chunk_path, start))
        start += chunk_duration
        index += 1
    return chunks


def split_audio_chunk(input_path: Path, workspace: Path, prefix: str) -> list[tuple[Path, float]]:
    chunk_duration = duration(input_path)
    midpoint = chunk_duration / 2
    chunks: list[tuple[Path, float]] = []
    for index, (start, length) in enumerate([(0.0, midpoint), (midpoint, chunk_duration - midpoint)]):
        if length <= 0:
            continue
        chunk_path = workspace / f"{prefix}_retry_{index}.mp3"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "96k",
            str(chunk_path),
        ]
        result = run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-4000:])
        chunks.append((chunk_path, start))
    return chunks


def post_direct_asr(chunk_path: Path, api_key: str) -> dict:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("直连 ASR 需要 Python requests 包；可改用 --asr-script 外部转写。") from exc

    headers = {"Authorization": f"Bearer {api_key}"}
    request_variants = [
        ("verbose_json", {"model": ASR_MODEL, "response_format": "verbose_json", "language": "zh"}),
        ("json", {"model": ASR_MODEL, "response_format": "json", "language": "zh"}),
        ("plain", {"model": ASR_MODEL}),
    ]
    failures: list[str] = []
    for label, data in request_variants:
        with chunk_path.open("rb") as handle:
            files = {"file": (chunk_path.name, handle, "application/octet-stream")}
            response = requests.post(
                f"{BASE_URL}/audio/transcriptions",
                headers=headers,
                files=files,
                data=data,
                timeout=ASR_TIMEOUT_SECONDS,
            )
        if response.status_code == 200:
            result = response.json()
            result.setdefault("_asr_response_format", label)
            return result
        failures.append(f"{label}: {response.status_code} {response.text[:300]}")
    raise RuntimeError("SiliconFlow ASR failed: " + " | ".join(failures))


def transcribe_direct_asr_chunk(
    chunk_path: Path,
    offset: float,
    api_key: str,
    workspace: Path,
    min_retry_seconds: float = 30.0,
) -> tuple[list[TranscriptEntry], str, list[str]]:
    chunk_duration = duration(chunk_path)
    try:
        result = post_direct_asr(chunk_path, api_key)
        entries = entries_from_direct_asr_result(result, offset, chunk_duration)
        text = str(result.get("text", "")).strip()
        return entries, text, []
    except RuntimeError as exc:
        if chunk_duration <= min_retry_seconds:
            warning = (
                f"ASR chunk skipped after retries: {chunk_path.name} "
                f"offset={fmt_time(offset)} duration={fmt_time(chunk_duration)} error={exc}"
            )
            print(warning, file=sys.stderr)
            return [], "", [warning]

        warnings: list[str] = []
        entries: list[TranscriptEntry] = []
        texts: list[str] = []
        retry_prefix = f"{chunk_path.stem}_{int(offset * 1000)}"
        for retry_path, relative_offset in split_audio_chunk(chunk_path, workspace, retry_prefix):
            retry_entries, retry_text, retry_warnings = transcribe_direct_asr_chunk(
                retry_path,
                offset + relative_offset,
                api_key,
                workspace,
                min_retry_seconds,
            )
            entries.extend(retry_entries)
            if retry_text:
                texts.append(retry_text)
            warnings.extend(retry_warnings)
        return entries, " ".join(texts).strip(), warnings


def split_transcript_text(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    sentences: list[str] = []
    current: list[str] = []
    for char in text:
        current.append(char)
        if SENTENCE_END_RE.match(char):
            sentence = "".join(current).strip()
            if sentence:
                sentences.append(sentence)
            current = []
    tail = "".join(current).strip()
    if tail:
        sentences.append(tail)
    return sentences or [text]


def estimate_entries_from_text(text: str, offset: float, fallback_duration: float) -> list[TranscriptEntry]:
    sentences = split_transcript_text(text)
    if not sentences:
        return []
    total_chars = sum(max(1, len(sentence)) for sentence in sentences)
    cursor = offset
    entries: list[TranscriptEntry] = []
    for index, sentence in enumerate(sentences):
        if index == len(sentences) - 1:
            end = offset + fallback_duration
        else:
            end = cursor + fallback_duration * (max(1, len(sentence)) / total_chars)
        if end <= cursor:
            end = cursor + 0.01
        entries.append(TranscriptEntry(cursor, end, sentence))
        cursor = end
    return entries


def entries_from_direct_asr_result(result: dict, offset: float, fallback_duration: float) -> list[TranscriptEntry]:
    entries: list[TranscriptEntry] = []
    for segment in result.get("segments") or []:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        start = float(segment.get("start", 0.0)) + offset
        end = float(segment.get("end", segment.get("start", 0.0))) + offset
        if end <= start:
            end = start + 0.01
        entries.append(TranscriptEntry(start, end, text))
    if entries:
        return entries

    text = str(result.get("text", "")).strip()
    if text:
        return estimate_entries_from_text(text, offset, fallback_duration)
    return []


def direct_transcribe(
    input_path: Path,
    transcript_dir: Path,
    basename: str,
    segment_seconds: float,
) -> tuple[Path, Path, Path]:
    api_key = direct_asr_api_key()
    all_entries: list[TranscriptEntry] = []
    all_text: list[str] = []
    with tempfile.TemporaryDirectory(prefix="podcast-direct-asr-") as tmp:
        workspace = Path(tmp)
        chunks = split_audio_for_direct_asr(input_path, segment_seconds, workspace)
        for index, (chunk_path, offset) in enumerate(chunks, 1):
            print(
                f"ASR chunk {index}/{len(chunks)} offset={fmt_time(offset)} "
                f"duration={fmt_time(duration(chunk_path))}",
                file=sys.stderr,
            )
            entries, text, _warnings = transcribe_direct_asr_chunk(chunk_path, offset, api_key, workspace)
            print(
                f"ASR chunk {index}/{len(chunks)} done entries={len(entries)} text_chars={len(text)}",
                file=sys.stderr,
            )
            all_entries.extend(entries)
            if text:
                all_text.append(text)

    all_entries.sort(key=lambda item: item.start)
    text = " ".join(all_text).strip() or " ".join(entry.text for entry in all_entries).strip()
    return write_transcript_outputs(transcript_dir, basename, all_entries, text)


def ensure_input_for_asr(args: argparse.Namespace, input_path: Path) -> Path:
    if args.extract_audio_output:
        return extract_audio(input_path, Path(args.extract_audio_output).expanduser().resolve())
    if input_path.suffix.lower() in VIDEO_EXTENSIONS:
        transcript_dir = Path(args.transcript_dir).expanduser().resolve() if args.transcript_dir else input_path.parent
        audio_output = transcript_dir.parent / "audio" / f"{input_path.stem}_原始音频.m4a"
        return extract_audio(input_path, audio_output)
    return input_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Podcast rough cut, ASR handoff, and loudness processing")
    parser.add_argument("--input", required=True, help="Original audio or video file")
    parser.add_argument("--cuts", help="TSV with columns: start, end, reason")
    parser.add_argument("--output", help="Output M4A path")
    parser.add_argument("--manifest", help="Cut manifest path")
    parser.add_argument("--loudness-report", help="Loudness report path")
    parser.add_argument("--buffer-seconds", type=float, default=1.0, help="Seconds preserved at both sides of each delete range")
    parser.add_argument("--dry-run", action="store_true", help="Only calculate cut plan")
    parser.add_argument("--no-processing", action="store_true", help="Skip dynaudnorm/compressor/limiter/loudnorm")
    parser.add_argument("--target-lufs", type=float, default=-16.0)
    parser.add_argument("--true-peak", type=float, default=-1.5)
    parser.add_argument("--lra", type=float, default=11.0)
    parser.add_argument(
        "--splice-marker-mode",
        choices=["none", "silence-insert"],
        default="none",
        help="Splice marker mode. silence-insert adds silent gaps for edit-point location.",
    )
    parser.add_argument("--splice-silence-duration", type=float, default=2.0, help="Inserted silence duration per splice when mode is silence-insert")
    parser.add_argument("--mark-splices", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--marker-frequency", type=float, default=1000.0, help=argparse.SUPPRESS)
    parser.add_argument("--marker-duration", type=float, default=0.18, help=argparse.SUPPRESS)
    parser.add_argument("--marker-level-db", type=float, default=-1.0, help=argparse.SUPPRESS)
    parser.add_argument("--restart-transcript", help="SRT transcript used to detect 321 restart markers and append restart cuts")
    parser.add_argument("--restart-cuts-output", help="Optional TSV path for generated 321 restart cuts")
    parser.add_argument("--restart-delete-after", type=float, default=1.0, help="Seconds included after a matched 321 marker")
    parser.add_argument("--restart-search-before", type=float, default=30.0, help="Seconds before marker searched for repeated take text")
    parser.add_argument("--restart-search-after", type=float, default=30.0, help="Seconds after marker used as the restarted take reference")
    parser.add_argument("--restart-similarity-threshold", type=float, default=0.35, help="Similarity threshold for repeated take detection")
    parser.add_argument("--transcribe", action="store_true", help="Generate SRT and TXT transcript using existing SiliconFlow ASR script")
    parser.add_argument("--transcribe-only", action="store_true", help="Only generate transcript, do not cut audio")
    parser.add_argument("--direct-asr", action="store_true", help="Use built-in SiliconFlow TeleSpeechASR instead of an external ASR script")
    parser.add_argument("--direct-asr-segment-seconds", type=float, default=3000.0, help="Chunk length for built-in direct ASR")
    parser.add_argument("--transcript-dir", help="Directory for SRT/TXT outputs")
    parser.add_argument("--asr-script", help="Path to scripts/siliconflow_asr.py")
    parser.add_argument("--extract-audio-output", help="Optional M4A path to extract audio before ASR")
    parser.add_argument("--suggest-cuts", nargs="?", const="auto", help="Generate candidate cuts from an SRT transcript; omit value after --transcribe to use generated SRT")
    parser.add_argument("--suggest-cuts-output", help="TSV path for candidate cuts")
    parser.add_argument("--suggest-cuts-json", help="JSON path for candidate cuts")
    parser.add_argument("--max-filler-suggestions", type=int, default=20, help="Maximum filler-word candidate cuts")
    parser.add_argument("--no-black-video", action="store_true", help="Skip the default Jianying/CapCut black-screen MP4 sidecar")
    parser.add_argument("--black-video-output", help="Optional MP4 path for the black-screen video sidecar")
    parser.add_argument("--black-video-width", type=int, default=16, help="Black-screen video width, default 16")
    parser.add_argument("--black-video-height", type=int, default=16, help="Black-screen video height, default 16")
    parser.add_argument("--black-video-fps", type=float, default=25.0, help="Black-screen video frame rate, default 25")
    parser.add_argument("--black-video-audio-bitrate", default="192k", help="AAC bitrate for black-screen video audio")
    parser.add_argument("--black-video-crf", type=int, default=35, help="H.264 CRF for black-screen video, default 35")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mark_splices and args.splice_marker_mode == "none":
        args.splice_marker_mode = "silence-insert"

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    generated_srt_path: Path | None = None
    if args.transcribe or args.transcribe_only:
        asr_input = ensure_input_for_asr(args, input_path)
        transcript_dir = Path(args.transcript_dir).expanduser().resolve() if args.transcript_dir else input_path.parent
        if args.direct_asr:
            if args.direct_asr_segment_seconds <= 0:
                raise RuntimeError("--direct-asr-segment-seconds must be greater than 0")
            srt_path, txt_path, json_path = direct_transcribe(
                asr_input,
                transcript_dir,
                input_path.stem,
                args.direct_asr_segment_seconds,
            )
        else:
            try:
                asr_script = find_asr_script(args.asr_script)
                srt_path, txt_path = transcribe(asr_input, transcript_dir, asr_script, input_path.stem)
                json_path = None
            except FileNotFoundError:
                srt_path, txt_path, json_path = direct_transcribe(
                    asr_input,
                    transcript_dir,
                    input_path.stem,
                    args.direct_asr_segment_seconds,
                )
        generated_srt_path = srt_path
        print(f"SRT: {srt_path}")
        print(f"TXT: {txt_path}")
        if json_path:
            print(f"JSON: {json_path}")
        if args.transcribe_only:
            return 0

    if args.suggest_cuts:
        if args.suggest_cuts == "auto":
            if generated_srt_path is None:
                raise SystemExit("--suggest-cuts without a path requires --transcribe")
            suggest_srt_path = generated_srt_path
        else:
            suggest_srt_path = Path(args.suggest_cuts).expanduser().resolve()
        entries = read_srt(suggest_srt_path)
        suggestions = suggest_cuts_from_entries(
            entries,
            args.restart_delete_after,
            args.restart_search_before,
            args.restart_search_after,
            args.restart_similarity_threshold,
            args.max_filler_suggestions,
        )
        suggestion_tsv = (
            Path(args.suggest_cuts_output).expanduser().resolve()
            if args.suggest_cuts_output
            else default_sidecar(suggest_srt_path, "_候选剪辑段.tsv")
        )
        suggestion_json = (
            Path(args.suggest_cuts_json).expanduser().resolve()
            if args.suggest_cuts_json
            else suggestion_tsv.with_suffix(".json")
        )
        write_cuts(suggestion_tsv, suggestions)
        write_suggestions_json(suggestion_json, suggestions)
        print(f"Suggested cuts: {len(suggestions)}")
        print(f"Suggestion TSV: {suggestion_tsv}")
        print(f"Suggestion JSON: {suggestion_json}")

    if not args.cuts and not args.restart_transcript:
        if args.suggest_cuts:
            return 0
        raise SystemExit("--cuts or --restart-transcript is required unless --transcribe-only is used")

    cuts = read_cuts(Path(args.cuts).expanduser().resolve()) if args.cuts else []
    restart_cuts: list[Cut] = []
    if args.restart_transcript:
        restart_entries = read_srt(Path(args.restart_transcript).expanduser().resolve())
        restart_cuts = detect_restart_cuts(
            restart_entries,
            args.restart_delete_after,
            args.restart_search_before,
            args.restart_search_after,
            args.restart_similarity_threshold,
        )
        if args.restart_cuts_output:
            write_cuts(Path(args.restart_cuts_output).expanduser().resolve(), restart_cuts)
        cuts.extend(restart_cuts)
        cuts.sort(key=lambda item: item.start)
    media_duration = duration(input_path)
    deletes = buffered_delete_segments(cuts, media_duration, args.buffer_seconds)
    keeps = keep_segments(deletes, media_duration)
    silence_rows = silence_marker_rows(keeps, args.splice_silence_duration) if args.splice_marker_mode == "silence-insert" else []
    silence_extra = len(silence_rows) * args.splice_silence_duration
    expected_duration = media_duration - sum(cut.end - cut.start for cut in deletes) + silence_extra

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    else:
        output_path = input_path.with_name(input_path.stem + "_rough_cut.m4a")
    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest else default_sidecar(output_path, "_剪辑清单.txt")
    report_path = Path(args.loudness_report).expanduser().resolve() if args.loudness_report else default_sidecar(output_path, "_响度报告.txt")
    black_video_path = black_video_output_path(output_path, args.black_video_output)
    process = not args.no_processing

    print(f"Input duration: {fmt_time(media_duration)}")
    print(f"Delete ranges: {len(deletes)}")
    if restart_cuts:
        print(f"321 restart cuts: {len(restart_cuts)}")
    print(f"Removed: {fmt_time(sum(cut.end - cut.start for cut in deletes))}")
    print(f"Expected output: {fmt_time(expected_duration)}")
    if args.splice_marker_mode == "silence-insert":
        print(f"Silence splice markers: {len(silence_rows)} x {args.splice_silence_duration:g}s")

    if args.dry_run:
        write_manifest(
            manifest_path,
            input_path,
            None,
            media_duration,
            None,
            cuts,
            deletes,
            keeps,
            args.buffer_seconds,
            process,
            splice_marker_mode=args.splice_marker_mode,
            silence_rows=silence_rows,
            splice_silence_duration=args.splice_silence_duration,
        )
        print(f"Dry-run manifest: {manifest_path}")
        return 0

    if output_path.exists() and output_path.resolve() == input_path.resolve():
        raise RuntimeError("Refusing to overwrite input file")

    if args.splice_silence_duration <= 0:
        raise RuntimeError("--splice-silence-duration must be greater than 0")
    if not args.no_black_video:
        validate_black_video_settings(
            args.black_video_width,
            args.black_video_height,
            args.black_video_fps,
            args.black_video_crf,
        )

    ffmpeg_cut(
        input_path,
        output_path,
        keeps,
        process,
        args.target_lufs,
        args.true_peak,
        args.lra,
        args.splice_marker_mode,
        args.splice_silence_duration,
    )
    output_duration = duration(output_path)
    if args.splice_marker_mode == "silence-insert":
        boundary_count, boundary_failures = validate_times(output_path, [row[0] for row in silence_rows])
    else:
        boundary_count, boundary_failures = validate_boundaries(output_path, keeps)
    write_manifest(
        manifest_path,
        input_path,
        output_path,
        media_duration,
        output_duration,
        cuts,
        deletes,
        keeps,
        args.buffer_seconds,
        process,
        boundary_count,
        boundary_failures,
        args.splice_marker_mode,
        silence_rows,
        args.splice_silence_duration,
    )
    write_loudness_report(report_path, input_path, output_path, args.target_lufs, args.true_peak, args.lra)
    black_video_duration: float | None = None
    if not args.no_black_video:
        ffmpeg_black_video(
            output_path,
            black_video_path,
            args.black_video_width,
            args.black_video_height,
            args.black_video_fps,
            args.black_video_audio_bitrate,
            args.black_video_crf,
        )
        black_video_duration = duration(black_video_path)

    print(f"Output: {output_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Loudness report: {report_path}")
    if black_video_duration is not None:
        print(f"Black video: {black_video_path}")
        print(f"Black video duration: {fmt_time(black_video_duration)}")
    print(f"Output duration: {fmt_time(output_duration)}")
    if boundary_failures:
        print(f"Boundary decode failures: {len(boundary_failures)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
