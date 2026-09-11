#!/usr/bin/env python3
"""Dead-air video editor for the Claude E2E Test Agent workflow.

Condenses the sandbox screen recording for high information density before it
is published to the PR comment: stretches where nothing changes on screen
(and, when the recording has an audio track, nothing is heard) are detected
with ffmpeg's freezedetect/silencedetect filters and cut from the timeline,
keeping a little context on both sides of every cut.

The edit is expressed as a Shotcut (https://www.shotcut.org/) project file --
an MLT XML timeline of the kept segments -- so a human can open the exact cut
in Shotcut and adjust it. Rendering uses Shotcut's engine, the MLT `melt`
CLI, with a pure-ffmpeg fallback when melt is unavailable or fails.

Stdlib-only; external processes: ffprobe, ffmpeg, melt.

Exit code 0 means the editor finished: either the edited video was written or
it decided the recording has too little dead air to be worth cutting (no
output file is written in that case). Non-zero means the edit failed and the
caller should publish the unedited recording.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

FREEZE_START_RE = re.compile(r"freeze_start:\s*(-?\d+(?:\.\d+)?)")
FREEZE_END_RE = re.compile(r"freeze_end:\s*(-?\d+(?:\.\d+)?)")
SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def log(msg: str) -> None:
    print(f"[edit-video] {msg}", file=sys.stderr, flush=True)


def run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ffprobe_meta(path: str) -> dict:
    proc = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
        "-of", "json", path,
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()[-500:]}")
    data = json.loads(proc.stdout)
    meta = {
        "duration": float(data["format"]["duration"]),
        "width": 1280,
        "height": 720,
        "fps_num": 25,
        "fps_den": 1,
        "has_audio": False,
    }
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("width"):
            meta["width"] = int(stream["width"])
            meta["height"] = int(stream["height"])
            num, _, den = str(stream.get("r_frame_rate") or "25/1").partition("/")
            try:
                meta["fps_num"], meta["fps_den"] = int(num), max(1, int(den or "1"))
            except ValueError:
                pass
            # ffprobe reports 0/0 for some streams; a 0-fps MLT profile
            # would make melt fail before the ffmpeg fallback even runs.
            if meta["fps_num"] <= 0:
                meta["fps_num"], meta["fps_den"] = 25, 1
        elif stream.get("codec_type") == "audio":
            meta["has_audio"] = True
    return meta


def merge_intervals(intervals, gap=0.0, merge_touching=True):
    """Coalesce overlapping intervals. With merge_touching (the default),
    intervals that merely share an endpoint also merge — right for cut
    ranges. Detector output must pass merge_touching=False: back-to-back
    freezes share the one frame where the screen actually changed (at low
    capture rates a UI change is a single frame, so freezedetect reports
    zero gap around it), and merging them erases that moment from the cut
    logic — a 20-minute recording of a real journey once collapsed into a
    single "frozen" interval this way and was cut to nothing."""
    merged = []
    for start, end in sorted(intervals):
        if merged:
            last_end = merged[-1][1]
            joins = start <= last_end + gap if merge_touching else start < last_end
        else:
            joins = False
        if joins:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _parse_intervals(text, duration, start_re, end_re):
    intervals, start = [], None
    for line in text.splitlines():
        started = start_re.search(line)
        if started:
            start = max(0.0, float(started.group(1)))
            continue
        ended = end_re.search(line)
        if ended and start is not None:
            end = min(duration, float(ended.group(1)))
            if end > start:
                intervals.append((start, end))
            start = None
    # A freeze/silence still open at EOF is reported without an end marker.
    if start is not None and start < duration:
        intervals.append((start, duration))
    # Touching detector intervals stay separate: their shared boundary is the
    # moment the screen (or audio) changed, which the cuts must keep.
    return merge_intervals(intervals, merge_touching=False)


def parse_freeze_intervals(text, duration):
    return _parse_intervals(text, duration, FREEZE_START_RE, FREEZE_END_RE)


def parse_silence_intervals(text, duration):
    return _parse_intervals(text, duration, SILENCE_START_RE, SILENCE_END_RE)


def intersect_intervals(first, second):
    result, i, j = [], 0, 0
    while i < len(first) and j < len(second):
        start = max(first[i][0], second[j][0])
        end = min(first[i][1], second[j][1])
        if end > start:
            result.append((start, end))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return result


def compute_cuts(dead, duration, pad, min_freeze):
    """Cut only dead intervals long enough to matter, keeping pad seconds on
    both sides so the state change before/after the dead air stays visible."""
    cuts = []
    for start, end in dead:
        if end - start < min_freeze:
            continue
        cut_start = max(0.0, start + pad)
        cut_end = min(duration, end - pad)
        if cut_end - cut_start >= 0.5:
            cuts.append((cut_start, cut_end))
    return merge_intervals(cuts)


def keep_segments(cuts, duration, min_keep=0.25):
    segments, cursor = [], 0.0
    for start, end in cuts:
        if start - cursor >= min_keep:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= min_keep:
        segments.append((cursor, duration))
    return segments


def worth_editing(total_cut, duration, min_total_cut, min_cut_ratio):
    return total_cut >= min_total_cut and total_cut >= min_cut_ratio * duration


def detect_freezes(path, meta, noise, min_hold):
    proc = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-map", "0:v:0", "-vf", f"freezedetect=n={noise}:d={min_hold}",
        "-an", "-f", "null", "-",
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"freezedetect failed: {proc.stderr.strip()[-500:]}")
    return parse_freeze_intervals(proc.stderr, meta["duration"])


def detect_silences(path, meta, noise, min_hold):
    proc = run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-map", "0:a:0", "-af", f"silencedetect=noise={noise}:d={min_hold}",
        "-vn", "-f", "null", "-",
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"silencedetect failed: {proc.stderr.strip()[-500:]}")
    return parse_silence_intervals(proc.stderr, meta["duration"])


def clock(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def mlt_project_xml(resource, segments, meta):
    """Shotcut (https://www.shotcut.org/) project: an MLT XML timeline of the
    kept segments, openable in Shotcut for manual adjustment and renderable
    with Shotcut's engine, melt."""
    source_out = clock(meta["duration"])
    entries = "\n".join(
        f'    <entry producer="chain0" in="{clock(start)}" out="{clock(end)}"/>'
        for start, end in segments
    )
    timeline_out = clock(sum(end - start for start, end in segments))
    return f"""<?xml version="1.0" standalone="no"?>
<mlt LC_NUMERIC="C" version="7.0.0" title="Shotcut e2e dead-air edit" producer="tractor0">
  <profile description="automatic" width="{meta['width']}" height="{meta['height']}" progressive="1" sample_aspect_num="1" sample_aspect_den="1" display_aspect_num="{meta['width']}" display_aspect_den="{meta['height']}" frame_rate_num="{meta['fps_num']}" frame_rate_den="{meta['fps_den']}" colorspace="709"/>
  <producer id="chain0" in="00:00:00.000" out="{source_out}">
    <property name="resource">{escape(resource)}</property>
    <property name="mlt_service">avformat</property>
    <property name="shotcut:caption">{escape(os.path.basename(resource))}</property>
  </producer>
  <playlist id="playlist0">
    <property name="shotcut:video">1</property>
    <property name="shotcut:name">V1</property>
{entries}
  </playlist>
  <tractor id="tractor0" title="Shotcut e2e dead-air edit" in="00:00:00.000" out="{timeline_out}">
    <property name="shotcut">1</property>
    <track producer="playlist0"/>
  </tractor>
</mlt>
"""


def output_ok(path, max_duration):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        meta = ffprobe_meta(path)
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    # An edit must never be longer than the source; a real cut is seconds
    # shorter, so container rounding never brings it near this bound.
    return 0.0 < meta["duration"] <= max_duration


def render_with_melt(project, output, meta):
    melt = shutil.which("melt") or shutil.which("melt-7")
    if not melt:
        log("melt (Shotcut's rendering engine) not found; using the ffmpeg fallback")
        return False
    cmd = [
        melt, "-silent", project, "-consumer", f"avformat:{output}",
        "vcodec=libx264", "preset=veryfast", "crf=23", "pix_fmt=yuv420p",
        "movflags=+faststart",
        "acodec=aac" if meta["has_audio"] else "an=1",
    ]
    proc = run(cmd, timeout=3600)
    if proc.returncode != 0:
        log(f"melt render failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
        return False
    if not output_ok(output, meta["duration"]):
        log("melt render produced an invalid file; using the ffmpeg fallback")
        return False
    return True


def select_expr(segments):
    return "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in segments)


def render_with_ffmpeg(source, segments, output, meta):
    expr = select_expr(segments)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-i", source,
        "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
    ]
    if meta["has_audio"]:
        cmd += ["-af", f"aselect='{expr}',asetpts=N/SR/TB"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", output,
    ]
    proc = run(cmd, timeout=3600)
    if proc.returncode != 0:
        log(f"ffmpeg render failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}")
        return False
    return output_ok(output, meta["duration"])


def write_summary(path, text):
    if path:
        with open(path, "w") as handle:
            handle.write(text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="source recording (mp4)")
    parser.add_argument("--output", default="", help="edited recording to write")
    parser.add_argument("--project", default="", help="Shotcut .mlt project to write")
    parser.add_argument("--summary", default="", help="one-line edit summary file")
    parser.add_argument(
        "--validate-edited", default="",
        help="validate an edited video against --input and exit (0 valid, 1 not) "
             "instead of editing",
    )
    parser.add_argument("--freeze-noise", default="-50dB", help="freezedetect noise tolerance")
    parser.add_argument("--silence-noise", default="-50dB", help="silencedetect noise floor")
    parser.add_argument(
        "--detect-min", type=float, default=2.0,
        help="minimum hold (s) before the ffmpeg detectors report an interval",
    )
    parser.add_argument(
        "--min-freeze", type=float, default=5.0,
        help="dead-air intervals shorter than this are kept (s)",
    )
    parser.add_argument(
        "--pad", type=float, default=1.0,
        help="context kept on each side of a cut (s)",
    )
    parser.add_argument(
        "--min-total-cut", type=float, default=5.0,
        help="skip the edit when less than this would be cut (s)",
    )
    parser.add_argument(
        "--min-cut-ratio", type=float, default=0.08,
        help="skip the edit when less than this share of the runtime would be cut",
    )
    args = parser.parse_args(argv)

    if args.validate_edited:
        valid = output_ok(args.validate_edited, ffprobe_meta(args.input)["duration"])
        log(f"validate {args.validate_edited}: {'ok' if valid else 'invalid'}")
        return 0 if valid else 1
    if not args.output or not args.project:
        parser.error("--output and --project are required unless --validate-edited is given")

    for stale in (args.output, args.project):
        if os.path.exists(stale):
            os.remove(stale)

    meta = ffprobe_meta(args.input)
    duration = meta["duration"]
    log(
        f"input {args.input}: {duration:.1f}s {meta['width']}x{meta['height']} "
        f"{meta['fps_num']}/{meta['fps_den']}fps audio={meta['has_audio']}"
    )

    dead = detect_freezes(args.input, meta, args.freeze_noise, args.detect_min)
    log(f"frozen-screen intervals: {len(dead)}")
    if meta["has_audio"]:
        # Silence only refines the frozen-screen detection, so an audio-pass
        # failure degrades to the freeze-only cut instead of losing the edit.
        try:
            silences = detect_silences(
                args.input, meta, args.silence_noise, args.detect_min
            )
        except RuntimeError as error:
            log(f"silencedetect failed ({error}); keeping freeze-only dead air")
        else:
            log(f"silent intervals: {len(silences)}")
            # Dead air means nothing to see AND nothing to hear.
            dead = intersect_intervals(dead, silences)

    # Keep at least one full frame of context on each side of a cut: the e2e
    # recorder captures at 0.5 fps, where a 1s pad is less than the spacing
    # between frames and a kept boundary could round down to no frames.
    pad = max(args.pad, meta["fps_den"] / meta["fps_num"])
    cuts = compute_cuts(dead, duration, pad, args.min_freeze)
    total_cut = sum(end - start for start, end in cuts)
    segments = keep_segments(cuts, duration)
    if not worth_editing(total_cut, duration, args.min_total_cut, args.min_cut_ratio) or not segments:
        message = (
            f"Dead-air edit skipped: only {total_cut:.1f}s of the "
            f"{duration:.1f}s recording is cuttable dead air."
        )
        log(message)
        write_summary(args.summary, message)
        return 0

    # Reference the source relative to the project so the .mlt opens in
    # Shotcut when both files are downloaded from the workflow artifacts.
    project_dir = os.path.dirname(os.path.abspath(args.project))
    try:
        resource = os.path.relpath(os.path.abspath(args.input), project_dir)
    except ValueError:
        resource = os.path.abspath(args.input)
    with open(args.project, "w") as handle:
        handle.write(mlt_project_xml(resource, segments, meta))
    log(f"Shotcut project written: {args.project} ({len(segments)} kept segments)")

    if not render_with_melt(args.project, args.output, meta):
        log("rendering the same cut list with ffmpeg instead")
        if not render_with_ffmpeg(args.input, segments, args.output, meta):
            if os.path.exists(args.output):
                os.remove(args.output)
            log("dead-air edit failed; keeping the unedited recording")
            return 1

    # Report the rendered file's actual runtime; melt/ffmpeg can shift it a
    # frame or two from the planned cut list.
    try:
        kept = ffprobe_meta(args.output)["duration"]
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError):
        kept = duration - total_cut
    message = (
        f"Dead air trimmed: cut {len(cuts)} segment(s) totaling {total_cut:.0f}s "
        f"from the {duration:.0f}s recording ({total_cut / duration:.0%}); "
        f"edited runtime {kept:.0f}s. The Shotcut project "
        f"({os.path.basename(args.project)}) is in the workflow artifacts."
    )
    log(message)
    write_summary(args.summary, message)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # pragma: no cover - CLI guard
        log(f"error: {error}")
        sys.exit(1)
