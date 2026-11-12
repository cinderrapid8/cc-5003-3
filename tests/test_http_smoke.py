"""HTTP-layer smoke tests: static assets, take audio, full server lifecycle."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audiobook_studio import audio as A
from tests.helpers import (Client, SR, chapter_clip, clip_with, make_candidate,
                           make_project, wav_b64)

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ok - {msg}")


def main():
    print("\n[S] HTTP smoke & static serving")
    with Client() as c:
        status, body = c.get_raw("/")
        check(status == 200 and b"<!DOCTYPE html>" in body, "index.html served")
        status, body = c.get_raw("/app.js")
        check(status == 200 and b"pcm16ToWav" in body, "app.js served")
        status, body = c.get_raw("/../audiobook_studio/storage.py")
        check(status in (403, 404), "path traversal outside web root blocked")

        status, body = c.get("/api/health")
        check(status == 200 and body["ok"] is True, "health endpoint")

        project = make_project(c)
        pid = project["id"]
        status, mark = c.post(f"/api/projects/{pid}/marks", {
            "sentence_label": "口令-冒烟", "range_start": SR // 4, "range_end": SR // 2})
        cand = make_candidate(c, pid, mark, clip_with(440.0, SR // 8))
        take_id = c.httpd.service.store.query_one(
            "SELECT take_id FROM candidates WHERE id=?", (cand["id"],))["take_id"]

        status, raw = c.get_raw(f"/api/takes/{take_id}/wav")
        check(status == 200 and raw[:4] == b"RIFF", "take WAV downloadable for A/B audition")
        check(A.read_wav(raw).frames == SR // 8, "take WAV decodes to recorded frames")

        status, missing = c.get_raw("/api/takes/does-not-exist/wav")
        check(status == 404, "unknown take returns 404")

        # 404 JSON on unknown API route
        status, body = c.get("/api/nope")
        check(status == 404 and body["error"] == "NOT_FOUND", "unknown route -> JSON 404")

    print(f"\nSMOKE {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
