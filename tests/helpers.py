"""Shared helpers for the workbench verification suite."""
from __future__ import annotations

import array
import base64
import json
import math
import tempfile
import threading
import urllib.request
import urllib.error
from urllib.parse import quote

from audiobook_studio import audio as A
from audiobook_studio.service import build_server

SR = 8000  # low rate keeps the pure-Python render pipeline fast


def tone(freq: float, frames: int, rate: int = SR, amp: int = 12000,
         phase: float = 0.0) -> array.array:
    out = array.array("h", [0] * frames)
    for i in range(frames):
        out[i] = int(amp * math.sin(2 * math.pi * freq * i / rate + phase))
    return out


def noise(frames: int, amp: int = 400, seed: int = 7) -> array.array:
    # Deterministic LCG so tests are reproducible; values mimic room hiss.
    x = seed
    out = array.array("h", [0] * frames)
    for i in range(frames):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out[i] = (x % (2 * amp)) - amp
    return out


def chapter_clip(rate: int = SR, seconds: float = 2.0) -> A.Clip:
    frames = int(rate * seconds)
    s = array.array("h", [0] * frames)
    # Three distinct frequency zones so seams are identifiable by ear/spectrum.
    for i in range(frames):
        if i < frames // 3:
            f = 220.0
        elif i < 2 * frames // 3:
            f = 330.0
        else:
            f = 440.0
        s[i] = int(10000 * math.sin(2 * math.pi * f * i / rate))
    return A.Clip(s, rate)


def clip_with(tone_freq: float, frames: int, rate: int = SR) -> A.Clip:
    return A.Clip(tone(tone_freq, frames, rate, amp=9000), rate)


def wav_b64(clip: A.Clip) -> str:
    return "base64:" + base64.b64encode(A.write_wav(clip)).decode("ascii")


class Client:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = build_server(self.tmp.name, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method: str, path: str, body: dict | None = None,
             raw: bool = False):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload
                return resp.status, json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if raw:
                return exc.code, payload
            try:
                return exc.code, json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                return exc.code, {"raw": payload.decode("utf-8", "replace")}

    # convenience verbs
    def get(self, path):
        return self.call("GET", path)

    def get_raw(self, path):
        return self.call("GET", path, raw=True)

    def post(self, path, body):
        return self.call("POST", path, body)


def make_project(client: Client, clip: A.Clip | None = None, name: str = "测试章节") -> dict:
    clip = clip or chapter_clip()
    status, body = client.post("/api/projects", {"name": name, "chapter_wav": wav_b64(clip)})
    assert status == 200, body
    return body


def complete_take(client: Client, pid: str, mark_id: str, candidate: A.Clip,
                  device_started_at: float = 1_700_000_000.123,
                  offset_ms: float = 47.5) -> dict:
    status, begin = client.post(f"/api/projects/{pid}/takes", {
        "mark_id": mark_id,
        "device_started_at": device_started_at,
        "first_frame_device_offset_ms": offset_ms,
        "client_clock_ms": 123456,
    })
    assert status == 200, begin
    tid = begin["take_id"]
    status, up = client.call("POST", f"/api/projects/{pid}/takes/{tid}/upload",
                             {"wav": wav_b64(candidate), "complete": True})
    assert status == 200, up
    assert up["state"] == "complete" and up["renderable"] is True
    return {"take_id": tid, "upload": up}


def make_candidate(client: Client, pid: str, mark: dict, candidate: A.Clip,
                   trim: tuple[int, int] | None = None) -> dict:
    t = complete_take(client, pid, mark["id"], candidate)
    frames = t["upload"]["frames"]
    ts, te = trim or (0, frames)
    status, cand = client.post(f"/api/projects/{pid}/candidates", {
        "take_id": t["take_id"], "mark_id": mark["id"],
        "trim_start": ts, "trim_end": te})
    assert status == 200, cand
    return cand


def q(s: str) -> str:
    return quote(s, safe="")
