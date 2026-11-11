"""PCM/WAV utilities, seam crossfades, edit application, and tone synthesis.

All audio is mono 16-bit PCM held in array('h'). No resampling and no time
stretching anywhere in this module: edits are splices plus user-sized
crossfades, and gap fill comes only from a supplied room-tone recording.
"""

import hashlib
import math
import sys
import wave
from array import array

CLIP_MIN, CLIP_MAX = -32768, 32767


class EditError(Exception):
    """Raised when an edit cannot be applied without violating honesty rules
    (no stretching, no synthesized room tone, fades must fit real audio)."""


def _clip(v):
    return CLIP_MIN if v < CLIP_MIN else CLIP_MAX if v > CLIP_MAX else int(v)


# ---------------------------------------------------------------- WAV I/O

def _read_wav_obj(w):
    if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getcomptype() != "NONE":
        raise ValueError("仅支持单声道 16-bit PCM WAV")
    rate = w.getframerate()
    raw = w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, rate


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return _read_wav_obj(w)


def read_wav_bytes(data):
    import io
    with wave.open(io.BytesIO(data), "rb") as w:
        return _read_wav_obj(w)


def write_wav(path, samples, rate):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def pcm_sha256(samples):
    return hashlib.sha256(samples.tobytes()).hexdigest()


def pcm_from_bytes(raw):
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


# ---------------------------------------------------------------- fades

def xfade(a, b):
    """Equal-power crossfade between equal-length arrays."""
    n = len(a)
    if n == 0:
        return array("h")
    if len(b) != n:
        raise ValueError("xfade 长度不一致")
    out = array("h", bytes(2 * n))
    for i in range(n):
        t = (i + 0.5) / n
        g1 = math.cos(t * math.pi / 2.0)
        g2 = math.sin(t * math.pi / 2.0)
        out[i] = _clip(round(a[i] * g1 + b[i] * g2))
    return out


def _ramp_inplace(buf, start, n, g_from, g_to):
    for i in range(n):
        t = (i + 0.5) / n
        g = g_from + (g_to - g_from) * t
        buf[start + i] = _clip(round(buf[start + i] * g))


# ---------------------------------------------------------------- edit

def apply_edit(parent, sel_start, sel_end, take, mode, fade_in, fade_out, roomtone):
    """Apply one replacement edit to `parent` PCM.

    Returns (output_samples, seam_report). Raises EditError when the request
    cannot be honored without stretching audio or inventing room tone.

    mode "insert_shift":   following content moves; output length changes by
                           (len(take) - selection) - fade_in - fade_out.
    mode "keep_duration":  output length equals input length exactly. A short
                           take is padded with real room tone; a long take is
                           rejected (trim it or choose insert_shift).
    """
    n = len(parent)
    t = len(take)
    if not (0 <= sel_start < sel_end <= n):
        raise EditError("选段范围超出章节时间线")
    if t == 0:
        raise EditError("候选有效长度为零")
    if fade_in < 0 or fade_out < 0:
        raise EditError("淡化长度不能为负")
    seg_len = sel_end - sel_start

    if mode == "insert_shift":
        if fade_in > sel_start:
            raise EditError("淡入长度超过选段之前的可用语境")
        if fade_out > n - sel_end:
            raise EditError("淡出长度超过选段之后的可用语境")
        if fade_in + fade_out > t:
            raise EditError("淡化总长度超过候选有效长度")
        a = parent[:sel_start]
        c = parent[sel_end:]
        parts = [a[: len(a) - fade_in]]
        if fade_in:
            parts.append(xfade(a[len(a) - fade_in:], take[:fade_in]))
        parts.append(take[fade_in: t - fade_out] if fade_out else take[fade_in:])
        if fade_out:
            parts.append(xfade(take[t - fade_out:], c[:fade_out]))
        parts.append(c[fade_out:])
        out = array("h")
        for p in parts:
            out.extend(p)
        body_start = sel_start
        body_end = sel_start + t - fade_in - fade_out
        report = {
            "mode": mode,
            "frames_in": n,
            "frames_out": len(out),
            "delta": len(out) - n,
            "selection_in": [sel_start, sel_end],
            "take_range_out": [body_start, body_end],
            "fade_regions_out": [r for r in (
                [sel_start - fade_in, sel_start] if fade_in else None,
                [body_end, body_end + fade_out] if fade_out else None,
            ) if r],
            "fill_range_out": None,
        }
        return out, report

    if mode == "keep_duration":
        if t > seg_len:
            raise EditError(
                "候选比原选段长 %d 帧；禁止拉伸遮差，请显式修剪或改用 insert_shift"
                % (t - seg_len))
        fill = seg_len - t
        if fade_in + fade_out > seg_len:
            raise EditError("淡化总长度超过选段长度")
        body = array("h", take)
        fill_range = None
        if fill > 0:
            if roomtone is None:
                raise EditError("候选短于原选段，需要环境底噪填充，但工程未提供底噪录音")
            if len(roomtone) < fill:
                raise EditError(
                    "环境底噪不足：填充需要 %d 帧，仅有 %d 帧；请提供更长的真实底噪"
                    % (fill, len(roomtone)))
            if fade_out > t:
                raise EditError("淡出长度超过候选有效长度")
            if fade_out > fill:
                raise EditError("底噪填充段不足以承载淡出淡化")
            rt = array("h", roomtone[:fill])
            if fade_out:
                _ramp_inplace(body, t - fade_out, fade_out, 1.0, 0.0)
                _ramp_inplace(rt, 0, fade_out, 0.0, 1.0)
            body.extend(rt)
            fill_range = [sel_start + t, sel_end]
        r = body
        if fade_in:
            r[:fade_in] = xfade(parent[sel_start: sel_start + fade_in], r[:fade_in])
        if fade_out:
            r[seg_len - fade_out:] = xfade(
                r[seg_len - fade_out:], parent[sel_end - fade_out: sel_end])
        out = parent[:sel_start] + r + parent[sel_end:]
        fade_regions = []
        if fade_in:
            fade_regions.append([sel_start, sel_start + fade_in])
        if fill > 0 and fade_out:
            fade_regions.append([sel_start + t - fade_out, sel_start + t + fade_out])
        if fade_out:
            fade_regions.append([sel_end - fade_out, sel_end])
        report = {
            "mode": mode,
            "frames_in": n,
            "frames_out": len(out),
            "delta": 0,
            "selection_in": [sel_start, sel_end],
            "take_range_out": [sel_start, sel_start + t],
            "fade_regions_out": fade_regions,
            "fill_range_out": fill_range,
        }
        return out, report

    raise EditError("未知插入方式: %r" % mode)


# ---------------------------------------------------------------- synthesis
# Used only to build demo chapters / test fixtures. The server never
# synthesizes audio to fill gaps in a render.

def synth_tone(freq, seconds, rate, amp=10000, edge=0.005):
    n = int(round(seconds * rate))
    out = array("h", bytes(2 * n))
    e = min(n // 2, int(round(edge * rate)))
    for i in range(n):
        g = 1.0
        if e:
            if i < e:
                g = (i + 0.5) / e
            elif i >= n - e:
                g = (n - i - 0.5) / e
        out[i] = _clip(round(amp * g * math.sin(2 * math.pi * freq * i / rate)))
    return out


def synth_silence(seconds, rate):
    return array("h", bytes(2 * int(round(seconds * rate))))


PASSWORD_TONES = (1000.0, 1250.0, 1500.0, 1750.0)


def synth_marker(rate, tones=PASSWORD_TONES, tone_s=0.08, gap_s=0.02):
    out = array("h")
    for f in tones:
        out.extend(synth_tone(f, tone_s, rate, amp=9000, edge=0.004))
        out.extend(synth_silence(gap_s, rate))
    return out


def synth_chapter(rate=8000, n_sentences=8, sentence_s=0.40, gap_s=0.15):
    """Chapter of distinct-tone 'sentences' bracketed by password markers.

    Returns (samples, manifest) with exact frame ranges for verification.
    """
    samples = array("h")
    manifest = {"rate": rate, "sentences": [], "markers": []}

    def push(buf, label):
        start = len(samples)
        samples.extend(buf)
        manifest["markers" if label.startswith("marker") else "sentences"].append(
            {"label": label, "start": start, "end": len(samples)})

    push(synth_silence(0.10, rate), "marker_lead_in")
    push(synth_marker(rate), "marker_password")
    push(synth_silence(0.10, rate), "marker_gap")
    for i in range(n_sentences):
        freq = 300.0 + 37.0 * i
        start = len(samples)
        samples.extend(synth_tone(freq, sentence_s, rate))
        manifest["sentences"].append(
            {"index": i, "freq": freq, "start": start, "end": len(samples)})
        samples.extend(synth_silence(gap_s, rate))
    push(synth_marker(rate, tones=tuple(reversed(PASSWORD_TONES))), "marker_password_end")
    push(synth_silence(0.10, rate), "marker_tail")
    manifest["frames"] = len(samples)
    return samples, manifest


def synth_roomtone(rate, seconds, amp=160, seed=20261111):
    """Deterministic low-level noise, standing in for a *recorded* room-tone
    asset supplied by the user. The renderer itself never creates this."""
    n = int(round(seconds * rate))
    out = array("h", bytes(2 * n))
    state = seed & 0x7FFFFFFF
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        out[i] = (state % (2 * amp + 1)) - amp
    return out


# ---------------------------------------------------------------- analysis

def tone_power(samples, rate, freq):
    n = len(samples)
    if n == 0:
        return 0.0
    w = 2.0 * math.pi * freq / rate
    cw = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + cw * s1 - s2
        s2 = s1
        s1 = s0
    return (s1 * s1 + s2 * s2 - cw * s1 * s2) / (n * n)


def find_tone_ranges(samples, rate, freq, win_s=0.05, threshold=1.0e6):
    """Locate [start, end) frame ranges where `freq` dominates."""
    win = max(8, int(round(win_s * rate)))
    hop = win // 2
    active = []
    i = 0
    while i + win <= len(samples):
        if tone_power(samples[i: i + win], rate, freq) > threshold:
            active.append((i, i + win))
        i += hop
    if not active:
        return []
    ranges = []
    cur_s, cur_e = active[0]
    for s, e in active[1:]:
        if s <= cur_e + hop:
            cur_e = max(cur_e, e)
        else:
            ranges.append([cur_s, cur_e])
            cur_s, cur_e = s, e
    ranges.append([cur_s, cur_e])
    return ranges


def max_abs(samples):
    m = 0
    for v in samples:
        av = -v if v < 0 else v
        if av > m:
            m = av
    return m


def max_step(samples):
    """Largest sample-to-sample jump; clicks at bad seams show up here."""
    m = 0
    prev = 0
    first = True
    for v in samples:
        if not first:
            d = v - prev
            if d < 0:
                d = -d
            if d > m:
                m = d
        prev = v
        first = False
    return m
