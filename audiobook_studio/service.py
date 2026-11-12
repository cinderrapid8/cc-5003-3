"""Audiobook re-recording workbench HTTP service (stdlib only).

Timing model
------------
The browser reports a device-side capture clock (`device_started_at`,
`first_frame_device_offset_ms`). It is stored verbatim and *separately* from
the chapter position of the mark. The server derives take duration only from
the actual number of decoded PCM frames. Leading silence is never treated as
an implicit alignment signal: trims are explicit editor choices.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import audio as A
from .render import AdoptedMark, RenderError, render_chapter
from .storage import Store, dumps, now_ms


class StudioService:
    def __init__(self, data_dir: str):
        self.store = Store(data_dir)
        self.data_dir = data_dir
        self.web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
        # render tokens: token -> snapshot of selection ids at prepare time
        self._render_tokens: dict[str, dict] = {}
        self._tokens_lock = threading.Lock()

    # ------------------------------------------------------------- projects
    def create_project(self, body: dict) -> dict:
        name = body["name"]
        clip = A.read_wav(_decode_upload(body["chapter_wav"]))
        blob_id = self.store.put_blob(A.write_wav(clip))
        pid = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO projects(id,name,chapter_blob_id,sample_rate,created_at) VALUES (?,?,?,?,?)",
            (pid, name, blob_id, clip.sample_rate, time.time()),
        )
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict:
        p = self.store.row_to_dict(
            self.store.query_one("SELECT * FROM projects WHERE id=?", (pid,)))
        if not p:
            raise ApiError(404, "PROJECT_NOT_FOUND")
        blob = self.store.row_to_dict(
            self.store.query_one("SELECT sha256, byte_size FROM blobs WHERE id=?",
                                 (p["chapter_blob_id"],)))
        clip = A.read_wav(self.store.read_blob(p["chapter_blob_id"]))
        p["chapter"] = {"blob_id": p.pop("chapter_blob_id"), "frames": clip.frames,
                        "sample_rate": clip.sample_rate,
                        "duration_ms": clip.duration_ms, **blob}
        p["marks"] = [self._mark_view(r) for r in self.store.query_all(
            "SELECT * FROM marks WHERE project_id=? ORDER BY range_start", (pid,))]
        p["noise_excerpts"] = [dict(r) for r in self.store.query_all(
            "SELECT * FROM noise_excerpts WHERE project_id=? ORDER BY created_at", (pid,))]
        p["versions"] = [dict(r) for r in self.store.query_all(
            "SELECT id,label,frame_count,cue_json,selection_json,render_config_id,created_at "
            "FROM versions WHERE project_id=? ORDER BY created_at", (pid,))]
        return p

    # ---------------------------------------------------------------- marks
    def create_mark(self, pid: str, body: dict) -> dict:
        p = self._require_project(pid)
        start, end = int(body["range_start"]), int(body["range_end"])
        if not (0 <= start < end):
            raise ApiError(400, "BAD_RANGE")
        clip = A.read_wav(self.store.read_blob(p["chapter_blob_id"]))
        if end > clip.frames:
            raise ApiError(400, "RANGE_OUT_OF_BOUNDS")
        mid = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO marks(id,project_id,sentence_label,range_start,range_end,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (mid, pid, str(body.get("sentence_label") or "未命名句子"), start, end, time.time()))
        return self._mark_view(self.store.query_one("SELECT * FROM marks WHERE id=?", (mid,)))

    def _mark_view(self, row) -> dict:
        d = dict(row)
        sel = self.store.query_one("SELECT * FROM selections WHERE mark_id=?", (d["id"],))
        d["selection"] = dict(sel) if sel else None
        d["candidates"] = [dict(c) for c in self.store.query_all(
            "SELECT * FROM candidates WHERE mark_id=? AND deleted=0 ORDER BY created_at",
            (d["id"],))]
        d["takes"] = [dict(t) for t in self.store.query_all(
            "SELECT id,state,frame_count,device_started_at,first_frame_device_offset_ms,"
            "finalize_error,created_at,finalized_at FROM takes WHERE mark_id=? ORDER BY created_at",
            (d["id"],))]
        return d

    # ---------------------------------------------------------------- takes
    def begin_take(self, pid: str, body: dict) -> dict:
        self._require_project(pid)
        mark_id = body.get("mark_id")
        if mark_id:
            self._require_mark(mark_id, pid)
        tid = str(uuid.uuid4())
        # Device clock stored verbatim; never used to derive frame timing.
        self.store.execute(
            "INSERT INTO takes(id,project_id,mark_id,state,device_started_at,"
            "client_clock_ms,first_frame_device_offset_ms,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (tid, pid, mark_id, "recording",
             body.get("device_started_at"), body.get("client_clock_ms"),
             body.get("first_frame_device_offset_ms"), time.time()))
        return {"take_id": tid, "state": "recording"}

    def upload_take(self, tid: str, body: dict) -> dict:
        take = self._require_take(tid)
        if take["state"] != "recording":
            raise ApiError(409, "TAKE_NOT_OPEN", {"state": take["state"]})
        complete = bool(body.get("complete", True))
        raw = _decode_upload(body["wav"])
        try:
            clip = A.read_wav(raw)
        except ValueError as exc:
            self._finalize_state(tid, "interrupted", error=f"undecodable upload: {exc}",
                                 byte_size=len(raw))
            raise ApiError(422, "TAKE_CORRUPT", {"error": str(exc)})
        blob_id = self.store.put_blob(A.write_wav(clip))
        if not complete:
            # Interrupted capture: bytes are preserved for audit, but the take
            # can never enter a render.
            self.store.execute(
                "UPDATE takes SET state='interrupted', blob_id=?, sample_rate=?, frame_count=?,"
                " uploaded_byte_size=?, finalize_error=?, finalized_at=? WHERE id=?",
                (blob_id, clip.sample_rate, clip.frames, len(raw),
                 body.get("reason") or "client reported incomplete capture", now_ms(), tid))
            return {"take_id": tid, "state": "interrupted", "frames": clip.frames,
                    "renderable": False}
        self._finalize_state(tid, "complete", blob_id=blob_id, clip=clip, byte_size=len(raw))
        return {"take_id": tid, "state": "complete", "frames": clip.frames,
                "duration_ms": clip.duration_ms, "blob_id": blob_id, "renderable": True}

    def _finalize_state(self, tid, state, blob_id=None, clip=None, error=None, byte_size=None):
        if state == "complete":
            self.store.execute(
                "UPDATE takes SET state='complete', blob_id=?, sample_rate=?, frame_count=?,"
                "uploaded_byte_size=?, finalized_at=? WHERE id=?",
                (blob_id, clip.sample_rate, clip.frames, byte_size, now_ms(), tid))
        else:
            self.store.execute(
                "UPDATE takes SET state='interrupted', blob_id=COALESCE(?,blob_id),"
                "frame_count=COALESCE(?,frame_count), uploaded_byte_size=?,"
                "finalize_error=?, finalized_at=? WHERE id=?",
                (blob_id, clip.frames if clip else None, byte_size, error, now_ms(), tid))

    def abandon_take(self, tid: str) -> dict:
        take = self._require_take(tid)
        if take["state"] not in ("recording", "interrupted"):
            raise ApiError(409, "TAKE_NOT_ABANDONABLE", {"state": take["state"]})
        self.store.execute("UPDATE takes SET state='abandoned', finalized_at=? WHERE id=?",
                           (now_ms(), tid))
        return {"take_id": tid, "state": "abandoned"}

    # ------------------------------------------------------------ candidates
    def create_candidate(self, pid: str, body: dict) -> dict:
        take = self._require_take(body["take_id"])
        if take["project_id"] != pid:
            raise ApiError(404, "TAKE_NOT_IN_PROJECT")
        if take["state"] != "complete":
            # Only finished takes may become render candidates.
            raise ApiError(409, "TAKE_NOT_COMPLETE", {"state": take["state"]})
        mark = self._require_mark(body["mark_id"], pid)
        clip = A.read_wav(self.store.read_blob(take["blob_id"]))
        ts, te = int(body["trim_start"]), int(body["trim_end"])
        if not (0 <= ts < te <= clip.frames):
            raise ApiError(400, "BAD_TRIM", {"take_frames": clip.frames})
        cid = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO candidates(id,take_id,mark_id,trim_start,trim_end,frame_count,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, take["id"], mark["id"], ts, te, te - ts, time.time()))
        return dict(self.store.query_one("SELECT * FROM candidates WHERE id=?", (cid,)))

    def delete_candidate(self, cid: str) -> dict:
        row = self.store.query_one("SELECT * FROM candidates WHERE id=?", (cid,))
        if not row:
            raise ApiError(404, "CANDIDATE_NOT_FOUND")
        sel = self.store.query_one("SELECT * FROM selections WHERE candidate_id=?", (cid,))
        if sel:
            raise ApiError(409, "CANDIDATE_IN_USE")
        # Soft delete: take bytes and version-rendered bytes stay on disk and
        # remain reachable via takes / versions. GC only drops zero-ref blobs.
        self.store.execute("UPDATE candidates SET deleted=1 WHERE id=?", (cid,))
        return {"deleted": cid}

    # ----------------------------------------------------------------- noise
    def add_noise(self, pid: str, body: dict) -> dict:
        p = self._require_project(pid)
        clip = A.read_wav(_decode_upload(body["wav"]))
        if clip.sample_rate != p["sample_rate"]:
            clip = A.resample_linear(clip, p["sample_rate"])
        profile = A.noise_profile(clip)
        nid = str(uuid.uuid4())
        blob_id = self.store.put_blob(A.write_wav(clip))
        self.store.execute(
            "INSERT INTO noise_excerpts(id,project_id,blob_id,frame_count,rms,peak,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (nid, pid, blob_id, clip.frames, profile["rms"], profile["peak"], time.time()))
        return {"id": nid, **profile, "sufficient": profile["duration_ms"] >= 200
                and profile["rms"] > 0.5}

    # ------------------------------------------------------------- selection
    def set_selection(self, pid: str, body: dict) -> dict:
        mark = self._require_mark(body["mark_id"], pid)
        cand = self.store.query_one(
            "SELECT * FROM candidates WHERE id=? AND deleted=0", (body["candidate_id"],))
        if not cand or cand["mark_id"] != mark["id"]:
            raise ApiError(404, "CANDIDATE_NOT_FOUND")
        mode = body.get("mode", "shift")
        if mode not in ("shift", "keep_duration"):
            raise ApiError(400, "BAD_MODE")
        fade_ms = float(body.get("fade_ms", 8.0))
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO selections(mark_id,candidate_id,mode,fade_ms,updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(mark_id) DO UPDATE SET"
                " candidate_id=excluded.candidate_id, mode=excluded.mode,"
                " fade_ms=excluded.fade_ms, updated_at=excluded.updated_at",
                (mark["id"], cand["id"], mode, fade_ms, time.time()))
        # Tokens are kept; commit_render compares its pinned snapshot against
        # the current selections and rejects a stale export with 409.
        return self._mark_view(self.store.query_one("SELECT * FROM marks WHERE id=?",
                                                     (mark["id"],)))

    # --------------------------------------------------------------- renders
    def prepare_render(self, pid: str, body: dict) -> dict:
        project = self._require_project(pid)
        snapshot = self._selection_snapshot(pid)
        if not snapshot:
            raise ApiError(400, "NO_SELECTIONS")
        token = uuid.uuid4().hex
        noise_id = body.get("noise_id")
        config_id = str(uuid.uuid4())
        self.store.execute(
            "INSERT INTO render_configs(id,project_id,mode,fade_ms,noise_id,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (config_id, pid, body.get("default_mode", "shift"),
             float(body.get("fade_ms", 8.0)), noise_id, time.time()))
        with self._tokens_lock:
            self._render_tokens[token] = {
                "project_id": pid, "snapshot": snapshot, "noise_id": noise_id,
                "config_id": config_id, "chapter_blob": project["chapter_blob_id"],
                "created": time.time()}
        return {"render_token": token, "snapshot": snapshot, "config_id": config_id}

    def commit_render(self, token: str, body: dict) -> dict:
        with self._tokens_lock:
            spec = self._render_tokens.get(token)
        if not spec:
            raise ApiError(404, "RENDER_TOKEN_UNKNOWN")
        pid = spec["project_id"]
        current = self._selection_snapshot(pid)
        if current != spec["snapshot"]:
            # Editor changed a choice while export was in flight.
            with self._tokens_lock:
                self._render_tokens.pop(token, None)
            raise ApiError(409, "SELECTION_CHANGED_DURING_EXPORT",
                           {"prepared": spec["snapshot"], "current": current})
        project = self._require_project(pid)
        if project["chapter_blob_id"] != spec["chapter_blob"]:
            raise ApiError(409, "CHAPTER_CHANGED_DURING_EXPORT")
        chapter = A.read_wav(self.store.read_blob(project["chapter_blob_id"]))
        adoptions = []
        for item in spec["snapshot"]:
            cand = self.store.query_one("SELECT * FROM candidates WHERE id=?",
                                        (item["candidate_id"],))
            take = self.store.query_one("SELECT * FROM takes WHERE id=?", (cand["take_id"],))
            if take["state"] != "complete":
                raise ApiError(409, "TAKE_NOT_COMPLETE", {"take_id": take["id"]})
            take_clip = A.read_wav(self.store.read_blob(take["blob_id"]))
            if take_clip.sample_rate != chapter.sample_rate:
                take_clip = A.resample_linear(take_clip, chapter.sample_rate)
            sub = A.slice_clip(take_clip, cand["trim_start"], cand["trim_end"])
            mark = self.store.query_one("SELECT * FROM marks WHERE id=?", (item["mark_id"],))
            adoptions.append(AdoptedMark(
                mark_id=mark["id"], label=mark["sentence_label"],
                range_start=mark["range_start"], range_end=mark["range_end"],
                candidate_clip=sub, mode=item["mode"], fade_ms=item["fade_ms"]))
        try:
            result = render_chapter(self.store, pid, chapter, adoptions, spec["noise_id"])
        except RenderError as exc:
            raise ApiError(422, exc.code, exc.details) from exc
        wav_bytes = A.write_wav_with_cues(result.clip, result.cues)
        blob_id = self.store.put_blob(wav_bytes)
        vid = str(uuid.uuid4())
        label = str(body.get("label") or f"version-{time.strftime('%Y%m%d-%H%M%S')}")
        self.store.execute(
            "INSERT INTO versions(id,project_id,label,blob_id,frame_count,cue_json,"
            "selection_json,render_config_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, pid, label, blob_id, result.clip.frames, dumps(result.cues),
             dumps(spec["snapshot"]), spec["config_id"], time.time()))
        with self._tokens_lock:
            self._render_tokens.pop(token, None)
        return {"version_id": vid, "blob_id": blob_id, "frames": result.clip.frames,
                "original_frames": result.original_frames,
                "delta_frames": result.clip.frames - result.original_frames,
                "cues": result.cues, "events": result.events}

    def render_wav(self, version_id: str) -> tuple[bytes, str]:
        row = self.store.query_one("SELECT * FROM versions WHERE id=?", (version_id,))
        if not row:
            raise ApiError(404, "VERSION_NOT_FOUND")
        return self.store.read_blob(row["blob_id"]), f"{row['label']}.wav"

    def take_wav(self, take_id: str) -> tuple[bytes, str]:
        row = self.store.query_one("SELECT * FROM takes WHERE id=?", (take_id,))
        if not row:
            raise ApiError(404, "TAKE_NOT_FOUND")
        if not row["blob_id"]:
            raise ApiError(409, "TAKE_HAS_NO_AUDIO", {"state": row["state"]})
        return self.store.read_blob(row["blob_id"]), f"take-{take_id[:8]}.wav"

    def gc(self) -> dict:
        return self.store.gc_blobs()

    # ------------------------------------------------------------- helpers
    def _selection_snapshot(self, pid: str) -> list[dict]:
        rows = self.store.query_all(
            "SELECT s.* FROM selections s JOIN marks m ON m.id=s.mark_id "
            "WHERE m.project_id=? ORDER BY m.range_start", (pid,))
        return [{"mark_id": r["mark_id"], "candidate_id": r["candidate_id"],
                 "mode": r["mode"], "fade_ms": r["fade_ms"]} for r in rows]

    def _require_project(self, pid: str):
        p = self.store.query_one("SELECT * FROM projects WHERE id=?", (pid,))
        if not p:
            raise ApiError(404, "PROJECT_NOT_FOUND")
        return p

    def _require_mark(self, mid: str, pid: str | None = None):
        r = self.store.query_one("SELECT * FROM marks WHERE id=?", (mid,))
        if not r or (pid is not None and r["project_id"] != pid):
            raise ApiError(404, "MARK_NOT_FOUND")
        return r

    def _require_take(self, tid: str):
        r = self.store.query_one("SELECT * FROM takes WHERE id=?", (tid,))
        if not r:
            raise ApiError(404, "TAKE_NOT_FOUND")
        return r


class ApiError(Exception):
    def __init__(self, status: int, code: str, details: dict | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.details = details or {}


def _decode_upload(item) -> bytes:
    if isinstance(item, str) and item.startswith("base64:"):
        import base64

        return base64.b64decode(item[7:])
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    raise ApiError(400, "BAD_UPLOAD")


# --------------------------------------------------------------------- HTTP
class _Handler(BaseHTTPRequestHandler):
    service: StudioService = None

    def log_message(self, fmt, *args):  # quieter tests
        pass

    def _send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(self, data: bytes, ctype: str, name: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            parts = path.strip("/").split("/")
            svc = self.service
            if path == "/api/health":
                return self._send_json({"ok": True})
            if path.startswith("/api/projects/"):
                pid = parts[2]
                if len(parts) == 3:
                    return self._send_json(svc.get_project(pid))
                if len(parts) == 5 and parts[3] == "versions":
                    data, name = svc.render_wav(parts[4])
                    return self._send_bytes(data, "audio/wav", name)
                if len(parts) == 5 and parts[3] == "blobs":
                    return self._send_bytes(svc.store.read_blob(parts[4]), "audio/wav",
                                            f"{parts[4]}.wav")
            if parts[:2] == ["api", "takes"] and len(parts) == 4 and parts[3] == "wav":
                data, name = svc.take_wav(parts[2])
                return self._send_bytes(data, "audio/wav", name)
            return self._serve_static(path)
        except ApiError as exc:
            self._send_json({"error": exc.code, "details": exc.details}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": "INTERNAL", "details": str(exc)}, 500)

    def do_POST(self):
        try:
            body = self._read_json()
            path = urlparse(self.path).path
            svc = self.service
            routes = {
                "/api/projects": lambda: svc.create_project(body),
                "/api/gc": lambda: svc.gc(),
            }
            if path in routes:
                return self._send_json(routes[path]())
            parts = path.strip("/").split("/")
            # /api/projects/<pid>/...
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "projects":
                pid = parts[2]
                sub = parts[3:]
                if sub == ["marks"]:
                    return self._send_json(svc.create_mark(pid, body))
                if sub == ["takes"]:
                    return self._send_json(svc.begin_take(pid, body))
                if sub == ["noise"]:
                    return self._send_json(svc.add_noise(pid, body))
                if sub == ["selection"]:
                    return self._send_json(svc.set_selection(pid, body))
                if sub == ["render", "prepare"]:
                    return self._send_json(svc.prepare_render(pid, body))
                if len(sub) == 3 and sub[0] == "takes" and sub[2] == "upload":
                    return self._send_json(svc.upload_take(sub[1], body))
                if len(sub) == 3 and sub[0] == "takes" and sub[2] == "abandon":
                    return self._send_json(svc.abandon_take(sub[1]))
                if sub == ["candidates"]:
                    return self._send_json(svc.create_candidate(pid, body))
            if parts[:2] == ["api", "candidates"] and len(parts) == 4 and parts[3] == "delete":
                return self._send_json(svc.delete_candidate(parts[2]))
            if parts[:2] == ["api", "renders"] and len(parts) == 3 and parts[2] == "commit":
                return self._send_json(svc.commit_render(query_token(self.path), body))
            raise ApiError(404, "NOT_FOUND")
        except ApiError as exc:
            self._send_json({"error": exc.code, "details": exc.details}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": "INTERNAL", "details": str(exc)}, 500)

    def _serve_static(self, path: str):
        if path == "/":
            path = "/index.html"
        target = os.path.normpath(os.path.join(self.service.web_dir, path.lstrip("/")))
        if not target.startswith(self.service.web_dir):
            raise ApiError(403, "FORBIDDEN")
        if not os.path.isfile(target):
            raise ApiError(404, "NOT_FOUND")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css"}.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def query_token(path: str) -> str:
    q = parse_qs(urlparse(path).query)
    if "token" not in q:
        raise ApiError(400, "TOKEN_REQUIRED")
    return q["token"][0]


def build_server(data_dir: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    service = StudioService(data_dir)
    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service
    return httpd
