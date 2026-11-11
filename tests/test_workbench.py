"""End-to-end scenarios for the pickup workbench.

Every requirement is exercised against the HTTP service with a synthesized
password-marked chapter, so assertions are made on real rendered WAV bytes:
adoption, length change, seam placement, room-tone provenance, undo/redo,
reopen, reference-counted deletion, interrupted/corrupt takes, and
changing the adopted take while an export is running.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from array import array

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from workbench import audio, render  # noqa: E402
from workbench.server import WorkbenchServer  # noqa: E402
from workbench.store import Store, StoreError  # noqa: E402

RATE = 8000
STATIC = os.path.join(ROOT, "workbench", "static")
SENT = int(0.40 * RATE)     # 3200 frames per synthetic sentence
TAKE_FREQ = 900.0


def sent_freq(i):
    return 300.0 + 37.0 * i


class Client:
    def __init__(self, base):
        self.base = base

    def _req(self, method, path, data=None, headers=None, timeout=60):
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()

    def j(self, method, path, obj=None):
        data = json.dumps(obj).encode() if obj is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        st, hd, body = self._req(method, path, data, headers)
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body[:200]}
        return st, parsed

    def raw(self, method, path, data=None):
        return self._req(method, path, data,
                         {"Content-Type": "application/octet-stream"} if data else {})


def tone_ranges(pcm, freq, threshold=4.0e6):
    return audio.find_tone_ranges(pcm, RATE, freq, win_s=0.02,
                                  threshold=threshold)


def onset(pcm, freq, threshold=4.0e6):
    rs = tone_ranges(pcm, freq, threshold)
    return rs[0][0] if rs else None


def onset_near(pcm, freq, expected, tol=800):
    """Detect a tone close to a known position (markers repeat in the chapter)."""
    rs = tone_ranges(pcm, freq)
    if not rs:
        return None
    best = min(rs, key=lambda r: abs(r[0] - expected))
    return best[0] if abs(best[0] - expected) <= tol else None


class BaseHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-")
        self.store = Store(self.tmp)
        self.srv = WorkbenchServer(("127.0.0.1", 0), self.store, STATIC)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.c = Client("http://127.0.0.1:%d" % self.srv.server_address[1])

        st, p = self.c.j("POST", "/api/projects", {"name": "测试工程"})
        self.assertEqual(st, 200, p)
        self.project_id = p["project"]["id"]
        st, d = self.c.j("POST", "/api/projects/%d/demo" % self.project_id,
                         {"rate": RATE, "sentences": 8})
        self.assertEqual(st, 200, d)
        self.chapter_id = d["chapter"]["id"]
        self.manifest = d["manifest"]
        st, det = self.c.j("GET", "/api/chapters/%d" % self.chapter_id)
        self.assertEqual(st, 200, det)
        self.detail = det
        self.rt_id = det["roomtone"]["id"]
        self.n = det["timeline_frames"]
        self.sent = {s["index"]: s for s in self.manifest["sentences"]}

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        finally:
            self.store.close()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def take_pcm(self, freq, tone_s, lead_s=0.0, tail_s=0.0, amp=10000):
        pcm = array("h")
        pcm.extend(audio.synth_silence(lead_s, RATE))
        pcm.extend(audio.synth_tone(freq, tone_s, RATE, amp=amp))
        pcm.extend(audio.synth_silence(tail_s, RATE))
        return pcm

    def create_take(self):
        st, t = self.c.j("POST", "/api/takes", {
            "project_id": self.project_id,
            "chapter_id": self.chapter_id,
            "sample_rate": RATE,
        })
        self.assertEqual(st, 200, t)
        return t["take"]["id"]

    def upload_take(self, pcm, device_start_utc="2026-11-11T09:00:00Z",
                    chunk_frames=1024):
        tid = self.create_take()
        payload = pcm.tobytes()
        seq = 0
        step = chunk_frames * 2
        for off in range(0, len(payload), step):
            st, _hd, r = self.c.raw("POST", "/api/takes/%d/chunks?seq=%d" % (tid, seq),
                                    payload[off:off + step])
            self.assertEqual(st, 200, r)
            seq += 1
        st, r = self.c.j("POST", "/api/takes/%d/finalize" % tid, {
            "frames": len(pcm),
            "sha256": audio.pcm_sha256(pcm),
            "device_start_utc": device_start_utc,
        })
        self.assertEqual(st, 200, r)
        return tid

    def mark(self, start, end, version_id=None, label="选段"):
        body = {"start_frame": start, "end_frame": end, "label": label}
        if version_id is not None:
            body["version_id"] = version_id
        st, r = self.c.j("POST", "/api/chapters/%d/selections" % self.chapter_id, body)
        self.assertEqual(st, 200, r)
        return r["selection"]["id"]

    def adopt(self, selection_id, take_id, mode="insert_shift",
              fades=(0, 0), trims=(0, 0), base=None, expect=200):
        body = {
            "selection_id": selection_id, "take_id": take_id, "mode": mode,
            "fade_in_frames": fades[0], "fade_out_frames": fades[1],
            "trim_start_frames": trims[0], "trim_end_frames": trims[1],
        }
        if base is not None:
            body["base_version_id"] = base
        st, r = self.c.j("POST", "/api/chapters/%d/adopt" % self.chapter_id, body)
        self.assertEqual(st, expect, r)
        return r.get("version", {}).get("id") if expect == 200 else r

    def render_sync(self, version_id=None, delay_ms=0):
        body = {"async": False, "delay_ms": delay_ms}
        if version_id is not None:
            body["version_id"] = version_id
        st, r = self.c.j("POST", "/api/chapters/%d/render" % self.chapter_id, body)
        self.assertEqual(st, 200, r)
        return r["job"]

    def job_wav(self, job_id):
        st, hd, body = self.c.raw("GET", "/api/render-jobs/%d/wav" % job_id)
        self.assertEqual(st, 200)
        return audio.read_wav_bytes(body)[0]

    def current_detail(self):
        st, d = self.c.j("GET", "/api/chapters/%d" % self.chapter_id)
        self.assertEqual(st, 200, d)
        self.detail = d
        return d

    def wait_job(self, job_id, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, r = self.c.j("GET", "/api/render-jobs/%d" % job_id)
            self.assertEqual(st, 200, r)
            if r["job"]["status"] in ("done", "failed"):
                return r["job"]
            time.sleep(0.05)
        self.fail("渲染任务超时")


class InsertShiftTests(BaseHTTPTest):
    def test_insert_shift_length_shift_seams_and_password_markers(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        pcm = self.take_pcm(TAKE_FREQ, 0.60, lead_s=0.10)   # 5600 frames
        tid = self.upload_take(pcm)
        f1 = f2 = 160
        delta = len(pcm) - SENT - f1 - f2
        vid = self.adopt(sel, tid, "insert_shift", (f1, f2))
        job = self.render_sync()
        self.assertEqual(job["status"], "done", job)
        rep = job["report"]["edits"][0]
        self.assertEqual(rep["mode"], "insert_shift")
        self.assertEqual(rep["frames_in"], self.n)
        self.assertEqual(rep["frames_out"], self.n + delta)
        self.assertEqual(rep["delta"], delta)
        self.assertEqual(rep["selection_in"], [s["start"], s["end"]])

        out = self.job_wav(job["id"])
        self.assertEqual(len(out), self.n + delta)

        # take body is byte-exact: no stretching, no resampling
        body_start, body_end = rep["take_range_out"]
        self.assertEqual(out[body_start:body_end], pcm[f1:len(pcm) - f2])

        # leading silence was preserved, not auto-used for alignment
        o = onset(out, TAKE_FREQ)
        self.assertIsNotNone(o)
        self.assertGreaterEqual(o - s["start"], 600)
        self.assertEqual(tone_ranges(out, sent_freq(3)), [])

        # every later sentence moved by exactly delta
        for idx in (4, 5, 7):
            expected = self.sent[idx]["start"] + delta
            got = onset_near(out, sent_freq(idx), expected)
            self.assertIsNotNone(got, "句子 %d 未找到" % idx)

        # password markers: head intact, tail shifted with the following audio
        marker = [m for m in self.manifest["markers"]
                  if m["label"] == "marker_password"][0]
        for f in audio.PASSWORD_TONES:
            self.assertIsNotNone(onset(out, f), "口令音 %s 丢失" % f)
        self.assertIsNotNone(onset_near(out, 1000.0, marker["start"]))

        end_marker = [m for m in self.manifest["markers"]
                      if m["label"] == "marker_password_end"][0]
        self.assertIsNotNone(
            onset_near(out, 1750.0, end_marker["start"] + delta, tol=900))

        # seams are smooth (no click) inside the advertised fade windows
        for a, b in rep["fade_regions_out"]:
            self.assertLess(audio.max_step(out[a:b]), 15000)
        self.assertEqual(job["report"]["chain_version_ids"], [vid])

    def test_device_start_utc_is_stored_but_never_drives_alignment(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        pcm = self.take_pcm(TAKE_FREQ, 0.50, lead_s=0.10)
        tid = self.upload_take(pcm, device_start_utc="1999-01-01T00:00:00Z")
        vid = self.adopt(sel, tid, "insert_shift", (0, 0))

        rec = self.store.get_recording(tid)
        self.assertEqual(rec["device_start_utc"], "1999-01-01T00:00:00Z")
        # server-measured duration is derived from received frames only
        self.assertEqual(rec["frames"], len(pcm))
        self.assertIsNotNone(rec["server_first_chunk_utc"])

        out1 = self.job_wav(self.render_sync()["id"])
        # mutate the device timestamp; alignment/render must not change
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE recordings SET device_start_utc=? WHERE id=?",
                ("2030-06-01T12:00:00Z", tid))
            self.store._conn.commit()
        # force a version with identical parameters? re-render same version
        out2 = self.job_wav(self.render_sync(version_id=vid)["id"])
        self.assertEqual(audio.pcm_sha256(out1), audio.pcm_sha256(out2))

        o = onset(out1, TAKE_FREQ)
        self.assertGreaterEqual(o - s["start"], 600)
        self.assertLessEqual(abs(o - (s["start"] + int(0.10 * RATE))), 500)

    def test_keep_duration_rejects_long_take_instead_of_stretching(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        pcm = self.take_pcm(TAKE_FREQ, 0.70)      # 5600 > 3200 selection
        tid = self.upload_take(pcm)
        err = self.adopt(sel, tid, "keep_duration", expect=409)
        self.assertIn("禁止拉伸", err["error"])
        # the same take is fine in insert mode
        vid = self.adopt(sel, tid, "insert_shift", (0, 0))
        job = self.render_sync()
        rep = job["report"]["edits"][0]
        self.assertEqual(rep["delta"], len(pcm) - SENT)
        out = self.job_wav(job["id"])
        bs, be = rep["take_range_out"]
        self.assertEqual(out[bs:be], pcm)

    def test_sample_rate_mismatch_is_refused_not_resampled(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        tid = self.create_take()
        wrong = self.take_pcm(TAKE_FREQ, 0.40)
        # finalize at 16 kHz: same byte payload, different declared rate
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE recordings SET sample_rate=16000 WHERE id=?", (tid,))
            self.store._conn.commit()
        payload = wrong.tobytes()
        for seq, off in enumerate(range(0, len(payload), 2048)):
            st, _hd, _r = self.c.raw(
                "POST", "/api/takes/%d/chunks?seq=%d" % (tid, seq),
                payload[off:off + 2048])
            self.assertEqual(st, 200)
        st, r = self.c.j("POST", "/api/takes/%d/finalize" % tid, {
            "frames": len(wrong), "sha256": audio.pcm_sha256(wrong),
            "device_start_utc": "2026-11-11T09:00:00Z"})
        self.assertEqual(st, 200, r)
        err = self.adopt(sel, tid, "insert_shift", (0, 0), expect=409)
        self.assertIn("采样率不一致", err["error"])


class KeepDurationTests(BaseHTTPTest):
    def test_keep_duration_pads_with_real_roomtone_and_preserves_length(self):
        s = self.sent[4]
        sel = self.mark(s["start"], s["end"])
        take = self.take_pcm(1350.0, 0.25)                  # 2000 frames
        tid = self.upload_take(take)
        f1 = f2 = 160
        fill = SENT - len(take)                             # 1200
        vid = self.adopt(sel, tid, "keep_duration", (f1, f2))
        job = self.render_sync()
        self.assertEqual(job["status"], "done", job)
        rep = job["report"]["edits"][0]
        self.assertEqual(rep["frames_out"], self.n)
        self.assertEqual(rep["delta"], 0)
        self.assertEqual(rep["fill_range_out"], [s["start"] + 2000, s["end"]])

        out = self.job_wav(job["id"])
        self.assertEqual(len(out), self.n)

        # take tone present in its slot
        self.assertIsNotNone(onset_near(out, 1350.0, s["start"], tol=900))
        # later sentence did not move
        self.assertIsNotNone(onset_near(out, sent_freq(5), self.sent[5]["start"]))
        end_marker = [m for m in self.manifest["markers"]
                      if m["label"] == "marker_password_end"][0]
        self.assertIsNotNone(onset_near(out, 1750.0, end_marker["start"]))

        # gap fill is the supplied room-tone recording, sample-exact
        st, hd, rt_bytes = self.c.raw("GET", "/api/recordings/%d/wav" % self.rt_id)
        self.assertEqual(st, 200)
        roomtone, _r = audio.read_wav_bytes(rt_bytes)
        fill_start = s["start"] + 2000
        lo, hi = 170, fill - 170
        self.assertEqual(out[fill_start + lo: fill_start + hi],
                         roomtone[lo:hi])

    def test_roomtone_shortage_is_reported_at_adopt_and_render(self):
        s = self.sent[5]
        sel = self.mark(s["start"], s["end"])
        take = self.take_pcm(1350.0, 0.25)
        tid = self.upload_take(take)
        # first adoption uses the demo project's long room tone
        vid = self.adopt(sel, tid, "keep_duration", (160, 160))

        # user replaces the room-tone asset with a too-short one
        short_rt = audio.synth_roomtone(RATE, 0.05)         # 400 frames
        data = render.wav_bytes(short_rt, RATE)
        st, _hd, r = self.c.raw("POST", "/api/projects/%d/roomtone" % self.project_id, data)
        self.assertEqual(st, 200, r)

        job = self.render_sync()
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("底噪不足", job["error"])

        # and a new keep-duration adoption is refused up front
        sel2 = self.mark(self.sent[6]["start"], self.sent[6]["end"])
        err = self.adopt(sel2, tid, "keep_duration", (160, 160), expect=409)
        self.assertIn("底噪不足", err["error"])

    def test_keep_duration_without_roomtone_asset_is_refused(self):
        # new project without demo room tone
        st, p = self.c.j("POST", "/api/projects", {"name": "无底噪"})
        pid = p["project"]["id"]
        source = audio.synth_tone(500.0, 1.0, RATE)
        st, _hd, r = self.c.raw("POST", "/api/projects/%d/chapters?title=x" % pid,
                                render.wav_bytes(source, RATE))
        self.assertEqual(st, 200, r)
        cid = json.loads(r)["chapter"]["id"]
        sel_st, sel = self.c.j("POST", "/api/chapters/%d/selections" % cid,
                               {"start_frame": 1000, "end_frame": 5000})
        self.assertEqual(sel_st, 200, sel)
        take = self.take_pcm(1200.0, 0.25)
        take_rec = self.store.import_recording(pid, "take", take, RATE, chapter_id=cid)
        st, err = self.c.j("POST", "/api/chapters/%d/adopt" % cid, {
            "selection_id": sel["selection"]["id"], "take_id": take_rec["id"],
            "mode": "keep_duration", "fade_in_frames": 100, "fade_out_frames": 100})
        self.assertEqual(st, 409, err)
        self.assertIn("未提供底噪", err["error"])


class VersionHistoryTests(BaseHTTPTest):
    def test_undo_redo_follow_the_take_chain(self):
        s3, s5 = self.sent[3], self.sent[5]
        sel3 = self.mark(s3["start"], s3["end"])
        take_a = self.take_pcm(TAKE_FREQ, 0.6, lead_s=0.1)
        tid_a = self.upload_take(take_a)
        v1 = self.adopt(sel3, tid_a, "insert_shift", (160, 160))

        sel5 = self.mark(s5["start"] + 2080, s5["end"] + 2080)   # v1 timeline
        take_b = self.take_pcm(1350.0, 0.25)
        tid_b = self.upload_take(take_b)
        v2 = self.adopt(sel5, tid_b, "keep_duration", (160, 160))

        d = self.current_detail()
        self.assertEqual(d["chapter"]["current_version_id"], v2)
        self.assertEqual(len(d["versions"]), 2)
        self.assertEqual([v["take"]["id"] for v in d["versions"]], [tid_a, tid_b])

        # undo: back to v1, chain still traceable in the record
        st, r = self.c.j("POST", "/api/chapters/%d/undo" % self.chapter_id)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["detail"]["chapter"]["current_version_id"], v1)
        self.assertEqual(len(r["detail"]["versions"]), 2)
        job = self.render_sync()
        rep = job["report"]["edits"]
        self.assertEqual([e["take_id"] for e in rep], [tid_a])
        self.assertEqual(job["report"]["chain_version_ids"], [v1])

        # redo: back to v2 with both takes
        st, r = self.c.j("POST", "/api/chapters/%d/redo" % self.chapter_id)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["detail"]["chapter"]["current_version_id"], v2)
        job = self.render_sync()
        self.assertEqual([e["take_id"] for e in job["report"]["edits"]],
                         [tid_a, tid_b])
        self.assertEqual(job["report"]["chain_version_ids"], [v1, v2])

        # version detail exposes its own history
        st, vr = self.c.j("GET", "/api/versions/%d" % v2)
        self.assertEqual(st, 200, vr)
        self.assertEqual([v["id"] for v in vr["chain"]], [v1, v2])

    def test_switching_candidate_keeps_history_and_protects_bytes(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        pcm_a = self.take_pcm(900.0, 0.5, lead_s=0.1)
        pcm_b = self.take_pcm(1400.0, 0.5, lead_s=0.1)
        tid_a = self.upload_take(pcm_a)
        tid_b = self.upload_take(pcm_b)
        v1 = self.adopt(sel, tid_a, "insert_shift", (160, 160))

        # switch the same selection to candidate B
        st, r = self.c.j("POST", "/api/versions/%d/switch" % v1, {"take_id": tid_b})
        self.assertEqual(st, 200, r)
        v2 = r["version"]["id"]
        self.assertEqual(r["version"]["parent_id"], None)
        self.assertEqual(r["version"]["selection_id"], sel)

        out_b = self.job_wav(self.render_sync()["id"])
        self.assertIsNotNone(onset(out_b, 1400.0))
        self.assertIsNone(onset(out_b, 900.0))

        # historical v1 is still renderable and still points at take A
        job_a = self.render_sync(version_id=v1)
        self.assertEqual(job_a["report"]["edits"][0]["take_id"], tid_a)
        out_a = self.job_wav(job_a["id"])
        self.assertIsNotNone(onset(out_a, 900.0))
        self.assertIsNone(onset(out_a, 1400.0))

        # deleting the now-unadopted-but-referenced candidate keeps bytes
        st, r = self.c.j("DELETE", "/api/recordings/%d" % tid_a)
        self.assertEqual(st, 200, r)
        self.assertFalse(r["bytes_removed"])
        self.assertGreaterEqual(r["refs"], 1)
        st, hd, wav = self.c.raw("GET", "/api/recordings/%d/wav" % tid_a)
        self.assertEqual(st, 200)
        self.assertTrue(self.store.verify_recording(tid_a))
        # and v1 still renders with those bytes
        job_a2 = self.render_sync(version_id=v1)
        self.assertEqual(job_a2["status"], "done", job_a2)

        # an unreferenced candidate is fully removed
        tid_c = self.upload_take(self.take_pcm(1600.0, 0.3))
        st, r = self.c.j("DELETE", "/api/recordings/%d" % tid_c)
        self.assertEqual(st, 200, r)
        self.assertTrue(r["bytes_removed"])
        st, hd, body = self.c.raw("GET", "/api/recordings/%d/wav" % tid_c)
        self.assertEqual(st, 404)


class FailureSimulationTests(BaseHTTPTest):
    def test_interrupted_recording_cannot_be_adopted_or_rendered(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        tid = self.create_take()
        payload = self.take_pcm(TAKE_FREQ, 0.4).tobytes()
        st, _hd, _r = self.c.raw("POST", "/api/takes/%d/chunks?seq=0" % tid,
                                 payload[:2048])
        self.assertEqual(st, 200)
        # recording "vanishes" here: no finalize, no abort
        d = self.current_detail()
        cand = [c for c in d["candidates"] if c["id"] == tid][0]
        self.assertEqual(cand["status"], "recording")
        self.assertGreater(cand["received_frames"], 0)
        self.assertEqual(cand["frames"], 0)   # only captured samples count
        err = self.adopt(sel, tid, "insert_shift", expect=409)
        self.assertIn("只有完成的录次", err["error"])

        # server restart freezes it as incomplete
        self.srv.shutdown()
        self.srv.server_close()
        self.store.close()
        self.store = Store(self.tmp)
        rec = self.store.get_recording(tid)
        self.assertEqual(rec["status"], "incomplete")

    def test_unfinished_candidate_write_is_rejected_at_finalize(self):
        tid = self.create_take()
        pcm = self.take_pcm(TAKE_FREQ, 0.4)
        payload = pcm.tobytes()
        st, _hd, _r = self.c.raw("POST", "/api/takes/%d/chunks?seq=0" % tid, payload)
        self.assertEqual(st, 200)
        # client claims more frames than were actually written
        st, r = self.c.j("POST", "/api/takes/%d/finalize" % tid, {
            "frames": len(pcm) + 100, "sha256": audio.pcm_sha256(pcm),
            "device_start_utc": "2026-11-11T09:00:00Z"})
        self.assertEqual(st, 409, r)
        self.assertEqual(self.store.get_recording(tid)["status"], "corrupt")

        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        err = self.adopt(sel, tid, "insert_shift", expect=409)
        self.assertIn("只有完成的录次", err["error"])

    def test_render_refuses_take_bytes_corrupted_after_adoption(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        tid = self.upload_take(self.take_pcm(TAKE_FREQ, 0.4, lead_s=0.1))
        self.adopt(sel, tid, "insert_shift", (160, 160))
        # disk-level damage after the take was adopted
        rec = self.store.get_recording(tid)
        path = os.path.join(self.tmp, rec["path"])
        audio.write_wav(path, audio.synth_tone(TAKE_FREQ, 0.2, RATE), RATE)
        job = self.render_sync()
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("校验失败", job["error"])
        self.assertIn(str(tid), job["error"])

    def test_selection_changed_while_export_runs(self):
        s3, s5 = self.sent[3], self.sent[5]
        sel3 = self.mark(s3["start"], s3["end"])
        tid_a = self.upload_take(self.take_pcm(900.0, 0.5, lead_s=0.1))
        v1 = self.adopt(sel3, tid_a, "insert_shift", (160, 160))
        self.assertEqual(self.current_detail()["chapter"]["current_version_id"], v1)

        st, r = self.c.j("POST", "/api/chapters/%d/render" % self.chapter_id,
                         {"version_id": v1, "delay_ms": 1000, "async": True})
        self.assertEqual(st, 200, r)
        job_id = r["job"]["id"]

        time.sleep(0.1)
        # editor re-adopts a different take while export is in flight
        shift = len(self.take_pcm(900.0, 0.5, lead_s=0.1)) - SENT - 320
        sel5 = self.mark(s5["start"] + shift, s5["end"] + shift)
        tid_b = self.upload_take(self.take_pcm(1650.0, 0.25))
        v2 = self.adopt(sel5, tid_b, "keep_duration", (160, 160))
        self.assertEqual(self.current_detail()["chapter"]["current_version_id"], v2)

        job = self.wait_job(job_id, timeout=10)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["version_id"], v1)
        self.assertTrue(job["stale"])
        self.assertTrue(job["report"]["stale_at_finish"])
        self.assertEqual(job["report"]["chain_version_ids"], [v1])
        self.assertEqual(job["report"]["current_version_at_finish"], v2)

        out = self.job_wav(job_id)
        # snapshot used candidate A; candidate B never leaked into the export
        self.assertIsNotNone(onset(out, 900.0))
        self.assertEqual(tone_ranges(out, 1650.0), [])
        self.assertEqual(len(out), self.n + shift)


class PreviewTests(BaseHTTPTest):
    def test_side_by_side_context_preview(self):
        s = self.sent[3]
        sel = self.mark(s["start"], s["end"])
        take = self.take_pcm(900.0, 0.5, lead_s=0.1)
        tid = self.upload_take(take)
        ctx = 800
        f1 = f2 = 160

        st, a = self.c.j("POST", "/api/chapters/%d/preview" % self.chapter_id, {
            "selection_id": sel, "take_id": None, "mode": "insert_shift",
            "fade_in_frames": f1, "fade_out_frames": f2,
            "context_frames": ctx})
        self.assertEqual(st, 200, a)
        st, b = self.c.j("POST", "/api/chapters/%d/preview" % self.chapter_id, {
            "selection_id": sel, "take_id": tid, "mode": "insert_shift",
            "fade_in_frames": f1, "fade_out_frames": f2,
            "context_frames": ctx})
        self.assertEqual(st, 200, b)

        pcm_a = audio.read_wav_bytes(base64.b64decode(a["wav_base64"]))[0]
        pcm_b = audio.read_wav_bytes(base64.b64decode(b["wav_base64"]))[0]
        self.assertEqual(a["report"]["window"],
                         [max(0, s["start"] - ctx), min(self.n, s["end"] + ctx)])
        # original context still has the old sentence; candidate preview doesn't
        self.assertIsNotNone(onset(pcm_a, sent_freq(3)))
        self.assertIsNone(onset(pcm_b, sent_freq(3)))
        self.assertIsNone(onset(pcm_a, 900.0))
        self.assertIsNotNone(onset(pcm_b, 900.0))
        # preview window grows by the same delta as a real edit
        self.assertEqual(len(pcm_b) - len(pcm_a),
                         (len(take) - SENT - f1 - f2))


class StaticUITests(unittest.TestCase):
    def test_editor_page_and_assets_are_served(self):
        tmp = tempfile.mkdtemp(prefix="wb-static-")
        store = Store(tmp)
        srv = WorkbenchServer(("127.0.0.1", 0), store, STATIC)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        c = Client("http://127.0.0.1:%d" % srv.server_address[1])
        try:
            st, hd, body = c.raw("GET", "/")
            self.assertEqual(st, 200)
            self.assertIn("补录选段工作台".encode(), body)
            self.assertIn(b"/static/app.js", body)
            st, hd, js = c.raw("GET", "/static/app.js")
            self.assertEqual(st, 200)
            self.assertIn(b"startRecording", js)
            st, hd, css = c.raw("GET", "/static/style.css")
            self.assertEqual(st, 200)
            st, hd, missing = c.raw("GET", "/static/nope.js")
            self.assertEqual(st, 404)
        finally:
            srv.shutdown(); srv.server_close(); store.close()
            shutil.rmtree(tmp, ignore_errors=True)


class AudioMathTests(unittest.TestCase):
    """Direct checks of the splice math, independent of HTTP."""

    def setUp(self):
        self.n = 4000
        self.parent = audio.synth_tone(400.0, self.n / RATE, RATE)
        self.roomtone = audio.synth_roomtone(RATE, 1.0)

    def test_insert_shift_formula(self):
        take = audio.synth_tone(900.0, 0.3, RATE)   # 2400
        out, rep = audio.apply_edit(
            self.parent, 1000, 3000, take, "insert_shift", 100, 100, None)
        self.assertEqual(len(out), self.n - 2000 + 2400 - 200)
        self.assertEqual(rep["delta"], len(out) - self.n)
        self.assertEqual(rep["take_range_out"],
                         [1000, 1000 + 2400 - 200])

    def test_keep_duration_formula_and_roomtone_provenance(self):
        take = audio.synth_tone(900.0, 0.1, RATE)   # 800
        out, rep = audio.apply_edit(
            self.parent, 1000, 3000, take, "keep_duration", 100, 100,
            self.roomtone)
        self.assertEqual(len(out), self.n)
        self.assertEqual(rep["delta"], 0)
        fill = 2000 - 800
        self.assertEqual(rep["fill_range_out"], [1800, 3000])
        self.assertEqual(out[1800 + 150: 3000 - 150],
                         self.roomtone[150: fill - 150])

    def test_keep_duration_requires_roomtone_for_short_take(self):
        take = audio.synth_tone(900.0, 0.1, RATE)
        with self.assertRaises(audio.EditError) as cm:
            audio.apply_edit(self.parent, 1000, 3000, take, "keep_duration",
                             0, 0, None)
        self.assertIn("底噪", str(cm.exception))
        with self.assertRaises(audio.EditError) as cm:
            audio.apply_edit(self.parent, 1000, 3000, take, "keep_duration",
                             0, 0, audio.synth_roomtone(RATE, 0.01))
        self.assertIn("底噪不足", str(cm.exception))

    def test_long_take_in_keep_mode_is_refused(self):
        take = audio.synth_tone(900.0, 0.4, RATE)    # 3200 > 2000
        with self.assertRaises(audio.EditError) as cm:
            audio.apply_edit(self.parent, 1000, 3000, take, "keep_duration",
                             0, 0, self.roomtone)
        self.assertIn("禁止拉伸", str(cm.exception))

    def test_fade_must_fit_real_audio(self):
        with self.assertRaises(audio.EditError):
            audio.apply_edit(self.parent, 0, 2000, audio.synth_tone(900, 0.3, RATE),
                             "insert_shift", 50, 0, None)
        with self.assertRaises(audio.EditError):
            audio.apply_edit(self.parent, 1000, 3000, audio.synth_tone(900, 0.05, RATE),
                             "insert_shift", 500, 500, None)


class PersistenceTests(unittest.TestCase):
    def test_reopen_project_keeps_versions_and_undo_history(self):
        tmp = tempfile.mkdtemp(prefix="wb-persist-")
        try:
            store = Store(tmp)
            project = store.create_project("重开工程")
            pcm, manifest = audio.synth_chapter(rate=RATE, n_sentences=4)
            src = store.import_recording(project["id"], "source", pcm, RATE)
            rt = store.import_recording(project["id"], "roomtone",
                                        audio.synth_roomtone(RATE, 2.0), RATE)
            store.set_roomtone(project["id"], rt["id"])
            ch = store.create_chapter(project["id"], "第一章", src["id"])
            s = manifest["sentences"][1]

            sel = store.create_selection(ch["id"], None, s["start"], s["end"])
            take_pcm = audio.synth_tone(900.0, 0.5, RATE)
            tid = store.create_take(project["id"], ch["id"], RATE)["id"]
            store.append_take_chunk(tid, 0, take_pcm.tobytes())
            store.finalize_take(tid, len(take_pcm), audio.pcm_sha256(take_pcm),
                                "2026-11-11T09:00:00Z")
            v1 = store.create_version(ch["id"], sel["id"], tid, "insert_shift",
                                      160, 160)["id"]

            # interrupted take left mid-capture
            hanging = store.create_take(project["id"], ch["id"], RATE)["id"]
            store.append_take_chunk(hanging, 0, b"\x00\x00" * 100)
            store.close()   # process "crashes"

            store = Store(tmp)
            self.assertEqual(store.get_recording(hanging)["status"], "incomplete")
            ch2 = store.get_chapter(ch["id"])
            self.assertEqual(ch2["current_version_id"], v1)
            versions = store.list_versions(ch["id"])
            self.assertEqual([v["take_id"] for v in versions], [tid])
            pcm_out, reports = render.render_version_pcm(store, ch["id"], v1,
                                                         use_cache=False)
            self.assertEqual(reports[0]["take_id"], tid)
            self.assertEqual(len(pcm_out),
                             len(pcm) - (s["end"] - s["start"]) + len(take_pcm) - 320)
            # selection metadata survived the reopen
            sel2 = store.get_selection(sel["id"])
            self.assertEqual((sel2["start_frame"], sel2["end_frame"]),
                             (s["start"], s["end"]))
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
