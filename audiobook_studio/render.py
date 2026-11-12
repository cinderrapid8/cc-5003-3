"""Render engine: splice adopted candidates into the chapter recording.

Hard rules enforced here:

* Audio is never time-stretched. A candidate longer than the marked range can
  only be rendered in SHIFT mode (later material moves). KEEP_DURATION with an
  over-long candidate is a hard error.
* Seams use user-controlled fades in frames. The original tail fades out and
  the candidate head fades in over adjacent, non-overlapping samples, so fades
  never alter the frame-count bookkeeping.
* KEEP_DURATION gaps are filled with a *real captured room-tone excerpt*.
  Missing or too-short room tone aborts the render with an actionable error;
  the engine never synthesizes fake 'original' audio.
* Every seam gets a named cue ('password mark') in the output WAV so adoption
  results, length changes and seam positions can be verified independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import audio as A
from .storage import Store

MIN_NOISE_MS = 200.0


class RenderError(Exception):
    """Render cannot proceed; the error code is machine-actionable."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class AdoptedMark:
    mark_id: str
    label: str
    range_start: int
    range_end: int
    candidate_clip: A.Clip
    mode: str  # 'shift' | 'keep_duration'
    fade_ms: float


@dataclass
class RenderResult:
    clip: A.Clip
    cues: list[tuple[int, str]]
    events: list[dict] = field(default_factory=list)
    original_frames: int = 0


def _fade_frames(fade_ms: float, sample_rate: int) -> int:
    return max(0, int(round(fade_ms * sample_rate / 1000.0)))


def _build_noise(store: Store, noise_id: str | None, need_frames: int,
                 sample_rate: int, target_rms: float) -> A.Clip:
    if not noise_id:
        raise RenderError(
            "NOISE_MISSING",
            "KEEP_DURATION needs a real room-tone excerpt; none was provided.",
            {"needed_frames": need_frames},
        )
    row = store.query_one("SELECT * FROM noise_excerpts WHERE id=?", (noise_id,))
    if not row:
        raise RenderError("NOISE_MISSING", "referenced room-tone excerpt no longer exists.")
    noise_clip = A.read_wav(store.read_blob(row["blob_id"]))
    if noise_clip.sample_rate != sample_rate:
        noise_clip = A.resample_linear(noise_clip, sample_rate)
    profile = A.noise_profile(noise_clip)
    if profile["duration_ms"] < MIN_NOISE_MS:
        raise RenderError(
            "NOISE_TOO_SHORT",
            f"room-tone excerpt is only {profile['duration_ms']:.0f} ms; "
            f"capture at least {MIN_NOISE_MS:.0f} ms of real room tone.",
            profile,
        )
    if profile["rms"] <= 0.5:
        raise RenderError(
            "NOISE_SILENT",
            "room-tone excerpt contains no measurable noise floor (digital silence).",
            profile,
        )
    tiled = A.tile_noise(noise_clip, need_frames)
    return A.match_gain(tiled, target_rms)


def validate_adoptions(store: Store, project_id: str, adoptions: list[AdoptedMark]) -> None:
    for a in adoptions:
        if a.mode not in ("shift", "keep_duration"):
            raise RenderError("BAD_MODE", f"unknown splice mode {a.mode!r}")
        if a.mode == "keep_duration" and a.candidate_clip.frames > (a.range_end - a.range_start):
            raise RenderError(
                "KEEP_DURATION_OVERLONG",
                f"candidate for {a.label!r} is longer than the marked range; "
                "choose SHIFT to move later audio, or trim the candidate. "
                "Stretching is not allowed.",
                {"mark_id": a.mark_id,
                 "candidate_frames": a.candidate_clip.frames,
                 "range_frames": a.range_end - a.range_start},
            )
        if a.fade_ms < 0:
            raise RenderError("BAD_FADE", "fade must be non-negative")


def render_chapter(store: Store, project_id: str, chapter: A.Clip,
                   adoptions: list[AdoptedMark],
                   noise_id: str | None) -> RenderResult:
    validate_adoptions(store, project_id, adoptions)
    rate = chapter.sample_rate
    ordered = sorted(adoptions, key=lambda a: a.range_start)

    prev_end = -1
    for a in ordered:
        if a.range_start < prev_end:
            raise RenderError("OVERLAPPING_MARKS",
                              "marked ranges overlap; resolve before rendering.")
        prev_end = a.range_end

    parts: list[A.Clip] = []
    cues: list[tuple[int, str]] = []
    events: list[dict] = []
    cursor = 0  # next unconsumed frame in the ORIGINAL chapter
    out_pos = 0  # frame position in the OUTPUT stream

    for a in ordered:
        fade = _fade_frames(a.fade_ms, rate)
        range_frames = a.range_end - a.range_start
        candidate = a.candidate_clip
        delta = candidate.frames - range_frames

        # 1) untouched original material before the mark.
        pre = A.slice_clip(chapter, cursor, a.range_start)
        # 2) head seam: fade the original tail out, fade the candidate head in.
        head_fade = min(fade, pre.frames, candidate.frames // 2)
        if head_fade:
            pre = A.fade_tail(pre, head_fade)
        replacement = A.apply_edge_fades(candidate, head_fade)

        seam_start = out_pos + pre.frames
        parts.append(pre)
        out_pos += pre.frames

        if a.mode == "shift":
            parts.append(replacement)
            out_pos += replacement.frames
            events.append({
                "mark_id": a.mark_id, "label": a.label, "mode": "shift",
                "seam_start_frame": seam_start,
                "seam_end_frame": out_pos,
                "delta_frames": delta,
            })
        else:  # keep_duration: fill any shortfall with real room tone
            if candidate.frames > range_frames:
                raise RenderError(
                    "KEEP_DURATION_OVERLONG",
                    "over-long candidate cannot be forced into keep_duration",
                    {"mark_id": a.mark_id})
            gap = range_frames - candidate.frames
            seg_parts = [replacement]
            if gap > 0:
                floor = _build_noise(
                    store, noise_id, gap, rate,
                    target_rms=A.rms(
                        A.slice_clip(chapter, a.range_start, a.range_end).samples) * 0.9)
                seg_parts.append(floor)
            segment = A.concat(seg_parts, rate)
            parts.append(segment)
            out_pos += segment.frames
            events.append({
                "mark_id": a.mark_id, "label": a.label, "mode": "keep_duration",
                "seam_start_frame": seam_start,
                "seam_end_frame": out_pos,
                "delta_frames": 0,
                "gap_frames": gap,
            })

        cues.append((seam_start, f"SEAM-IN {a.label}"))
        cues.append((out_pos, f"SEAM-OUT {a.label}"))
        cursor = a.range_end

    # Trailing original material after the final mark.
    tail = A.slice_clip(chapter, cursor, chapter.frames)
    parts.append(tail)
    out_pos += tail.frames

    rendered = A.concat(parts, rate)
    assert rendered.frames == out_pos
    cues.append((0, "CHAPTER-START"))
    cues.sort(key=lambda c: c[0])
    return RenderResult(clip=rendered, cues=cues, events=events,
                        original_frames=chapter.frames)
