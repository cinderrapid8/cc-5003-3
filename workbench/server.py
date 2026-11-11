"""Stdlib HTTP service for the pickup re-recording workbench."""

import base64
import json
import mimetypes
import os
import re
import threading
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audio, render
from .store import StoreError

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, static_dir):
        super().__init__(address, Handler)
        self.store = store
        self.static_dir = static_dir


class Handler(BaseHTTPRequestHandler):
    server_version = "PickupWorkbench/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("WORKBENCH_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ plumbing
    def _send(self, status, body, ctype="application/json", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise StoreError("请求体不是合法 JSON: %s" % e, 400)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if method == "GET" and path == "/":
            return self._static("index.html")
        if method == "GET" and path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        for m, pat, fn in ROUTES:
            if m != method:
                continue
            match = pat.match(path)
            if not match:
                continue
            try:
                body = {}
                raw = b""
                if method in ("POST", "DELETE"):
                    if self._is_json_request():
                        body = self._json_body()
                    else:
                        raw = self._body()
                return fn(self, match.groups(), query, body, raw)
            except StoreError as e:
                return self._json({"error": str(e)}, e.status)
            except (audio.EditError, render.RenderError) as e:
                return self._json({"error": str(e)}, 409)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except (wave.Error, EOFError) as e:
                return self._json({"error": "无法解析 WAV: %s" % e}, 400)
        return self._json({"error": "未知接口: %s %s" % (method, path)}, 404)

    def _is_json_request(self):
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        return ct == "application/json"

    def _static(self, name):
        name = name.replace("\\", "/")
        if ".." in name.split("/") or name.startswith("/"):
            return self._json({"error": "非法路径"}, 400)
        full = os.path.join(self.server.static_dir, name)
        if not os.path.isfile(full):
            return self._json({"error": "文件不存在"}, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


# ------------------------------------------------------------------ helpers
def _store(handler):
    return handler.server.store


def _chapter_detail(store, chapter_id):
    ch = store.get_chapter(chapter_id)
    versions = store.list_versions(chapter_id)
    for v in versions:
        take = store.get_recording(v["take_id"])
        sel = store.get_selection(v["selection_id"])
        v["take"] = {
            "id": take["id"], "frames": take["frames"], "status": take["status"],
            "sample_rate": take["sample_rate"], "pcm_sha256": take["pcm_sha256"],
            "device_start_utc": take["device_start_utc"],
        }
        v["selection"] = sel
    selections = store.list_selections(chapter_id)
    candidates = []
    for take in store.list_recordings(
            project_id=ch["project_id"], kind="take", chapter_id=chapter_id):
        t = dict(take)
        t["received_frames"] = store.take_frames_received(take["id"]) \
            if take["status"] in ("recording", "incomplete") else take["frames"]
        t["integrity_ok"] = (store.verify_recording(take["id"])
                             if take["status"] == "complete" else False)
        candidates.append(t)
    proj = store.get_project(ch["project_id"])
    rt = None
    if proj["roomtone_recording_id"]:
        r = store.get_recording(proj["roomtone_recording_id"])
        rt = {"id": r["id"], "frames": r["frames"], "sample_rate": r["sample_rate"],
              "status": r["status"], "pcm_sha256": r["pcm_sha256"]}
    return {
        "chapter": ch,
        "rate": store.chapter_sample_rate(chapter_id),
        "timeline_frames": store.timeline_frames(chapter_id, ch["current_version_id"]),
        "versions": versions,
        "selections": selections,
        "candidates": candidates,
        "roomtone": rt,
        "can_undo": ch["current_version_id"] is not None,
        "render_jobs": store.list_render_jobs(chapter_id),
    }


# ------------------------------------------------------------------ projects
@route("POST", r"/api/projects")
def create_project(h, g, q, body, raw):
    name = (body.get("name") or "").strip()
    if not name:
        raise StoreError("name 不能为空", 400)
    return _json_project(h, _store(h).create_project(name))


def _json_project(h, project):
    store = _store(h)
    chapters = store.list_chapters(project["id"])
    roomtone = None
    if project["roomtone_recording_id"]:
        r = store.get_recording(project["roomtone_recording_id"])
        roomtone = {"id": r["id"], "frames": r["frames"], "status": r["status"],
                    "sample_rate": r["sample_rate"]}
    return h._json({"project": project, "chapters": chapters, "roomtone": roomtone})


@route("GET", r"/api/projects")
def list_projects(h, g, q, body, raw):
    store = _store(h)
    return h._json({"projects": store.list_projects()})


@route("GET", r"/api/projects/(\d+)")
def get_project(h, g, q, body, raw):
    return _json_project(h, _store(h).get_project(int(g[0])))


@route("POST", r"/api/projects/(\d+)/roomtone")
def upload_roomtone(h, g, q, body, raw):
    store = _store(h)
    pid = int(g[0])
    samples, rate = audio.read_wav_bytes(raw)
    rec = store.import_recording(pid, "roomtone", samples, rate)
    store.set_roomtone(pid, rec["id"])
    return h._json({"roomtone": {"id": rec["id"], "frames": rec["frames"],
                                 "sample_rate": rate}})


@route("POST", r"/api/projects/(\d+)/demo")
def demo_chapter(h, g, q, body, raw):
    store = _store(h)
    pid = int(g[0])
    rate = int(body.get("rate") or 8000)
    sentences = int(body.get("sentences") or 8)
    chapter_pcm, manifest = audio.synth_chapter(rate=rate, n_sentences=sentences)
    src = store.import_recording(pid, "source", chapter_pcm, rate)
    proj = store.get_project(pid)
    if not proj["roomtone_recording_id"]:
        rt = audio.synth_roomtone(rate, 6.0)
        rt_rec = store.import_recording(pid, "roomtone", rt, rate)
        store.set_roomtone(pid, rt_rec["id"])
    title = "演示章节 %d" % (len(store.list_chapters(pid)) + 1)
    ch = store.create_chapter(pid, title, src["id"])
    return h._json({"chapter": ch, "manifest": manifest, "rate": rate})


@route("POST", r"/api/projects/(\d+)/chapters")
def upload_chapter(h, g, q, body, raw):
    store = _store(h)
    pid = int(g[0])
    title = (q.get("title") or [""])[0] or "未命名章节"
    samples, rate = audio.read_wav_bytes(raw)
    src = store.import_recording(pid, "source", samples, rate)
    ch = store.create_chapter(pid, title, src["id"])
    return h._json({"chapter": ch})


# ------------------------------------------------------------------ chapters
@route("GET", r"/api/chapters/(\d+)")
def get_chapter(h, g, q, body, raw):
    return h._json(_chapter_detail(_store(h), int(g[0])))


@route("POST", r"/api/chapters/(\d+)/selections")
def create_selection(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    ch = store.get_chapter(cid)
    version_id = body.get("version_id", ch["current_version_id"])
    start = int(body.get("start_frame", 0))
    end = int(body.get("end_frame", 0))
    sel = store.create_selection(cid, version_id, start, end,
                                 body.get("label") or "选段")
    return h._json({"selection": sel})


@route("GET", r"/api/chapters/(\d+)/peaks")
def peaks(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    ch = store.get_chapter(cid)
    vid = q.get("version_id", [ch["current_version_id"]])[0]
    vid = int(vid) if vid not in (None, "", "null", "None") else None
    buckets = int(q.get("buckets", ["800"])[0])
    return h._json(render.peaks_for_version(store, cid, vid, buckets))


@route("GET", r"/api/chapters/(\d+)/audio")
def chapter_audio(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    ch = store.get_chapter(cid)
    vid = q.get("version_id", [ch["current_version_id"]])[0]
    vid = int(vid) if vid not in (None, "", "null", "None") else None
    pcm, _ = render.render_version_pcm(store, cid, vid)
    data = render.wav_bytes(pcm, store.chapter_sample_rate(cid))
    return h._send(200, data, "audio/wav")


@route("POST", r"/api/chapters/(\d+)/preview")
def preview(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    wav, report = render.build_preview(
        store, cid, int(body["selection_id"]),
        body.get("take_id"),
        body.get("mode") or "insert_shift",
        int(body.get("fade_in_frames") or 0),
        int(body.get("fade_out_frames") or 0),
        int(body.get("trim_start_frames") or 0),
        int(body.get("trim_end_frames") or 0),
        int(body.get("context_frames") or 0))
    return h._json({"wav_base64": base64.b64encode(wav).decode("ascii"),
                    "report": report})


@route("POST", r"/api/chapters/(\d+)/adopt")
def adopt(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    parent = body.get("base_version_id", "__current__")
    if parent == "__current__":
        parent = store.get_chapter(cid)["current_version_id"]
    version = store.create_version(
        cid, int(body["selection_id"]), int(body["take_id"]),
        body.get("mode") or "insert_shift",
        int(body.get("fade_in_frames") or 0),
        int(body.get("fade_out_frames") or 0),
        int(body.get("trim_start_frames") or 0),
        int(body.get("trim_end_frames") or 0),
        parent_id=(int(parent) if parent is not None else None))
    return h._json({"version": version})


@route("POST", r"/api/chapters/(\d+)/undo")
def undo(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    return h._json({"chapter": store.undo(cid),
                    "detail": _chapter_detail(store, cid)})


@route("POST", r"/api/chapters/(\d+)/redo")
def redo(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    return h._json({"chapter": store.redo(cid),
                    "detail": _chapter_detail(store, cid)})


@route("POST", r"/api/chapters/(\d+)/render")
def render_chapter(h, g, q, body, raw):
    store = _store(h)
    cid = int(g[0])
    ch = store.get_chapter(cid)
    vid = body.get("version_id", ch["current_version_id"])
    delay_ms = int(body.get("delay_ms") or 0)
    job = store.create_render_job(cid, vid)
    if body.get("async", True):
        t = threading.Thread(target=render.render_job,
                             args=(store, job["id"], delay_ms), daemon=True)
        t.start()
        return h._json({"job": store.get_render_job(job["id"])})
    done = render.render_job(store, job["id"], delay_ms)
    return h._json({"job": done})


@route("GET", r"/api/chapters/(\d+)/jobs")
def list_jobs(h, g, q, body, raw):
    return h._json({"jobs": _store(h).list_render_jobs(int(g[0]))})


@route("GET", r"/api/render-jobs/(\d+)")
def get_job(h, g, q, body, raw):
    return h._json({"job": _store(h).get_render_job(int(g[0]))})


@route("GET", r"/api/render-jobs/(\d+)/wav")
def get_job_wav(h, g, q, body, raw):
    store = _store(h)
    job = store.get_render_job(int(g[0]))
    if not job["output_path"]:
        raise StoreError("渲染尚未产出音频", 404)
    full = os.path.join(store.data_dir, job["output_path"])
    if not os.path.exists(full):
        raise StoreError("渲染音频不存在", 404)
    with open(full, "rb") as f:
        return h._send(200, f.read(), "audio/wav")


# ------------------------------------------------------------------ takes
@route("POST", r"/api/takes")
def create_take(h, g, q, body, raw):
    store = _store(h)
    take = store.create_take(int(body["project_id"]), int(body["chapter_id"]),
                             int(body["sample_rate"]))
    return h._json({"take": take})


@route("POST", r"/api/takes/(\d+)/chunks")
def take_chunk(h, g, q, body, raw):
    store = _store(h)
    tid = int(g[0])
    seq = int((q.get("seq") or ["0"])[0])
    frames = store.append_take_chunk(tid, seq, raw)
    return h._json({"take_id": tid, "frames": frames})


@route("POST", r"/api/takes/(\d+)/finalize")
def take_finalize(h, g, q, body, raw):
    store = _store(h)
    rec = store.finalize_take(int(g[0]), int(body["frames"]),
                              body["sha256"], body.get("device_start_utc"))
    return h._json({"take": rec})


@route("POST", r"/api/takes/(\d+)/abort")
def take_abort(h, g, q, body, raw):
    store = _store(h)
    return h._json({"take": store.abort_take(int(g[0]))})


# ------------------------------------------------------------------ recordings
@route("GET", r"/api/recordings/(\d+)/wav")
def recording_wav(h, g, q, body, raw):
    store = _store(h)
    rec = store.get_recording(int(g[0]))
    if not rec["path"]:
        raise StoreError("录音已删除或没有可用字节", 404)
    full = os.path.join(store.data_dir, rec["path"])
    if not os.path.exists(full):
        raise StoreError("录音字节不存在", 404)
    with open(full, "rb") as f:
        return h._send(200, f.read(), "audio/wav")


@route("DELETE", r"/api/recordings/(\d+)")
def recording_delete(h, g, q, body, raw):
    store = _store(h)
    return h._json(store.delete_recording(int(g[0])))


# ------------------------------------------------------------------ versions
@route("GET", r"/api/versions/(\d+)")
def get_version(h, g, q, body, raw):
    store = _store(h)
    v = store.get_version(int(g[0]))
    v["take"] = store.get_recording(v["take_id"])
    v["selection"] = store.get_selection(v["selection_id"])
    return h._json({"version": v,
                    "chain": store.version_chain(v["chapter_id"], v["id"])})


@route("POST", r"/api/versions/(\d+)/switch")
def switch_version(h, g, q, body, raw):
    store = _store(h)
    v = store.switch_version(
        int(g[0]), int(body["take_id"]),
        body.get("mode"),
        body.get("fade_in_frames"),
        body.get("fade_out_frames"),
        body.get("trim_start_frames"),
        body.get("trim_end_frames"))
    return h._json({"version": v})
