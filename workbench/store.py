"""SQLite-backed persistence for the pickup workbench.

Entities: projects, recordings (source / take / roomtone), chapters,
selections (candidate ranges on a specific version timeline), versions
(adoption relations forming an undoable chain), render jobs.

Timing rule enforced here: a take's duration is the number of PCM frames
actually received by the server; the device-reported start timestamp is
stored alongside but never feeds alignment math.
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  roomtone_recording_id INTEGER,
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  chapter_id INTEGER,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  sample_rate INTEGER NOT NULL,
  frames INTEGER NOT NULL DEFAULT 0,
  pcm_sha256 TEXT,
  device_start_utc TEXT,
  server_first_chunk_utc TEXT,
  path TEXT,
  deleted_utc TEXT,
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  source_recording_id INTEGER NOT NULL,
  current_version_id INTEGER,
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selections(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id INTEGER NOT NULL,
  version_id INTEGER,
  start_frame INTEGER NOT NULL,
  end_frame INTEGER NOT NULL,
  label TEXT,
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id INTEGER NOT NULL,
  parent_id INTEGER,
  seq INTEGER NOT NULL,
  selection_id INTEGER NOT NULL,
  take_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  fade_in_frames INTEGER NOT NULL,
  fade_out_frames INTEGER NOT NULL,
  trim_start_frames INTEGER NOT NULL DEFAULT 0,
  trim_end_frames INTEGER NOT NULL DEFAULT 0,
  created_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chapter_id INTEGER NOT NULL,
  version_id INTEGER,
  status TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0,
  output_path TEXT,
  report_json TEXT,
  error TEXT,
  created_utc TEXT NOT NULL,
  finished_utc TEXT
);
"""

_CURRENT = object()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class StoreError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        for sub in ("recordings", "renders", "cache"):
            os.makedirs(os.path.join(self.data_dir, sub), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            os.path.join(self.data_dir, "workbench.db"), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._reconcile_interrupted_takes()

    def close(self):
        with self._lock:
            self._conn.close()

    def _reconcile_interrupted_takes(self):
        # A take left in 'recording' means the client vanished mid-capture;
        # it must never be adoptable, so freeze it as 'incomplete'.
        self._conn.execute(
            "UPDATE recordings SET status='incomplete' WHERE kind='take' AND status='recording'")
        self._conn.commit()

    # ------------------------------------------------------------ helpers
    def _one(self, sql, args=()):
        return self._conn.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self._conn.execute(sql, args).fetchall()

    def _insert(self, sql, args=()):
        cur = self._conn.execute(sql, args)
        self._conn.commit()
        return cur.lastrowid

    def _path(self, rel):
        return os.path.join(self.data_dir, rel)

    # ------------------------------------------------------------ projects
    def create_project(self, name):
        with self._lock:
            pid = self._insert(
                "INSERT INTO projects(name, created_utc) VALUES(?, ?)", (name, utcnow()))
            return self.get_project(pid)

    def get_project(self, project_id):
        row = self._one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not row:
            raise StoreError("工程不存在", 404)
        return dict(row)

    def list_projects(self):
        return [dict(r) for r in self._all(
            "SELECT * FROM projects ORDER BY id")]

    def set_roomtone(self, project_id, recording_id):
        rec = self.get_recording(recording_id)
        if rec["kind"] != "roomtone" or rec["status"] != "complete":
            raise StoreError("底噪必须是一段完整的环境录音")
        with self._lock:
            self._conn.execute(
                "UPDATE projects SET roomtone_recording_id=? WHERE id=?",
                (recording_id, project_id))
            self._conn.commit()
        return self.get_project(project_id)

    # ------------------------------------------------------------ recordings
    def import_recording(self, project_id, kind, samples, sample_rate,
                         chapter_id=None, pcm_sha256=None, frames=None):
        from . import audio
        with self._lock:
            rid = self._insert(
                "INSERT INTO recordings(project_id, chapter_id, kind, status, sample_rate,"
                " frames, pcm_sha256, created_utc) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, chapter_id, kind, "complete", sample_rate,
                 frames if frames is not None else len(samples),
                 pcm_sha256 or audio.pcm_sha256(samples), utcnow()))
            rel = os.path.join("recordings", "%d.wav" % rid)
            audio.write_wav(self._path(rel), samples, sample_rate)
            self._conn.execute("UPDATE recordings SET path=? WHERE id=?", (rel, rid))
            self._conn.commit()
            return self.get_recording(rid)

    def get_recording(self, recording_id):
        row = self._one("SELECT * FROM recordings WHERE id=?", (recording_id,))
        if not row:
            raise StoreError("录音不存在", 404)
        return dict(row)

    def list_recordings(self, project_id=None, kind=None, chapter_id=None,
                        include_deleted=False):
        sql, args = "SELECT * FROM recordings WHERE 1=1", []
        if project_id is not None:
            sql += " AND project_id=?"; args.append(project_id)
        if kind is not None:
            sql += " AND kind=?"; args.append(kind)
        if chapter_id is not None:
            sql += " AND chapter_id=?"; args.append(chapter_id)
        if not include_deleted:
            sql += " AND deleted_utc IS NULL"
        sql += " ORDER BY id"
        return [dict(r) for r in self._all(sql, args)]

    # -- take capture (chunked; server counts frames actually received) --
    def create_take(self, project_id, chapter_id, sample_rate):
        with self._lock:
            rid = self._insert(
                "INSERT INTO recordings(project_id, chapter_id, kind, status, sample_rate,"
                " created_utc) VALUES(?,?,'take','recording',?,?)",
                (project_id, chapter_id, sample_rate, utcnow()))
            return self.get_recording(rid)

    def _part_path(self, take_id):
        return self._path(os.path.join("recordings", "%d.part" % take_id))

    def append_take_chunk(self, take_id, seq, data):
        rec = self.get_recording(take_id)
        if rec["kind"] != "take":
            raise StoreError("不是候选录次")
        if rec["status"] != "recording":
            raise StoreError("录次已结束，不能继续写入 (status=%s)" % rec["status"])
        if len(data) % 2:
            raise StoreError("PCM 块长度必须是 2 的倍数", 400)
        with self._lock:
            first = not os.path.exists(self._part_path(take_id)) or \
                os.path.getsize(self._part_path(take_id)) == 0
            with open(self._part_path(take_id), "ab") as f:
                f.write(data)
            if first:
                self._conn.execute(
                    "UPDATE recordings SET server_first_chunk_utc=? WHERE id=?",
                    (utcnow(), take_id))
                self._conn.commit()
            return os.path.getsize(self._part_path(take_id)) // 2

    def take_frames_received(self, take_id):
        p = self._part_path(take_id)
        return os.path.getsize(p) // 2 if os.path.exists(p) else 0

    def finalize_take(self, take_id, frames, pcm_sha256, device_start_utc=None):
        rec = self.get_recording(take_id)
        if rec["kind"] != "take":
            raise StoreError("不是候选录次")
        if rec["status"] not in ("recording", "incomplete"):
            raise StoreError("录次状态不允许完成化: %s" % rec["status"])
        if frames <= 0:
            raise StoreError("录次没有收到任何采样，不能完成", 400)
        with self._lock:
            if not os.path.exists(self._part_path(take_id)):
                self._mark_corrupt(take_id)
                raise StoreError("候选未写完：缺少 PCM 数据")
            with open(self._part_path(take_id), "rb") as f:
                raw = f.read()
            actual_frames = len(raw) // 2
            actual_sha = hashlib.sha256(raw).hexdigest()
            if actual_frames != frames or actual_sha != pcm_sha256:
                self._mark_corrupt(take_id)
                raise StoreError(
                    "候选未写完或被截断：声明 %d 帧/%s，实收 %d 帧/%s"
                    % (frames, pcm_sha256[:12], actual_frames, actual_sha[:12]))
            from . import audio
            samples = audio.pcm_from_bytes(raw)
            rel = os.path.join("recordings", "%d.wav" % take_id)
            tmp = self._path(rel + ".tmp")
            audio.write_wav(tmp, samples, rec["sample_rate"])
            os.replace(tmp, self._path(rel))
            os.remove(self._part_path(take_id))
            self._conn.execute(
                "UPDATE recordings SET status='complete', frames=?, pcm_sha256=?,"
                " device_start_utc=?, path=? WHERE id=?",
                (actual_frames, actual_sha, device_start_utc, rel, take_id))
            self._conn.commit()
            return self.get_recording(take_id)

    def _mark_corrupt(self, take_id):
        self._conn.execute(
            "UPDATE recordings SET status='corrupt' WHERE id=?", (take_id,))
        self._conn.commit()

    def abort_take(self, take_id):
        rec = self.get_recording(take_id)
        if rec["status"] == "recording":
            with self._lock:
                self._conn.execute(
                    "UPDATE recordings SET status='incomplete' WHERE id=?", (take_id,))
                self._conn.commit()
        return self.get_recording(take_id)

    def verify_recording(self, recording_id):
        """Re-hash bytes on disk; catches truncated / corrupted files."""
        from . import audio
        rec = self.get_recording(recording_id)
        if rec["status"] != "complete" or not rec["path"]:
            return False
        full = self._path(rec["path"])
        if not os.path.exists(full):
            return False
        try:
            samples, rate = audio.read_wav(full)
        except Exception:
            return False
        return (len(samples) == rec["frames"]
                and rate == rec["sample_rate"]
                and audio.pcm_sha256(samples) == rec["pcm_sha256"])

    def read_recording_samples(self, recording_id):
        from . import audio
        rec = self.get_recording(recording_id)
        if not rec["path"]:
            raise StoreError("录音没有可用字节 (status=%s)" % rec["status"])
        return audio.read_wav(self._path(rec["path"]))

    def take_refcount(self, take_id):
        row = self._one(
            "SELECT COUNT(*) AS c FROM versions WHERE take_id=?", (take_id,))
        return row["c"]

    def delete_recording(self, recording_id):
        """Bytes are removed only when no version/chapter/project references
        them; history keeps its audio either way."""
        rec = self.get_recording(recording_id)
        refs = 0
        if rec["kind"] == "take":
            refs += self.take_refcount(recording_id)
        if rec["kind"] == "source":
            refs += self._one(
                "SELECT COUNT(*) AS c FROM chapters WHERE source_recording_id=?",
                (recording_id,))["c"]
        if rec["kind"] == "roomtone":
            refs += self._one(
                "SELECT COUNT(*) AS c FROM projects WHERE roomtone_recording_id=?",
                (recording_id,))["c"]
        with self._lock:
            self._conn.execute(
                "UPDATE recordings SET deleted_utc=? WHERE id=?", (utcnow(), recording_id))
            self._conn.commit()
            if refs == 0:
                for rel in (rec["path"],):
                    if rel and os.path.exists(self._path(rel)):
                        os.remove(self._path(rel))
                part = self._part_path(recording_id)
                if os.path.exists(part):
                    os.remove(part)
                self._conn.execute(
                    "UPDATE recordings SET path=NULL WHERE id=?", (recording_id,))
                self._conn.commit()
                return {"deleted": True, "bytes_removed": True, "refs": 0}
            return {"deleted": True, "bytes_removed": False, "refs": refs}

    # ------------------------------------------------------------ chapters
    def create_chapter(self, project_id, title, source_recording_id):
        src = self.get_recording(source_recording_id)
        if src["kind"] != "source" or src["status"] != "complete":
            raise StoreError("章节源必须是完整录音")
        with self._lock:
            cid = self._insert(
                "INSERT INTO chapters(project_id, title, source_recording_id, created_utc)"
                " VALUES(?,?,?,?)", (project_id, title, source_recording_id, utcnow()))
            return self.get_chapter(cid)

    def get_chapter(self, chapter_id):
        row = self._one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
        if not row:
            raise StoreError("章节不存在", 404)
        return dict(row)

    def list_chapters(self, project_id):
        return [dict(r) for r in self._all(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY id", (project_id,))]

    def chapter_sample_rate(self, chapter_id):
        ch = self.get_chapter(chapter_id)
        return self.get_recording(ch["source_recording_id"])["sample_rate"]

    # ------------------------------------------------------------ selections
    def create_selection(self, chapter_id, version_id, start_frame, end_frame, label=None):
        n = self.timeline_frames(chapter_id, version_id)
        if not (0 <= start_frame < end_frame <= n):
            raise StoreError(
                "选段 [%d,%d) 超出时间线长度 %d" % (start_frame, end_frame, n), 400)
        with self._lock:
            sid = self._insert(
                "INSERT INTO selections(chapter_id, version_id, start_frame, end_frame,"
                " label, created_utc) VALUES(?,?,?,?,?,?)",
                (chapter_id, version_id, start_frame, end_frame, label, utcnow()))
            return self.get_selection(sid)

    def get_selection(self, selection_id):
        row = self._one("SELECT * FROM selections WHERE id=?", (selection_id,))
        if not row:
            raise StoreError("选段不存在", 404)
        return dict(row)

    def list_selections(self, chapter_id):
        return [dict(r) for r in self._all(
            "SELECT * FROM selections WHERE chapter_id=? ORDER BY id", (chapter_id,))]

    # ------------------------------------------------------------ versions
    def get_version(self, version_id):
        row = self._one("SELECT * FROM versions WHERE id=?", (version_id,))
        if not row:
            raise StoreError("版本不存在", 404)
        return dict(row)

    def list_versions(self, chapter_id):
        return [dict(r) for r in self._all(
            "SELECT * FROM versions WHERE chapter_id=? ORDER BY seq", (chapter_id,))]

    def version_chain(self, chapter_id, version_id):
        chain, vid = [], version_id
        while vid is not None:
            v = self.get_version(vid)
            if v["chapter_id"] != chapter_id:
                raise StoreError("版本不属于该章节")
            chain.append(v)
            vid = v["parent_id"]
        chain.reverse()
        return chain

    def timeline_frames(self, chapter_id, version_id):
        ch = self.get_chapter(chapter_id)
        n = self.get_recording(ch["source_recording_id"])["frames"]
        for v in self.version_chain(chapter_id, version_id):
            take = self.get_recording(v["take_id"])
            t_eff = take["frames"] - v["trim_start_frames"] - v["trim_end_frames"]
            sel = self.get_selection(v["selection_id"])
            if v["mode"] == "insert_shift":
                n = n - (sel["end_frame"] - sel["start_frame"]) + t_eff \
                    - v["fade_in_frames"] - v["fade_out_frames"]
        return n

    def _validate_edit(self, chapter, parent_id, selection, take, mode,
                       fade_in, fade_out, trim_start, trim_end):
        if selection["chapter_id"] != chapter["id"]:
            raise StoreError("选段不属于该章节")
        if selection["version_id"] != parent_id:
            raise StoreError(
                "选段基于版本 %s 的时间线，不能套用到版本 %s"
                % (selection["version_id"], parent_id))
        if take["kind"] != "take":
            raise StoreError("只能采用候选录次")
        if take["status"] != "complete":
            raise StoreError(
                "只有完成的录次可以进入采用与正式渲染 (当前: %s)" % take["status"])
        if take["deleted_utc"]:
            raise StoreError("候选已被删除，不能继续采用")
        if not self.verify_recording(take["id"]):
            raise StoreError("候选字节校验失败（文件缺失或被截断）")
        rate = self.chapter_sample_rate(chapter["id"])
        if take["sample_rate"] != rate:
            raise StoreError(
                "采样率不一致：候选 %d Hz，章节 %d Hz；不做重采样" % (take["sample_rate"], rate))
        t_eff = take["frames"] - trim_start - trim_end
        if t_eff <= 0:
            raise StoreError("修剪后候选有效长度为零", 400)
        n = self.timeline_frames(chapter["id"], parent_id)
        s, e = selection["start_frame"], selection["end_frame"]
        seg = e - s
        if fade_in < 0 or fade_out < 0:
            raise StoreError("淡化长度不能为负", 400)
        if mode == "insert_shift":
            if fade_in > s:
                raise StoreError("淡入长度超过选段之前的可用语境")
            if fade_out > n - e:
                raise StoreError("淡出长度超过选段之后的可用语境")
            if fade_in + fade_out > t_eff:
                raise StoreError("淡化总长度超过候选有效长度")
        elif mode == "keep_duration":
            if t_eff > seg:
                raise StoreError(
                    "候选比原选段长 %d 帧；禁止拉伸遮差，请显式修剪或改用 insert_shift"
                    % (t_eff - seg))
            fill = seg - t_eff
            if fade_in + fade_out > seg:
                raise StoreError("淡化总长度超过选段长度")
            if fill > 0:
                proj = self.get_project(chapter["project_id"])
                rt_id = proj["roomtone_recording_id"]
                if not rt_id:
                    raise StoreError("候选短于原选段，需要环境底噪填充，但工程未提供底噪录音")
                rt = self.get_recording(rt_id)
                if rt["frames"] < fill:
                    raise StoreError(
                        "环境底噪不足：填充需要 %d 帧，仅有 %d 帧；请提供更长的真实底噪"
                        % (fill, rt["frames"]))
                if fade_out > fill:
                    raise StoreError("底噪填充段不足以承载淡出淡化")
            if fade_out > t_eff:
                raise StoreError("淡出长度超过候选有效长度")
        else:
            raise StoreError("未知插入方式: %r" % mode, 400)

    def create_version(self, chapter_id, selection_id, take_id, mode,
                       fade_in_frames, fade_out_frames,
                       trim_start_frames=0, trim_end_frames=0, parent_id=_CURRENT):
        chapter = self.get_chapter(chapter_id)
        if parent_id is _CURRENT:
            parent_id = chapter["current_version_id"]
        selection = self.get_selection(selection_id)
        take = self.get_recording(take_id)
        self._validate_edit(chapter, parent_id, selection, take, mode,
                            fade_in_frames, fade_out_frames,
                            trim_start_frames, trim_end_frames)
        with self._lock:
            seq = self._one(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM versions WHERE chapter_id=?",
                (chapter_id,))["s"]
            vid = self._insert(
                "INSERT INTO versions(chapter_id, parent_id, seq, selection_id, take_id,"
                " mode, fade_in_frames, fade_out_frames, trim_start_frames,"
                " trim_end_frames, created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (chapter_id, parent_id, seq, selection_id, take_id, mode,
                 fade_in_frames, fade_out_frames, trim_start_frames, trim_end_frames,
                 utcnow()))
            self._conn.execute(
                "UPDATE chapters SET current_version_id=? WHERE id=?", (vid, chapter_id))
            self._conn.commit()
            return self.get_version(vid)

    def switch_version(self, version_id, take_id, mode=None, fade_in_frames=None,
                       fade_out_frames=None, trim_start_frames=None, trim_end_frames=None):
        old = self.get_version(version_id)
        return self.create_version(
            old["chapter_id"], old["selection_id"], take_id,
            mode or old["mode"],
            old["fade_in_frames"] if fade_in_frames is None else fade_in_frames,
            old["fade_out_frames"] if fade_out_frames is None else fade_out_frames,
            old["trim_start_frames"] if trim_start_frames is None else trim_start_frames,
            old["trim_end_frames"] if trim_end_frames is None else trim_end_frames,
            parent_id=old["parent_id"])

    def undo(self, chapter_id):
        ch = self.get_chapter(chapter_id)
        cur = ch["current_version_id"]
        if cur is None:
            raise StoreError("已处于原始版本，无法撤销")
        parent = self.get_version(cur)["parent_id"]
        with self._lock:
            self._conn.execute(
                "UPDATE chapters SET current_version_id=? WHERE id=?", (parent, chapter_id))
            self._conn.commit()
        return self.get_chapter(chapter_id)

    def redo(self, chapter_id):
        ch = self.get_chapter(chapter_id)
        cur = ch["current_version_id"]
        if cur is None:
            row = self._one(
                "SELECT id FROM versions WHERE chapter_id=? AND parent_id IS NULL"
                " ORDER BY seq DESC LIMIT 1", (chapter_id,))
        else:
            row = self._one(
                "SELECT id FROM versions WHERE chapter_id=? AND parent_id=?"
                " ORDER BY seq DESC LIMIT 1", (chapter_id, cur))
        if not row:
            raise StoreError("没有可重做的版本")
        with self._lock:
            self._conn.execute(
                "UPDATE chapters SET current_version_id=? WHERE id=?",
                (row["id"], chapter_id))
            self._conn.commit()
        return self.get_chapter(chapter_id)

    # ------------------------------------------------------------ render jobs
    def create_render_job(self, chapter_id, version_id):
        with self._lock:
            jid = self._insert(
                "INSERT INTO render_jobs(chapter_id, version_id, status, created_utc)"
                " VALUES(?,?, 'running', ?)", (chapter_id, version_id, utcnow()))
            return self.get_render_job(jid)

    def finish_render_job(self, job_id, status, output_path=None, report=None,
                          stale=False, error=None):
        with self._lock:
            self._conn.execute(
                "UPDATE render_jobs SET status=?, stale=?, output_path=?, report_json=?,"
                " error=?, finished_utc=? WHERE id=?",
                (status, 1 if stale else 0, output_path,
                 json.dumps(report) if report is not None else None,
                 error, utcnow(), job_id))
            self._conn.commit()
        return self.get_render_job(job_id)

    def get_render_job(self, job_id):
        row = self._one("SELECT * FROM render_jobs WHERE id=?", (job_id,))
        if not row:
            raise StoreError("渲染任务不存在", 404)
        d = dict(row)
        if d["report_json"]:
            d["report"] = json.loads(d["report_json"])
        return d

    def list_render_jobs(self, chapter_id):
        return [self.get_render_job(r["id"]) for r in self._all(
            "SELECT id FROM render_jobs WHERE chapter_id=? ORDER BY id", (chapter_id,))]
