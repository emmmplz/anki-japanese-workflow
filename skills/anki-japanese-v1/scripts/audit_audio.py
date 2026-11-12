"""Audit generated WAV files for basic file and signal quality.

This is a conservative file-quality check.  It does not judge pronunciation,
and it never edits or removes audio.  The report contains a separate summary
for each manifest ``kind`` (normally ``word`` and ``sentence``) and a list of
files that should be listened to when a measurable anomaly is found.

Usage::

    python audit_audio.py --manifest path/to/audio-manifest.json
    python audit_audio.py --manifest path/to/audio-manifest.json --output audit.json

The manifest may be a generation ``audio-manifest.json`` or the small legacy
``manifest.json`` produced by the smoke fixtures.  Relative audio paths are
resolved against the manifest's directory.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import math
import wave
from typing import Any

import numpy as np


@dataclass(frozen=True)
class AuditOptions:
    min_duration: float = 0.10
    max_duration: float = 30.0
    silence_dbfs: float = -42.0
    frame_ms: float = 20.0
    max_leading_silence: float = 0.80
    max_trailing_silence: float = 0.80
    max_sentence_trailing_silence: float = 2.00
    max_internal_silence: float = 1.20
    min_rms_dbfs: float = -80.0
    min_active_rms_dbfs: float = -40.0
    clipping_threshold: float = 32760.0 / 32768.0


def _dbfs(value: float) -> float:
    if value <= 0 or not math.isfinite(value):
        return -200.0  # Finite floor keeps the report standards-compliant JSON.
    return 20.0 * math.log10(value)


def _load_wav(path: Path) -> tuple[np.ndarray, int, dict[str, Any]]:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.getnframes()
        if rate <= 0:
            raise ValueError("invalid WAV sample rate")
        if width != 2:
            raise ValueError(f"expected 16-bit PCM WAV, got sample width {width}")
        if channels != 1:
            raise ValueError(f"expected mono WAV, got {channels} channels")
        payload = source.readframes(frames)
    pcm = np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0
    if pcm.size != frames:
        raise ValueError(f"truncated WAV: header declares {frames} frames, read {pcm.size}")
    if not pcm.size or not np.isfinite(pcm).all():
        raise ValueError("empty or non-finite PCM data")
    peak = float(np.max(np.abs(pcm)))
    rms = float(np.sqrt(np.mean(np.square(pcm))))
    return pcm, rate, {
        "sample_rate": rate,
        "frames": int(pcm.size),
        "duration_seconds": float(pcm.size / rate),
        "rms": rms,
        "rms_dbfs": _dbfs(rms),
        "peak": peak,
        "peak_dbfs": _dbfs(peak),
        "clipping_fraction": float(np.mean(np.abs(pcm) >= 32760.0 / 32768.0)),
    }


def _silence_stats(pcm: np.ndarray, rate: int, options: AuditOptions) -> dict[str, float]:
    frame = max(1, int(round(rate * options.frame_ms / 1000.0)))
    count = len(pcm) // frame
    if count == 0:
        framed = pcm.reshape(1, -1)
    else:
        framed = pcm[: count * frame].reshape(count, frame)
    energy = np.sqrt(np.mean(np.square(framed), axis=1))
    active = energy >= 10 ** (options.silence_dbfs / 20.0)
    if not active.any():
        return {
            "leading_silence_seconds": float(len(pcm) / rate),
            "trailing_silence_seconds": float(len(pcm) / rate),
            "max_internal_silence_seconds": float(len(pcm) / rate),
            "active_frame_fraction": 0.0,
            "active_rms_dbfs": -200.0,
        }
    first = int(np.flatnonzero(active)[0])
    last = int(np.flatnonzero(active)[-1])
    # Frame boundaries are deliberately conservative: an anomaly is a prompt
    # to listen, not an assertion that the recording is incorrect.
    leading = first * frame / rate
    trailing = max(0, len(pcm) - (last + 1) * frame) / rate
    max_gap = 0
    gap = 0
    for value in active[first : last + 1]:
        if value:
            max_gap = max(max_gap, gap * frame / rate)
            gap = 0
        else:
            gap += 1
    max_gap = max(max_gap, gap * frame / rate)
    return {
        "leading_silence_seconds": float(leading),
        "trailing_silence_seconds": float(trailing),
        "max_internal_silence_seconds": float(max_gap),
        "active_frame_fraction": float(np.mean(active)),
        "active_rms_dbfs": _dbfs(float(np.sqrt(np.mean(np.square(framed[active]))))),
    }


def audit_one(path: Path, kind: str, job_id: str, options: AuditOptions) -> dict[str, Any]:
    result: dict[str, Any] = {"job_id": job_id, "kind": kind, "file": str(path), "issues": [],
                              "requires_listen_review": False, "pronunciation_judged": False}
    try:
        pcm, rate, signal = _load_wav(path)
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        result["issues"] = ["unreadable_or_invalid_wav"]
        result["error"] = str(exc)
        result["requires_listen_review"] = True
        return result
    silence = _silence_stats(pcm, rate, options)
    result.update(signal)
    result.update(silence)
    issues: list[str] = []
    if signal["duration_seconds"] < options.min_duration or signal["duration_seconds"] > options.max_duration:
        issues.append("duration_out_of_range")
    if signal["rms_dbfs"] < options.min_rms_dbfs:
        issues.append("near_silence")
    if silence["active_rms_dbfs"] < options.min_active_rms_dbfs:
        issues.append("low_active_volume")
    clipping = float(np.mean(np.abs(pcm) >= options.clipping_threshold))
    result["clipping_fraction"] = clipping
    if clipping > 0:
        issues.append("clipping")
    if silence["leading_silence_seconds"] > options.max_leading_silence:
        issues.append("long_leading_silence")
    trailing_limit = options.max_sentence_trailing_silence if kind == "sentence" else options.max_trailing_silence
    if silence["trailing_silence_seconds"] > trailing_limit:
        issues.append("long_trailing_silence")
    if silence["max_internal_silence_seconds"] > options.max_internal_silence:
        issues.append("long_internal_silence")
    result["issues"] = issues
    # This label is intentionally about measurable audio quality only.  It is
    # never a pronunciation verdict; listen before deciding whether to keep it.
    result["requires_listen_review"] = bool(issues)
    return result


def audit_manifest(manifest_path: Path, options: AuditOptions) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    audio = manifest.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("manifest must contain an audio object")
    root = manifest_path.resolve().parent
    results: list[dict[str, Any]] = []
    missing = []
    for job_id, job in manifest.get("jobs", {}).items():
        if job_id not in audio:
            missing.append({"job_id": job_id, "kind": job.get("kind", "unknown"), "text": job.get("text", "")})
    for job_id, entry in audio.items():
        if not isinstance(entry, dict):
            results.append({"job_id": job_id, "kind": "unknown", "file": "", "issues": ["malformed_manifest_entry"], "requires_listen_review": True})
            continue
        relative = entry.get("file")
        if not isinstance(relative, str) or not relative.strip():
            results.append({"job_id": job_id, "kind": str(entry.get("kind", "unknown")), "file": "", "issues": ["missing_manifest_file_path"], "requires_listen_review": True})
            continue
        path = (root / relative).resolve()
        item = audit_one(path, str(entry.get("kind", "unknown")), job_id, options)
        item["text"] = entry.get("text", "")
        if "sample_rate" in item and entry.get("sample_rate") and item["sample_rate"] != entry["sample_rate"]:
            item["issues"].append("manifest_sample_rate_mismatch")
            item["requires_listen_review"] = True
        results.append(item)
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        by_kind[item["kind"]].append(item)
    summaries: dict[str, Any] = {}
    for kind in sorted(set(by_kind) | {x["kind"] for x in missing}):
        items = by_kind[kind]
        durations = [x["duration_seconds"] for x in items if "duration_seconds" in x]
        summaries[kind] = {
            "files": len(items),
            "missing_jobs": sum(x["kind"] == kind for x in missing),
            "valid_files": sum("duration_seconds" in x for x in items),
            "files_with_issues": sum(bool(x.get("issues")) for x in items),
            "listen_review_files": sum(bool(x.get("requires_listen_review")) for x in items),
            "duration_seconds": {
                "min": min(durations) if durations else None,
                "max": max(durations) if durations else None,
                "mean": (sum(durations) / len(durations)) if durations else None,
            },
            "issue_counts": {
                issue: sum(issue in x.get("issues", []) for x in items)
                for issue in sorted({issue for x in items for issue in x.get("issues", [])})
            },
        }
    return {
        "schema_version": "1.0",
        "manifest": str(manifest_path.resolve()),
        "scope": "file_quality_only; pronunciation_is_not_judged",
        "options": options.__dict__,
        "summary_by_kind": summaries,
        "listen_review": [x for x in results if x.get("requires_listen_review")],
        "missing_jobs": missing,
        "files": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-duration", type=float, default=30.0)
    parser.add_argument("--max-leading-silence", type=float, default=0.80)
    parser.add_argument("--max-trailing-silence", type=float, default=0.80, help="word/other kinds; sentence uses its own limit")
    parser.add_argument("--max-sentence-trailing-silence", type=float, default=2.00)
    parser.add_argument("--max-internal-silence", type=float, default=1.20)
    args = parser.parse_args()
    for name in ("max_duration", "max_leading_silence", "max_trailing_silence", "max_sentence_trailing_silence", "max_internal_silence"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be a finite positive number")
    options = AuditOptions(
        max_duration=args.max_duration,
        max_leading_silence=args.max_leading_silence,
        max_trailing_silence=args.max_trailing_silence,
        max_sentence_trailing_silence=args.max_sentence_trailing_silence,
        max_internal_silence=args.max_internal_silence,
    )
    report = audit_manifest(args.manifest.resolve(), options)
    output = args.output.resolve() if args.output else args.manifest.with_name("audio-audit.json").resolve()
    if output == args.manifest.resolve():
        parser.error("output must not overwrite the input manifest")
    if any(item.get("file") and Path(item["file"]).resolve() == output for item in report["files"]):
        parser.error("output must not overwrite any input audio")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    compact = {"output": str(output), "summary_by_kind": report["summary_by_kind"], "listen_review": len(report["listen_review"])}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
