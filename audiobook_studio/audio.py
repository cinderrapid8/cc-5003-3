"""PCM16 mono audio primitives for the re-recording workbench.

All sample positions are integer *frames* (1 frame == 1 mono sample).
Duration is always derived from actual captured frames, never from the
device-reported clock. No time-stretching is implemented anywhere: a
candidate whose length does not fit the target range must be rendered
explicitly in SHIFT or KEEP_DURATION mode.
"""
from __future__ import annotations

import array
import io
import math
import struct
import sys
import wave
from dataclasses import dataclass

CUE_TAG = b"cue "
LIST_TAG = b"LIST"
LABL_TAG = b"labl"
DATA_TAG = b"data"


@dataclass(frozen=True)
class Clip:
    samples: array.array  # signed 16-bit interleaved mono
    sample_rate: int

    @property
    def frames(self) -> int:
        return len(self.samples)

    @property
    def duration_ms(self) -> float:
        return len(self.samples) * 1000.0 / self.sample_rate


def _array_of(raw: bytes) -> array.array:
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder != "little":
        a.byteswap()
    return a


def read_wav(data: bytes) -> Clip:
    """Read a PCM WAV; only uncompressed PCM16 mono is accepted."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        comp = wf.getcomptype()
        rate = wf.getframerate()
        if comp != "NONE":
            raise ValueError("only uncompressed PCM WAV is supported")
        if width != 2:
            raise ValueError(f"only 16-bit PCM is supported, got {width * 8}-bit")
        if channels != 1:
            raise ValueError(f"only mono is supported, got {channels} channels")
        frames = wf.readframes(wf.getnframes())
    return Clip(_array_of(frames), rate)


def write_wav(clip: Clip) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(clip.sample_rate)
        wf.writeframes(clip.samples.tobytes())
    return buf.getvalue()


def write_wav_with_cues(clip: Clip, cues: list[tuple[int, str]]) -> bytes:
    """Write a WAV with a standard `cue ` chunk and associated `labl` notes.

    Cue positions are sample frames; labels are the spoken password marks so
    editors can verify adoption results and seam locations externally.
    """
    body = write_wav(clip)
    if not cues:
        return body

    # WAV layout: RIFF(4) size(4) WAVE(4) then chunks. Insert cue chunks before
    # the data chunk for maximum player compatibility.
    pos = 12
    chunks: list[bytes] = []
    data_chunk = b""
    while pos + 8 <= len(body):
        cid = body[pos:pos + 4]
        csize = struct.unpack("<I", body[pos + 4:pos + 8])[0]
        raw = body[pos:pos + 8 + csize + (csize & 1)]
        if cid == DATA_TAG:
            data_chunk = raw
        else:
            chunks.append(raw)
        pos += 8 + csize + (csize & 1)

    cue_payload = struct.pack("<I", len(cues))
    labl_entries = []
    for idx, (frame, label) in enumerate(cues, start=1):
        cue_payload += struct.pack("<IIIIII", idx, frame, 0, 0, 0, frame)
        text = label.encode("utf-8") + b"\x00"
        if len(text) & 1:
            text += b"\x00"
        labl_entries.append(struct.pack("<I", idx) + LABL_TAG + struct.pack("<I", len(text)) + text)

    list_payload = b"adtl" + b"".join(labl_entries)
    cue_chunk = _chunk(CUE_TAG, cue_payload)
    list_chunk = _chunk(LIST_TAG, list_payload)

    out = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE"
    out += b"".join(chunks)
    out += cue_chunk + list_chunk + data_chunk
    total = len(out) - 8
    out = out[:4] + struct.pack("<I", total) + out[8:]
    return out


def _chunk(tag: bytes, payload: bytes) -> bytes:
    raw = tag + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        raw += b"\x00"
    return raw


def slice_clip(clip: Clip, start: int, end: int) -> Clip:
    start = max(0, start)
    end = min(clip.frames, end)
    if end < start:
        raise ValueError("slice end before start")
    return Clip(clip.samples[start:end], clip.sample_rate)


def concat(clips: list[Clip], sample_rate: int) -> Clip:
    out = array.array("h")
    for c in clips:
        if c.sample_rate != sample_rate:
            raise ValueError("sample rate mismatch in concat")
        out.extend(c.samples)
    return Clip(out, sample_rate)


def apply_edge_fades(clip: Clip, fade_frames: int) -> Clip:
    """Fade in at the head and fade out at the tail of a clip."""
    f = max(0, min(fade_frames, clip.frames // 2))
    if f == 0:
        return clip
    samples = array.array("h", clip.samples)
    for i in range(f):
        g = i / f
        samples[i] = int(round(samples[i] * g))
        samples[clip.frames - 1 - i] = int(round(samples[clip.frames - 1 - i] * g))
    return Clip(samples, clip.sample_rate)


def fade_tail(clip: Clip, fade_frames: int) -> Clip:
    """Fade the last `fade_frames` samples down to silence; head untouched."""
    f = max(0, min(fade_frames, clip.frames))
    if f == 0:
        return clip
    samples = array.array("h", clip.samples)
    n = clip.frames
    for i in range(f):
        g = i / f
        samples[n - 1 - i] = int(round(samples[n - 1 - i] * g))
    return Clip(samples, clip.sample_rate)


def resample_linear(clip: Clip, target_rate: int) -> Clip:
    """Linear rate conversion used only to align take rate to chapter rate."""
    if clip.sample_rate == target_rate:
        return clip
    ratio = clip.sample_rate / target_rate
    n = int(round(clip.frames / ratio))
    out = array.array("h", [0] * n)
    src = clip.samples
    for i in range(n):
        x = i * ratio
        i0 = int(math.floor(x))
        frac = x - i0
        i1 = min(i0 + 1, clip.frames - 1)
        out[i] = int(round(src[i0] * (1 - frac) + src[i1] * frac))
    return Clip(out, target_rate)


def rms(samples: array.array) -> float:
    if not samples:
        return 0.0
    acc = 0
    for v in samples:
        acc += v * v
    return math.sqrt(acc / len(samples))


def noise_profile(clip: Clip) -> dict:
    """Characterize a room-tone excerpt by RMS and peak (16-bit units)."""
    s = clip.samples
    peak = max((abs(v) for v in s), default=0)
    return {
        "frames": clip.frames,
        "duration_ms": clip.duration_ms,
        "rms": rms(s),
        "peak": peak,
    }


def tile_noise(noise: Clip, frames_needed: int) -> Clip:
    """Repeat a real room-tone excerpt to cover a gap.

    The source bytes are always genuine captured room noise; we never
    synthesize a fake 'original' tone. Short sources are flagged separately
    by the caller via noise_profile.
    """
    if noise.frames == 0:
        raise ValueError("empty noise source")
    if frames_needed <= noise.frames:
        return slice_clip(noise, 0, frames_needed)
    out = array.array("h")
    while len(out) < frames_needed:
        out.extend(noise.samples)
    return Clip(out[:frames_needed], noise.sample_rate)


def match_gain(source: Clip, target_rms: float) -> Clip:
    """Scale a noise clip toward a target RMS, clamped to PCM range."""
    cur = rms(source.samples)
    if cur <= 0 or target_rms <= 0:
        return source
    g = min(target_rms / cur, 4.0)
    out = array.array("h", (int(max(-32768, min(32767, round(v * g)))) for v in source.samples))
    return Clip(out, source.sample_rate)
