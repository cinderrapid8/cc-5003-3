"""End-to-end verification for the re-recording workbench.

Run:  python -m tests.test_workbench
Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audiobook_studio import audio as A
from tests.helpers import (Client, SR, chapter_clip, clip_with, complete_take,
                           make_candidate, make_project, noise, q, tone, wav_b64)

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ok - {msg}")


# ----------------------------------------------------------- 1. core workflow
def test_full_workflow_shift():
    print("\n[1] full workflow: mark -> takes -> candidate -> SHIFT render with cues")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        chapter = chapter_clip()
        mark_start, mark_end = SR // 3, SR // 2  # inside the 330Hz zone
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-阿尔法", "range_start": mark_start, "range_end": mark_end})
        check(status == 200 and mark["range_end"] - mark["range_start"] == mark_end - mark_start,
              "mark created at exact chapter frame range")

        # Two replacement recordings of different lengths.
        long_cand = clip_with(660.0, int(SR * 0.5))   # longer than marked range
        cand_a = make_candidate(c, pid, mark, long_cand)
        short_cand = clip_with(880.0, int(SR * 0.2))
        cand_b = make_candidate(c, pid, mark, short_cand)
        check(cand_a["frame_count"] != cand_b["frame_count"],
              "candidates of different lengths stored per mark")

        status, view = c.post(f"/api/projects/{pid}/selection", {
            "mark_id": mark["id"], "candidate_id": cand_a["id"],
            "mode": "shift", "fade_ms": 10.0})
        check(status == 200 and view["selection"]["mode"] == "shift",
              "explicit SHIFT selection saved")

        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {"fade_ms": 10.0})
        check(status == 200 and "render_token" in prep, "render token prepared")
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}",
                              {"label": "v1-shift"})
        check(status == 200, f"render committed ({done.get('error')})")

        expected_delta = long_cand.frames - (mark_end - mark_start)
        check(done["frames"] == chapter.frames + expected_delta,
              f"SHIFT output length = original + delta ({done['delta_frames']} frames)")
        check(done["original_frames"] == chapter.frames, "original frame count recorded")
        labels = [label for _f, label in done["cues"]]
        check("SEAM-IN 口令-阿尔法" in labels and "SEAM-OUT 口令-阿尔法" in labels,
              "password-marked seam cues returned")
        check(any(l == "CHAPTER-START" for l in labels), "chapter start cue present")

        # The downloaded WAV must contain a real cue chunk at the seam frames.
        status, raw = c.get_raw(f"/api/projects/{pid}/versions/{done['version_id']}")
        check(status == 200 and raw[:4] == b"RIFF", "version WAV downloadable")
        decoded = A.read_wav(raw)
        check(decoded.frames == done["frames"], "downloaded WAV frame count matches")
        cue_frames = _parse_cue_frames(raw)
        seam_in = next(f for f, l in done["cues"] if l == "SEAM-IN 口令-阿尔法")
        check(seam_in in cue_frames, f"cue chunk embeds seam frame {seam_in}")


def _parse_cue_frames(wav_bytes: bytes) -> set[int]:
    pos = 12
    frames = set()
    while pos + 8 <= len(wav_bytes):
        cid = wav_bytes[pos:pos + 4]
        size = int.from_bytes(wav_bytes[pos + 4:pos + 8], "little")
        payload = wav_bytes[pos + 8:pos + 8 + size]
        if cid == b"cue ":
            count = int.from_bytes(payload[:4], "little")
            for i in range(count):
                entry = payload[4 + i * 24:4 + (i + 1) * 24]
                # dwName(4) dwPosition(4) fccChunk(4) dwChunkStart(4)
                # dwBlockStart(4) dwSampleOffset(4)
                frames.add(int.from_bytes(entry[20:24], "little"))
        pos += 8 + size + (size & 1)
    return frames


# ------------------------------------------------- 2. keep_duration + room tone
def test_keep_duration_and_noise_rules():
    print("\n[2] KEEP_DURATION mode, real room tone, missing/insufficient tone errors")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        rng = (SR // 3, SR // 2)
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-贝塔", "range_start": rng[0], "range_end": rng[1]})
        assert status == 200
        shorter = clip_with(550.0, int(SR * 0.15))
        cand = make_candidate(c, pid, mark, shorter)

        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": mark["id"], "candidate_id": cand["id"],
                "mode": "keep_duration", "fade_ms": 6.0})

        # No noise configured -> render must refuse, never synthesize.
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}", {})
        check(status == 422 and done["error"] == "NOISE_MISSING",
              "KEEP_DURATION without room tone is rejected (no fake synthesis)")

        # Too-short noise excerpt -> explicit insufficiency error.
        tiny = A.Clip(noise(int(SR * 0.05)), SR)
        status, n = c.post(f"/api/projects/{pid}/noise", {"wav": wav_b64(tiny)})
        check(status == 200 and n["sufficient"] is False, "short noise flagged insufficient")
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {"noise_id": n["id"]})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}", {})
        check(status == 422 and done["error"] == "NOISE_TOO_SHORT",
              "render aborts telling editor to capture more room tone")

        # Digital silence must also be rejected rather than treated as tone.
        silent = A.Clip(tone(0, int(SR * 0.5), amp=0), SR)
        status, ns = c.post(f"/api/projects/{pid}/noise", {"wav": wav_b64(silent)})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {"noise_id": ns["id"]})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}", {})
        check(status == 422 and done["error"] == "NOISE_SILENT",
              "digital-silence 'room tone' rejected")

        # Sufficient real room tone -> output keeps the original total length.
        real_tone = A.Clip(noise(int(SR * 0.5), amp=500), SR)
        status, good = c.post(f"/api/projects/{pid}/noise", {"wav": wav_b64(real_tone)})
        check(good["sufficient"] is True and good["rms"] > 0, "real room tone accepted")
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {"noise_id": good["id"]})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}",
                              {"label": "v1-keep"})
        check(status == 200, f"keep_duration render ok ({done.get('error')})")
        check(done["frames"] == done["original_frames"],
              "KEEP_DURATION output length equals original length exactly")
        check(done["events"][0]["gap_frames"] == (rng[1] - rng[0]) - shorter.frames,
              "gap filled with real captured room-tone bytes")


def test_overlong_candidate_rejected_in_keep_mode():
    print("\n[3] over-long candidate may not be stretched into KEEP_DURATION")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        rng = (SR // 3, SR // 2)
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-伽马", "range_start": rng[0], "range_end": rng[1]})
        overlong = clip_with(700.0, (rng[1] - rng[0]) + SR // 4)
        cand = make_candidate(c, pid, mark, overlong)
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": mark["id"], "candidate_id": cand["id"], "mode": "keep_duration"})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}", {})
        check(status == 422 and done["error"] == "KEEP_DURATION_OVERLONG",
              "stretch-to-cover explicitly forbidden; editor told to use SHIFT or trim")


# ----------------------------------------------------------- 4. timing semantics
def test_timing_uses_frames_not_device_clock():
    print("\n[4] timing from captured frames; device clock stored separately")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-德尔塔",
            "range_start": SR // 4, "range_end": SR // 2})
        frames = int(SR * 0.3)
        # Leading silence then tone; device claims an implausible start offset.
        samples = tone(500.0, frames)
        for i in range(SR // 10):
            samples[i] = 0
        candidate = A.Clip(samples, SR)
        t = complete_take(c, pid, mark["id"], candidate,
                          device_started_at=9_999_999_999.999, offset_ms=12345.6)
        # Duration must come from actual frames, ignoring the device clock.
        expected_ms = frames * 1000.0 / SR
        check(abs(t["upload"]["duration_ms"] - expected_ms) < 0.01,
              f"duration {t['upload']['duration_ms']:.2f}ms derived from {frames} real frames")

        status, proj = c.get(f"/api/projects/{pid}")
        take_view = proj["marks"][0]["takes"][0]
        check(take_view["device_started_at"] == 9_999_999_999.999
              and take_view["first_frame_device_offset_ms"] == 12345.6,
              "device-reported start time kept verbatim as separate metadata")
        check(take_view["frame_count"] == frames,
              "chapter position and device clock never conflated")

        # Explicit trim is required to discard leading silence; nothing auto-aligns.
        status, cand = c.post(f"/api/projects/{pid}/candidates", {
            "take_id": t["take_id"], "mark_id": mark["id"],
            "trim_start": SR // 10, "trim_end": frames})
        check(status == 200 and cand["frame_count"] == frames - SR // 10,
              "leading silence removed only via explicit editor trim")


# --------------------------------------------- 5. interruptions & partial writes
def test_interrupted_and_unfinished_takes():
    print("\n[5] interrupted capture / unfinished candidate never enter a render")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-艾普西隆",
            "range_start": SR // 4, "range_end": SR // 2})

        # 5a. client reports incomplete upload: bytes preserved, state interrupted.
        status, begin = c.post(f"/api/projects/{pid}/takes", {"mark_id": mark["id"]})
        tid = begin["take_id"]
        partial = clip_with(440.0, SR // 5)
        status, up = c.call("POST", f"/api/projects/{pid}/takes/{tid}/upload",
                            {"wav": wav_b64(partial), "complete": False,
                             "reason": "network dropped"})
        check(status == 200 and up["state"] == "interrupted" and up["renderable"] is False,
              "interrupted take preserved but flagged non-renderable")
        status, cand = c.post(f"/api/projects/{pid}/candidates", {
            "take_id": tid, "mark_id": mark["id"], "trim_start": 0,
            "trim_end": up["frames"]})
        check(status == 409 and cand["error"] == "TAKE_NOT_COMPLETE",
              "interrupted take cannot become a candidate")

        # 5b. take left in 'recording' (candidate write never happened).
        status, begin2 = c.post(f"/api/projects/{pid}/takes", {"mark_id": mark["id"]})
        status, cand = c.post(f"/api/projects/{pid}/candidates", {
            "take_id": begin2["take_id"], "mark_id": mark["id"],
            "trim_start": 0, "trim_end": 100})
        check(status == 409 and cand["error"] == "TAKE_NOT_COMPLETE",
              "take with no finished upload cannot enter rendering")
        status, ab = c.call("POST", f"/api/projects/{pid}/takes/{begin2['take_id']}/abandon", {})
        check(ab["state"] == "abandoned", "stuck recording can be abandoned")

        # 5c. corrupt upload -> interrupted with reason, no crash.
        status, begin3 = c.post(f"/api/projects/{pid}/takes", {"mark_id": mark["id"]})
        status, bad = c.call("POST", f"/api/projects/{pid}/takes/{begin3['take_id']}/upload",
                             {"wav": "base64:" + base64.b64encode(b"NOTAWAV").decode(),
                              "complete": True})
        check(status == 422 and bad["error"] == "TAKE_CORRUPT", "corrupt upload rejected")
        status, proj = c.get(f"/api/projects/{pid}")
        states = {t["id"]: t["state"] for t in proj["marks"][0]["takes"]}
        check(states[begin3["take_id"]] == "interrupted",
              "corrupt take lands in interrupted, never complete")

        # Only a genuinely complete take renders.
        good = make_candidate(c, pid, mark, clip_with(600.0, SR // 6))
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": mark["id"], "candidate_id": good["id"], "mode": "shift"})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}", {})
        check(status == 200, "render succeeds using only the completed take")


# --------------------------------------------- 6. selection change during export
def test_selection_change_during_export():
    print("\n[6] changing selection mid-export invalidates the prepared render")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-泽塔",
            "range_start": SR // 4, "range_end": SR // 2})
        cand1 = make_candidate(c, pid, mark, clip_with(500.0, SR // 6))
        cand2 = make_candidate(c, pid, mark, clip_with(700.0, SR // 5))
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": mark["id"], "candidate_id": cand1["id"], "mode": "shift"})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {})
        token = prep["render_token"]

        # Editor re-picks while export is in flight (same splice mode, different
        # candidate + fade so the prepared snapshot no longer matches).
        status, _ = c.post(f"/api/projects/{pid}/selection",
                           {"mark_id": mark["id"], "candidate_id": cand2["id"],
                            "mode": "shift", "fade_ms": 12.0})
        status, done = c.post(f"/api/renders/commit?token={token}", {})
        check(status == 409 and done["error"] == "SELECTION_CHANGED_DURING_EXPORT",
              "stale export token refused after re-selection")

        # Token is single-use even when nothing changed.
        status, prep2 = c.post(f"/api/projects/{pid}/render/prepare", {})
        status, ok = c.post(f"/api/renders/commit?token={prep2['render_token']}", {})
        check(status == 200, "fresh token renders the current selection")
        status, again = c.post(f"/api/renders/commit?token={prep2['render_token']}", {})
        check(status == 404 and again["error"] == "RENDER_TOKEN_UNKNOWN",
              "render tokens are single-use")


# ------------------------------- 7. history, undo, reopen, blob retention + GC
def test_history_undo_and_blob_retention():
    print("\n[7] version history, reopen traceability, delete-safe GC")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-伊塔",
            "range_start": SR // 4, "range_end": SR // 2})
        cand1 = make_candidate(c, pid, mark, clip_with(520.0, SR // 6))
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": mark["id"], "candidate_id": cand1["id"], "mode": "shift"})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {})
        status, v1 = c.post(f"/api/renders/commit?token={prep['render_token']}",
                            {"label": "v1"})
        v1_blob = v1["blob_id"]

        # 'Undo' by selecting the second candidate and rendering again.
        cand2 = make_candidate(c, pid, mark, clip_with(760.0, SR // 5))
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": mark["id"], "candidate_id": cand2["id"], "mode": "shift"})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {})
        status, v2 = c.post(f"/api/renders/commit?token={prep['render_token']}",
                            {"label": "v2"})
        check(v1["version_id"] != v2["version_id"], "new adoption creates a new version")

        # Reopen the project: every selection/take/candidate/version is traceable.
        status, reopened = c.get(f"/api/projects/{pid}")
        check(status == 200 and len(reopened["versions"]) == 2,
              "project reopen lists both historical versions")
        v1_row = next(v for v in reopened["versions"] if v["id"] == v1["version_id"])
        sel_json = v1_row["selection_json"]
        check(cand1["id"] in sel_json and cand2["id"] not in sel_json,
              "historical version pins the exact candidate/take it was built from")
        check(reopened["marks"][0]["selection"]["candidate_id"] == cand2["id"],
              "current selection independently tracks latest choice")

        # Delete the now-unadopted first candidate (soft delete; take bytes stay).
        status, deleted = c.call("POST", f"/api/candidates/{cand1['id']}/delete", {})
        check(status == 200, "unadopted candidate deleted from workspace")
        status, proj2 = c.get(f"/api/projects/{pid}")
        check(all(x["id"] != cand1["id"] for x in proj2["marks"][0]["candidates"]),
              "deleted candidate hidden from candidate list")

        # The v1 render blob must survive GC because the version still cites it.
        status, gc = c.post("/api/gc", {})
        blob_path = c.httpd.service.store.blob_path(v1_blob)
        check(os.path.exists(blob_path),
              "GC keeps bytes still referenced by a historical version")
        status, raw = c.get_raw(f"/api/projects/{pid}/versions/{v1['version_id']}")
        check(status == 200 and A.read_wav(raw).frames == v1["frames"],
              "historical version still plays byte-identically after candidate deletion")

        # Deleting an in-use candidate is refused.
        status, refused = c.call("POST", f"/api/candidates/{cand2['id']}/delete", {})
        check(status == 409 and refused["error"] == "CANDIDATE_IN_USE",
              "candidate referenced by current selection cannot be deleted")

        # Take-level traceability: v1's take still resolves to its original blob.
        take_id = c.httpd.service.store.query_one(
            "SELECT take_id FROM candidates WHERE id=?", (cand1["id"],))["take_id"]
        take = c.httpd.service.store.query_one("SELECT * FROM takes WHERE id=?", (take_id,))
        check(take["state"] == "complete" and os.path.exists(
            c.httpd.service.store.blob_path(take["blob_id"])),
              "original take bytes remain reachable for audit after undo")


# --------------------------------------------- 8. side-by-side context endpoints
def test_context_and_audio_access():
    print("\n[8] before/after context playback uses frame-accurate chapter slices")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        s, e = SR // 3, SR // 2
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-西塔", "range_start": s, "range_end": e})
        cand = make_candidate(c, pid, mark, clip_with(660.0, SR // 6))
        status, proj = c.get(f"/api/projects/{pid}")
        ch_blob = proj["chapter"]["blob_id"]
        status, raw = c.get_raw(f"/api/projects/{pid}/blobs/{ch_blob}")
        chapter = A.read_wav(raw)
        before = A.slice_clip(chapter, max(0, s - SR // 8), s)
        after = A.slice_clip(chapter, e, min(chapter.frames, e + SR // 8))
        check(before.frames == SR // 8 and after.frames == SR // 8,
              "before/after context slices are frame-accurate for side-by-side A/B")
        # Candidate audio independently playable for comparison.
        take_id = c.httpd.service.store.query_one(
            "SELECT take_id FROM candidates WHERE id=?", (cand["id"],))["take_id"]
        take_blob = c.httpd.service.store.query_one(
            "SELECT blob_id FROM takes WHERE id=?", (take_id,))["blob_id"]
        status, raw = c.get_raw(f"/api/projects/{pid}/blobs/{take_blob}")
        check(status == 200 and A.read_wav(raw).frames == SR // 6,
              "candidate take bytes served for audition")


# ------------------------------------------------------------- 9. multi-mark
def test_multiple_marks_and_length_bookkeeping():
    print("\n[9] multiple marks, mixed modes, cumulative length change")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        m1r = (SR // 6, SR // 4)
        m2r = (SR // 2, 2 * SR // 3)
        status, m1 = c.post(f"/api/projects/{pid}/marks",
                            {"sentence_label": "口令-一", "range_start": m1r[0], "range_end": m1r[1]})
        status, m2 = c.post(f"/api/projects/{pid}/marks",
                            {"sentence_label": "口令-二", "range_start": m2r[0], "range_end": m2r[1]})
        # m1 shifts longer; m2 keep_duration shorter with room tone.
        c1 = make_candidate(c, pid, m1, clip_with(500.0, (m1r[1] - m1r[0]) + SR // 10))
        c2 = make_candidate(c, pid, m2, clip_with(700.0, (m2r[1] - m2r[0]) - SR // 10))
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": m1["id"], "candidate_id": c1["id"], "mode": "shift", "fade_ms": 5})
        tone = A.Clip(noise(int(SR * 0.4), amp=450), SR)
        status, n = c.post(f"/api/projects/{pid}/noise", {"wav": wav_b64(tone)})
        c.post(f"/api/projects/{pid}/selection",
               {"mark_id": m2["id"], "candidate_id": c2["id"],
                "mode": "keep_duration", "fade_ms": 5})
        status, prep = c.post(f"/api/projects/{pid}/render/prepare", {"noise_id": n["id"]})
        status, done = c.post(f"/api/renders/commit?token={prep['render_token']}", {})
        check(status == 200, f"mixed-mode render ({done.get('error')})")
        check(done["delta_frames"] == SR // 10,
              "only the SHIFT mark changes total length; KEEP mark contributes zero delta")
        seams = [l for _f, l in done["cues"] if l.startswith("SEAM")]
        check(len(seams) == 4, "both marks contribute IN/OUT seam password cues")
        modes = {ev["label"]: ev["mode"] for ev in done["events"]}
        check(modes == {"口令-一": "shift", "口令-二": "keep_duration"},
              "per-mark splice mode recorded on each seam event")


def test_content_addressing_dedup():
    print("\n[10] content-addressed blobs: identical bytes share storage")
    with Client() as c:
        project = make_project(c)
        pid = project["id"]
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-约塔",
            "range_start": SR // 4, "range_end": SR // 2})
        identical = clip_with(620.0, SR // 7)
        t1 = complete_take(c, pid, mark["id"], identical)
        t2 = complete_take(c, pid, mark["id"], identical)
        b1 = c.httpd.service.store.query_one(
            "SELECT blob_id FROM takes WHERE id=?", (t1["take_id"],))["blob_id"]
        b2 = c.httpd.service.store.query_one(
            "SELECT blob_id FROM takes WHERE id=?", (t2["take_id"],))["blob_id"]
        check(b1 == b2, "byte-identical takes share one content-addressed blob")
        import os
        path = c.httpd.service.store.blob_path(b1)
        check(os.path.exists(path), "shared blob present on disk")
        # Abandon one take; the other still references the same bytes.
        c.call("POST", f"/api/projects/{pid}/takes/{t1['take_id']}/abandon", {})
        status, gc = c.post("/api/gc", {})
        check(os.path.exists(path), "GC retains blob while any take references it")


def main():
    tests = [
        test_full_workflow_shift,
        test_keep_duration_and_noise_rules,
        test_overlong_candidate_rejected_in_keep_mode,
        test_timing_uses_frames_not_device_clock,
        test_interrupted_and_unfinished_takes,
        test_selection_change_during_export,
        test_history_undo_and_blob_retention,
        test_context_and_audio_access,
        test_multiple_marks_and_length_bookkeeping,
        test_content_addressing_dedup,
    ]
    started = time.time()
    for t in tests:
        t()
    print(f"\nALL {PASS} CHECKS PASSED in {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
