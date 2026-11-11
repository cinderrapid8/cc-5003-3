"""Render engine: replay a version chain into PCM, cache timelines, produce
previews, peaks, and background export jobs.

Formal renders re-verify every referenced take against its stored SHA-256,
so a take that was complete at adoption time but later truncated on disk
cannot slip into the exported audio.
"""

import io
import json
import os
import time
import wave

from . import audio
from .store import StoreError


class RenderError(Exception):
    pass


def _roomtone_pcm(store, chapter):
    proj = store.get_project(chapter["project_id"])
    rt_id = proj["roomtone_recording_id"]
    if not rt_id:
        return None
    rec = store.get_recording(rt_id)
    if rec["status"] != "complete":
        raise RenderError("环境底噪录音不完整 (status=%s)" % rec["status"])
    return store.read_recording_samples(rt_id)[0]


def render_version_pcm(store, chapter_id, version_id, use_cache=True):
    """Return (pcm, edits_report) for the timeline at `version_id`."""
    cache = _cache_path(store, chapter_id, version_id)
    meta = cache + ".json"
    if use_cache and os.path.exists(cache) and os.path.exists(meta):
        pcm, _ = audio.read_wav(cache)
        with open(meta, "r", encoding="utf-8") as f:
            reports = json.load(f)
        return pcm, reports
    return _render_uncached(
        store, chapter_id, version_id, write_cache=cache if use_cache else None)


def _cache_path(store, chapter_id, version_id):
    key = "base" if version_id is None else str(version_id)
    return os.path.join(store.data_dir, "cache", "ch%d_v%s.wav" % (chapter_id, key))


def _render_uncached(store, chapter_id, version_id, write_cache=None):
    chapter = store.get_chapter(chapter_id)
    src = store.get_recording(chapter["source_recording_id"])
    if src["status"] != "complete" or not store.verify_recording(src["id"]):
        raise RenderError("章节源录音校验失败")
    pcm, _rate = store.read_recording_samples(src["id"])
    chain = store.version_chain(chapter_id, version_id)
    reports = []
    for v in chain:
        take = store.get_recording(v["take_id"])
        if take["status"] != "complete":
            raise RenderError(
                "录次 %d 状态为 %s，只有完成的录次可以进入正式渲染"
                % (take["id"], take["status"]))
        if not store.verify_recording(take["id"]):
            raise RenderError(
                "录次 %d 字节校验失败（疑似被截断），拒绝渲染" % take["id"])
        take_pcm, _r = store.read_recording_samples(take["id"])
        eff_end = len(take_pcm) - v["trim_end_frames"] \
            if v["trim_end_frames"] else len(take_pcm)
        eff = take_pcm[v["trim_start_frames"]:eff_end]
        sel = store.get_selection(v["selection_id"])
        rt = _roomtone_pcm(store, chapter) if v["mode"] == "keep_duration" else None
        pcm, rep = audio.apply_edit(
            pcm, sel["start_frame"], sel["end_frame"], eff, v["mode"],
            v["fade_in_frames"], v["fade_out_frames"], rt)
        rep = dict(rep)
        rep.update({
            "version_id": v["id"],
            "take_id": take["id"],
            "take_sha256": take["pcm_sha256"],
            "selection_id": sel["id"],
        })
        reports.append(rep)

    if write_cache:
        os.makedirs(os.path.dirname(write_cache), exist_ok=True)
        tmp = write_cache + ".tmp.wav"
        audio.write_wav(tmp, pcm, chapter_rate(store, chapter_id))
        os.replace(tmp, write_cache)
        with open(write_cache + ".json", "w", encoding="utf-8") as f:
            json.dump(reports, f)
    return pcm, reports


def chapter_rate(store, chapter_id):
    return store.chapter_sample_rate(chapter_id)


def wav_bytes(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def render_job(store, job_id, delay_ms=0):
    """Execute an export job. `delay_ms` simulates a slow export so a
    concurrent re-adoption can be observed; the job keeps its snapshot."""
    job = store.get_render_job(job_id)
    try:
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        chapter_id = job["chapter_id"]
        version_id = job["version_id"]
        pcm, reports = render_version_pcm(store, chapter_id, version_id, use_cache=False)
        rate = chapter_rate(store, chapter_id)
        rel = os.path.join("renders", "job_%d.wav" % job_id)
        out = os.path.join(store.data_dir, rel)
        tmp = out + ".tmp.wav"
        audio.write_wav(tmp, pcm, rate)
        os.replace(tmp, out)

        chapter = store.get_chapter(chapter_id)
        stale = chapter["current_version_id"] != version_id
        report = {
            "job_id": job_id,
            "chapter_id": chapter_id,
            "version_id": version_id,
            "chain_version_ids": [v["id"] for v in
                                  store.version_chain(chapter_id, version_id)],
            "edits": reports,
            "frames_out": len(pcm),
            "rate": rate,
            "stale_at_finish": stale,
            "current_version_at_finish": chapter["current_version_id"],
        }
        return store.finish_render_job(
            job_id, "done", output_path=rel, report=report, stale=stale)
    except (RenderError, StoreError, audio.EditError, ValueError) as e:
        return store.finish_render_job(job_id, "failed", error=str(e))


def peaks_for_version(store, chapter_id, version_id, buckets=800):
    pcm, _ = render_version_pcm(store, chapter_id, version_id)
    n = len(pcm)
    buckets = max(1, min(int(buckets), max(1, n)))
    return {
        "frames": n,
        "rate": chapter_rate(store, chapter_id),
        "buckets": buckets,
        "peaks": _peaks(pcm, buckets),
    }


def _peaks(pcm, buckets):
    n = len(pcm)
    step = n / float(buckets)
    out = []
    for b in range(buckets):
        a = int(b * step)
        z = int((b + 1) * step) if b + 1 < buckets else n
        if z <= a:
            out.append([0, 0])
            continue
        lo = min(pcm[a:z])
        hi = max(pcm[a:z])
        out.append([lo, hi])
    return out


def build_preview(store, chapter_id, selection_id, take_id, mode,
                  fade_in_frames, fade_out_frames, trim_start_frames,
                  trim_end_frames, context_frames):
    """Render a short A/B window around the selection: original context
    (take_id=None) or the candidate applied."""
    sel = store.get_selection(selection_id)
    basis_id = sel["version_id"]
    base_pcm, _ = render_version_pcm(store, chapter_id, basis_id)
    report = None
    if take_id is None:
        out = base_pcm
        focus_end = sel["end_frame"]
    else:
        take = store.get_recording(take_id)
        if take["status"] != "complete":
            raise StoreError("只有完成的录次可以试听")
        if not store.verify_recording(take_id):
            raise StoreError("候选字节校验失败")
        if take["sample_rate"] != store.chapter_sample_rate(chapter_id):
            raise StoreError("采样率不一致，不能试听")
        take_pcm, _ = store.read_recording_samples(take_id)
        eff_end = len(take_pcm) - trim_end_frames if trim_end_frames else len(take_pcm)
        eff = take_pcm[trim_start_frames:eff_end]
        chapter = store.get_chapter(chapter_id)
        rt = _roomtone_pcm(store, chapter) if mode == "keep_duration" else None
        out, report = audio.apply_edit(
            base_pcm, sel["start_frame"], sel["end_frame"], eff, mode,
            fade_in_frames, fade_out_frames, rt)
        focus_end = report["take_range_out"][1]

    start = max(0, sel["start_frame"] - context_frames)
    end = min(len(out), focus_end + context_frames)
    clip = out[start:end]
    return wav_bytes(clip, store.chapter_sample_rate(chapter_id)), {
        "selection_id": selection_id,
        "take_id": take_id,
        "window": [start, end],
        "edit": report,
    }
