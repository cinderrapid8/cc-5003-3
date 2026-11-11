/* Pickup re-recording workbench UI. Vanilla JS, no build step. */

const $ = (id) => document.getElementById(id);

const state = {
  projects: [],
  projectId: null,
  project: null,
  chapterId: null,
  detail: null,
  rate: 8000,
  frames: 0,
  peaks: null,
  drag: null,
  pendingSelection: null,
  selectionId: null,
  takeId: null,
  recording: null,
  audioCtx: null,
  pollTimer: null,
  previewUrls: {},
};

/* ------------------------------------------------------------ helpers */

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await res.json()
             : ct.includes("audio/") ? await res.arrayBuffer()
             : await res.text();
  if (!res.ok) {
    const msg = (body && body.error) || (typeof body === "string" ? body : res.statusText);
    throw new Error(msg);
  }
  return body;
}

function apiJSON(path, obj, method = "POST") {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj === undefined ? {} : obj),
  });
}

function apiRaw(path, data) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: data,
  });
}

function msToFrames(el) {
  const v = Number(el.value || 0);
  return Math.round(v * state.rate / 1000);
}

function fmtSec(frames) {
  return (frames / state.rate).toFixed(2) + " 秒";
}

function b64ToBlob(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: "audio/wav" });
}

async function sha256Hex(buf) {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function msg(el, text, kind) {
  el.textContent = text || "";
  el.className = "hint" + (kind ? " " + kind : "");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function ensureCtx() {
  if (!state.audioCtx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    state.audioCtx = new AC();
  }
  if (state.audioCtx.state === "suspended") await state.audioCtx.resume();
  return state.audioCtx;
}

/* ------------------------------------------------------------ projects */

async function refreshProjects(preferChapterId) {
  try {
    const r = await api("/api/projects");
    state.projects = r.projects;
    const sel = $("projectSelect");
    sel.innerHTML = state.projects
      .map((p) => `<option value="${p.id}">#${p.id} ${esc(p.name)}</option>`)
      .join("");
    if (!state.projects.length) {
      msg($("projectInfo"), "还没有工程；点击“新建演示工程”开始。");
      return;
    }
    if (!state.projectId || !state.projects.some((p) => p.id === state.projectId)) {
      state.projectId = state.projects[state.projects.length - 1].id;
    }
    sel.value = state.projectId;
    await loadProject(state.projectId, preferChapterId);
  } catch (e) {
    msg($("projectInfo"), e.message, "bad");
  }
}

async function loadProject(pid, preferChapterId) {
  state.projectId = Number(pid);
  state.project = await api(`/api/projects/${state.projectId}`);
  const chapters = state.project.chapters;
  const rt = state.project.roomtone;
  msg($("projectInfo"), rt
    ? `工程 #${state.projectId} · 底噪 ${rt.frames} 帧 / ${(rt.frames / (rt.sample_rate || 1)).toFixed(2)} 秒`
    : `工程 #${state.projectId} · 未提供环境底噪`);
  let cid = preferChapterId;
  if (!cid) {
    if (state.chapterId && chapters.some((c) => c.id === state.chapterId)) cid = state.chapterId;
    else if (chapters.length) cid = chapters[chapters.length - 1].id;
  }
  if (!cid) {
    state.chapterId = null;
    state.detail = null;
    $("timelineInfo").textContent = "该工程还没有章节，请新建或上传章节源。";
    renderCandidates();
    renderVersions();
    return;
  }
  await loadChapter(cid);
  renderRoomtone();
}

async function newProject() {
  try {
    const ctx = await ensureCtx();
    const p = await apiJSON("/api/projects",
      { name: "补录工程 " + new Date().toLocaleString("zh-CN") });
    const d = await apiJSON(`/api/projects/${p.project.id}/demo`,
      { rate: ctx.sampleRate, sentences: 8 });
    state.projectId = p.project.id;
    await refreshProjects(d.chapter.id);
  } catch (e) {
    msg($("projectInfo"), e.message, "bad");
  }
}

async function newDemoChapter() {
  try {
    if (!state.projectId) return newProject();
    const ctx = await ensureCtx();
    const d = await apiJSON(`/api/projects/${state.projectId}/demo`,
      { rate: ctx.sampleRate, sentences: 8 });
    await refreshProjects(d.chapter.id);
  } catch (e) {
    msg($("projectInfo"), e.message, "bad");
  }
}

/* ------------------------------------------------------------ chapter */

async function loadChapter(cid) {
  state.chapterId = Number(cid);
  state.detail = await api(`/api/chapters/${state.chapterId}`);
  state.rate = state.detail.rate;
  state.frames = state.detail.timeline_frames;
  state.selectionId = null;
  state.pendingSelection = null;
  const cur = state.detail.chapter.current_version_id;
  await loadPeaks(cur);
  renderAll();
}

async function loadPeaks(versionId) {
  const q = versionId == null ? "" : `&version_id=${versionId}`;
  const pk = await api(
    `/api/chapters/${state.chapterId}/peaks?buckets=900${q}`);
  state.peaks = pk;
  state.frames = pk.frames;
  drawWave();
}

function renderAll() {
  renderTimelineInfo();
  renderSelections();
  renderCandidates();
  renderTakeSelect();
  renderVersions();
  renderJobs();
  renderRoomtone();
  drawWave();
}

function renderTimelineInfo() {
  const d = state.detail;
  if (!d) return;
  const cur = d.chapter.current_version_id;
  $("timelineInfo").textContent =
    `${esc(d.chapter.title)} · ${d.rate} Hz · ${state.frames} 帧 / ${fmtSec(state.frames)}` +
    ` · 当前版本 ${cur == null ? "原始" : "#" + cur} · 撤销${d.can_undo ? "可用" : "无"}`;
}

function renderRoomtone() {
  const rt = state.project && state.project.roomtone;
  msg($("roomtoneInfo"), rt
    ? `底噪录音 #${rt.id} · ${rt.frames} 帧 · ${rt.status === "complete" ? "可用" : rt.status}`
    : "未提供真实底噪；keep_duration 填充会被拒绝");
}

/* ------------------------------------------------------------ waveform */

function drawWave() {
  const cv = $("waveCanvas");
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 900;
  cv.width = w * dpr;
  cv.height = 150 * dpr;
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const h = 150;
  const mid = h / 2;
  g.fillStyle = "#0f1115";
  g.fillRect(0, 0, w, h);

  const peaks = state.peaks && state.peaks.peaks;
  if (peaks && peaks.length) {
    const bw = w / peaks.length;
    g.strokeStyle = "#5f7189";
    g.beginPath();
    for (let i = 0; i < peaks.length; i++) {
      const [lo, hi] = peaks[i];
      const x = i * bw + bw / 2;
      const y1 = mid - (hi / 32768) * (mid - 8);
      const y2 = mid - (lo / 32768) * (mid - 8);
      g.moveTo(x, y1);
      g.lineTo(x, Math.max(y1 + 1, y2));
    }
    g.stroke();
  }
  g.strokeStyle = "#2a2f3a";
  g.beginPath();
  g.moveTo(0, mid);
  g.lineTo(w, mid);
  g.stroke();

  const range = state.pendingSelection;
  if (range && state.frames > 0) {
    const x1 = (range.start / state.frames) * w;
    const x2 = (range.end / state.frames) * w;
    g.fillStyle = "rgba(255,159,67,0.28)";
    g.fillRect(x1, 0, Math.max(2, x2 - x1), h);
    g.strokeStyle = "#ff9f43";
    g.strokeRect(x1 + 0.5, 0.5, Math.max(2, x2 - x1 - 1), h - 1);
  }

  // markers for selections on the current timeline
  if (state.detail && state.frames > 0) {
    const cur = state.detail.chapter.current_version_id;
    for (const s of state.detail.selections) {
      if ((s.version_id || null) !== (cur || null)) continue;
      const x1 = (s.start_frame / state.frames) * w;
      const x2 = (s.end_frame / state.frames) * w;
      g.fillStyle = "rgba(79,157,255,0.10)";
      g.fillRect(x1, 0, Math.max(1, x2 - x1), h);
    }
  }
  state.drag && drawDragOverlay(g, w, h);
}

function drawDragOverlay(g, w, h) {
  const { x0, x1 } = state.drag;
  g.fillStyle = "rgba(255,159,67,0.20)";
  g.fillRect(Math.min(x0, x1), 0, Math.abs(x1 - x0), h);
}

function canvasPos(ev) {
  const cv = $("waveCanvas");
  const rect = cv.getBoundingClientRect();
  return Math.min(rect.width, Math.max(0, ev.clientX - rect.left));
}

function frameAtX(x) {
  const cv = $("waveCanvas");
  const w = cv.clientWidth || 1;
  return Math.round((x / w) * state.frames);
}

$("waveCanvas").addEventListener("mousedown", (ev) => {
  if (!state.frames) return;
  const x = canvasPos(ev);
  state.drag = { x0: x, x1: x };
  drawWave();
});
window.addEventListener("mousemove", (ev) => {
  if (!state.drag) return;
  state.drag.x1 = canvasPos(ev);
  drawWave();
});
window.addEventListener("mouseup", () => {
  if (!state.drag) return;
  const { x0, x1 } = state.drag;
  state.drag = null;
  if (Math.abs(x1 - x0) < 8) { drawWave(); return; }
  const a = frameAtX(Math.min(x0, x1));
  const b = frameAtX(Math.max(x0, x1));
  if (b - a < 1) { drawWave(); return; }
  state.pendingSelection = { start: a, end: b };
  msg($("selectionInfo"),
    `待标记选段：${a} – ${b} 帧 / ${fmtSec(b - a)}`);
  drawWave();
});

$("markBtn").addEventListener("click", async () => {
  if (!state.pendingSelection) return msg($("selectionInfo"), "先在波形上拖动选出一段", "bad");
  try {
    const r = await apiJSON(`/api/chapters/${state.chapterId}/selections`, {
      start_frame: state.pendingSelection.start,
      end_frame: state.pendingSelection.end,
      label: $("selectionLabel").value || "补录句",
    });
    state.selectionId = r.selection.id;
    await reloadDetail();
    msg($("selectionInfo"), `已标记选段 #${r.selection.id}`, "ok");
  } catch (e) {
    msg($("selectionInfo"), e.message, "bad");
  }
});

async function reloadDetail() {
  state.detail = await api(`/api/chapters/${state.chapterId}`);
  state.frames = state.detail.timeline_frames;
  await loadPeaks(state.detail.chapter.current_version_id);
  renderAll();
}

/* ------------------------------------------------------------ selections */

function renderSelections() {
  const box = $("selectionList");
  const cur = state.detail ? state.detail.chapter.current_version_id : null;
  const sels = state.detail ? state.detail.selections : [];
  box.innerHTML = sels.map((s) => {
    const onOther = (s.version_id || null) !== (cur || null);
    return `<div class="chip ${s.id === state.selectionId ? "active" : ""}"
      data-id="${s.id}" title="基于版本 ${s.version_id == null ? "原始" : "#" + s.version_id}">
      #${s.id} [${s.start_frame}, ${s.end_frame}) ${esc(s.label || "")}${onOther ? " ⚠其他版本" : ""}
    </div>`;
  }).join("");
  box.querySelectorAll(".chip").forEach((el) => {
    el.addEventListener("click", () => {
      const s = sels.find((x) => x.id === Number(el.dataset.id));
      if (!s) return;
      state.selectionId = s.id;
      state.pendingSelection = { start: s.start_frame, end: s.end_frame };
      msg($("selectionInfo"), `已选择选段 #${s.id} [${s.start_frame}, ${s.end_frame})`);
      renderSelections();
      drawWave();
    });
  });
}

/* ------------------------------------------------------------ recording */

function setRecButtons(on) {
  $("recordBtn").disabled = on;
  $("stopBtn").disabled = !on;
  $("interruptBtn").disabled = !on;
}

async function startRecording() {
  if (!state.chapterId) return msg($("recState"), "先选择章节", "bad");
  try {
    const ctx = await ensureCtx();
    if (ctx.sampleRate !== state.rate) {
      msg($("recState"),
        `注意：麦克风采样率 ${ctx.sampleRate} Hz 与章节 ${state.rate} Hz 不一致；采用时会被拒绝（不做重采样）`,
        "bad");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    const t = await apiJSON("/api/takes", {
      project_id: state.projectId, chapter_id: state.chapterId, sample_rate: ctx.sampleRate,
    });
    const rec = {
      takeId: t.take.id, chunks: [], total: 0, seq: 0, queue: Promise.resolve(),
      deviceStart: new Date().toISOString(), stream, stopped: false, error: null, ctx,
    };
    const srcNode = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const sink = ctx.createGain();
    sink.gain.value = 0;
    srcNode.connect(proc);
    proc.connect(sink);
    sink.connect(ctx.destination);
    proc.onaudioprocess = (ev) => {
      if (rec.stopped || rec.error) return;
      const f32 = ev.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        const v = Math.max(-1, Math.min(1, f32[i]));
        i16[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      rec.chunks.push(i16);
      rec.total += i16.length;
      const seq = rec.seq++;
      const buf = i16.buffer;
      rec.queue = rec.queue.then(async () => {
        if (rec.error) return;
        const r = await apiRaw(`/api/takes/${rec.takeId}/chunks?seq=${seq}`, buf);
        msg($("recState"),
          `录制中（录次 #${rec.takeId}）：服务端实收 ${r.frames}/${rec.ctx.sampleRate} 帧 = ${(r.frames / rec.ctx.sampleRate).toFixed(2)} 秒 · 设备起始 ${rec.deviceStart}（仅记录，不参与对齐）`);
      }).catch((e) => { rec.error = e; });
    };
    rec.proc = proc; rec.srcNode = srcNode; rec.sink = sink;
    state.recording = rec;
    setRecButtons(true);
    msg($("recState"), `录次 #${rec.takeId} 开始；设备起始 ${rec.deviceStart}`);
    await reloadDetail();
  } catch (e) {
    msg($("recState"), "无法开始录制：" + e.message, "bad");
    setRecButtons(false);
  }
}

async function stopRecording(finalize) {
  const rec = state.recording;
  if (!rec) return;
  rec.stopped = true;
  setRecButtons(false);
  try {
    rec.proc.disconnect(); rec.srcNode.disconnect(); rec.sink.disconnect();
    rec.stream.getTracks().forEach((tr) => tr.stop());
    await rec.queue;
    if (rec.error) throw rec.error;
    if (!finalize) {
      await apiJSON(`/api/takes/${rec.takeId}/abort`);
      msg($("recState"), `录次 #${rec.takeId} 已中断，未完成，无法采用或渲染`, "bad");
    } else {
      const total = rec.total;
      const combined = new Int16Array(total);
      let off = 0;
      for (const c of rec.chunks) { combined.set(c, off); off += c.length; }
      const sha = await sha256Hex(combined.buffer);
      const r = await apiJSON(`/api/takes/${rec.takeId}/finalize`, {
        frames: total, sha256: sha, device_start_utc: rec.deviceStart,
      });
      msg($("recState"),
        `录次 #${rec.takeId} 完成：服务端计数 ${r.take.frames} 帧 = ${fmtSec(r.take.frames)}（设备起始 ${rec.deviceStart} 仅存档）`,
        "ok");
    }
  } catch (e) {
    msg($("recState"), "录制收尾失败：" + e.message, "bad");
  } finally {
    state.recording = null;
    await reloadDetail();
  }
}

async function simulateUnfinishedTake() {
  /* Deliberately finalize a take whose bytes were never fully written. */
  try {
    const ctx = await ensureCtx();
    const t = await apiJSON("/api/takes", {
      project_id: state.projectId, chapter_id: state.chapterId, sample_rate: ctx.sampleRate,
    });
    const declaredFrames = 4096;
    const full = new Int16Array(declaredFrames);
    for (let i = 0; i < full.length; i++) full[i] = 0;
    const half = full.slice(0, declaredFrames / 2);
    await apiRaw(`/api/takes/${t.take.id}/chunks?seq=0`, half.buffer);
    const sha = await sha256Hex(full.buffer);
    try {
      await apiJSON(`/api/takes/${t.take.id}/finalize`, {
        frames: declaredFrames, sha256: sha, device_start_utc: new Date().toISOString(),
      });
      msg($("recState"), "意外：未写完的录次被接受", "bad");
    } catch (e) {
      msg($("recState"), `未写完的录次 #${t.take.id} 被拒绝：${e.message}`, "ok");
    }
    await reloadDetail();
  } catch (e) {
    msg($("recState"), e.message, "bad");
  }
}

/* ------------------------------------------------------------ candidates */

function statusBadge(t) {
  const map = {
    complete: ["ok", "已完成"],
    recording: ["warn", "录制中"],
    incomplete: ["warn", "未完成"],
    corrupt: ["bad", "损坏/未写完"],
  };
  const [cls, label] = map[t.status] || ["", t.status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderCandidates() {
  const box = $("candidateList");
  const d = state.detail;
  if (!d || !d.candidates.length) {
    box.innerHTML = `<span class="hint">还没有候选录次。选择选段后开始录制。</span>`;
    return;
  }
  box.innerHTML = d.candidates.map((t) => {
    const frames = t.status === "complete" ? t.frames : t.received_frames;
    const secs = t.sample_rate ? (frames / t.sample_rate).toFixed(2) : "0.00";
    const ok = t.status === "complete" && t.integrity_ok;
    return `<div class="item">
      <div class="grow">
        <div>录次 #${t.id} ${statusBadge(t)}
          <span class="badge ${t.integrity_ok ? "ok" : "bad"}">字节校验 ${t.integrity_ok ? "通过" : "未通过"}</span>
          ${t.id === state.takeId ? '<span class="badge warn">当前候选</span>' : ""}</div>
        <div class="hint">服务端计数 ${frames} 帧 / ${secs} 秒 · ${t.sample_rate} Hz · 设备起始 ${esc(t.device_start_utc || "—")}（不用于对齐）</div>
        <div class="mono">sha256 ${esc(String(t.pcm_sha256 || "—").slice(0, 24))}…</div>
      </div>
      ${ok ? `<audio controls preload="none" src="/api/recordings/${t.id}/wav"></audio>` : ""}
      <button data-act="pick" data-id="${t.id}">选为当前候选</button>
      <button data-act="del" data-id="${t.id}" class="danger">删除</button>
    </div>`;
  }).join("");

  box.querySelectorAll("button[data-act=pick]").forEach((b) => {
    b.addEventListener("click", () => selectTake(Number(b.dataset.id)));
  });
  box.querySelectorAll("button[data-act=del]").forEach((b) => {
    b.addEventListener("click", () => deleteTake(Number(b.dataset.id)));
  });
}

function renderTakeSelect() {
  const sel = $("takeSelect");
  const d = state.detail;
  const usable = d ? d.candidates.filter(
    (t) => t.status === "complete" && t.integrity_ok && !t.deleted_utc) : [];
  sel.innerHTML = usable.length
    ? usable.map((t) => `<option value="${t.id}">#${t.id} · ${(t.frames / t.sample_rate).toFixed(2)} 秒</option>`).join("")
    : `<option value="">（无可用候选）</option>`;
  if (usable.length && !usable.some((t) => t.id === state.takeId)) {
    state.takeId = usable[usable.length - 1].id;
  }
  sel.value = state.takeId || "";
}

function selectTake(id) {
  state.takeId = id;
  renderCandidates();
  renderTakeSelect();
}

async function deleteTake(id) {
  if (!confirm(`删除候选录次 #${id}？如仍被历史版本引用，原始字节会被保留。`)) return;
  try {
    const r = await apiJSON(`/api/recordings/${id}`, {}, "DELETE");
    let note = r.bytes_removed
      ? `候选 #${id} 已删除，字节已移除`
      : `候选 #${id} 标记删除，但仍有 ${r.refs} 个历史版本引用，原始字节保留`;
    msg($("adoptMsg"), note, r.bytes_removed ? "ok" : "bad");
    await reloadDetail();
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

/* ------------------------------------------------------------ preview */

function editParams(takeId) {
  return {
    take_id: takeId,
    mode: $("modeSelect").value,
    fade_in_frames: msToFrames($("fadeIn")),
    fade_out_frames: msToFrames($("fadeOut")),
    trim_start_frames: msToFrames($("trimStart")),
    trim_end_frames: msToFrames($("trimEnd")),
  };
}

async function preview(which) {
  if (!state.selectionId) return msg($("adoptMsg"), "先标记或选择一个选段", "bad");
  const takeId = which === "take" ? state.takeId : null;
  if (which === "take" && !takeId) return msg($("adoptMsg"), "先选择一个候选", "bad");
  const body = Object.assign(editParams(takeId), {
    selection_id: state.selectionId,
    context_frames: Math.round(state.rate * 1.0),
  });
  try {
    const r = await apiJSON(`/api/chapters/${state.chapterId}/preview`, body);
    const blob = b64ToBlob(r.wav_base64);
    const audio = which === "take" ? $("audioTake") : $("audioOrig");
    if (state.previewUrls[which]) URL.revokeObjectURL(state.previewUrls[which]);
    state.previewUrls[which] = URL.createObjectURL(blob);
    audio.src = state.previewUrls[which];
    const rep = r.report || {};
    const edit = rep.edit;
    const lines = [
      `窗口 ${rep.window ? rep.window[0] + "–" + rep.window[1] + " 帧" : "—"}`,
      edit ? `方式 ${edit.mode} · 长度变化 ${edit.delta} 帧 · 接缝 ${edit.fade_regions_out.map((x) => "[" + x.join(",") + ")").join(" ")}`
           : "原始语境（无编辑）",
      edit && edit.fill_range_out ? `底噪填充区间 ${edit.fill_range_out.join("–")}` : "",
    ].filter(Boolean);
    (which === "take" ? $("reportTake") : $("reportOrig")).textContent = lines.join("\n");
    audio.play().catch(() => {});
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

async function adopt() {
  if (!state.selectionId) return msg($("adoptMsg"), "先标记或选择一个选段", "bad");
  if (!state.takeId) return msg($("adoptMsg"), "先选择一个完成且校验通过的候选", "bad");
  const body = Object.assign(editParams(state.takeId), { selection_id: state.selectionId });
  try {
    const r = await apiJSON(`/api/chapters/${state.chapterId}/adopt`, body);
    msg($("adoptMsg"),
      `已采用录次 #${state.takeId}，生成版本 #${r.version.id}（${r.version.mode}）`, "ok");
    await reloadDetail();
    renderAll();
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

/* ------------------------------------------------------------ versions */

function renderVersions() {
  const box = $("versionList");
  const d = state.detail;
  if (!d) { box.innerHTML = ""; return; }
  const cur = d.chapter.current_version_id;
  const rows = [];
  rows.push(`<div class="item ${cur == null ? "current" : ""}">
    <div class="grow"><div>原始版本 ${cur == null ? '<span class="badge ok">当前</span>' : ""}</div>
    <div class="hint">未做任何补录</div></div>
  </div>`);
  for (const v of d.versions) {
    const take = v.take || {};
    rows.push(`<div class="item ${v.id === cur ? "current" : ""}">
      <div class="grow">
        <div>版本 #${v.id} (seq ${v.seq}) ${v.id === cur ? '<span class="badge ok">当前</span>' : ""}
          · 录次 #${v.take_id} <span class="badge ${take.status === "complete" ? "ok" : "bad"}">${esc(take.status || "?")}</span></div>
        <div class="hint">${v.mode === "insert_shift" ? "插入后移" : "保持原时长"} · 淡入 ${v.fade_in_frames} / 淡出 ${v.fade_out_frames} 帧
          · 修剪 ${v.trim_start_frames}/${v.trim_end_frames} 帧
          · 选段 #${v.selection_id} [${(v.selection || {}).start_frame}, ${(v.selection || {}).end_frame})</div>
        <div class="mono">take sha ${esc(String(take.pcm_sha256 || "—").slice(0, 20))}…</div>
      </div>
      <button data-act="switch" data-id="${v.id}">换用当前所选候选</button>
      <button data-act="render" data-id="${v.id}">导出此版本</button>
    </div>`);
  }
  box.innerHTML = rows.join("");
  box.querySelectorAll("button[data-act=switch]").forEach((b) => {
    b.addEventListener("click", () => switchTake(Number(b.dataset.id)));
  });
  box.querySelectorAll("button[data-act=render]").forEach((b) => {
    b.addEventListener("click", () => renderVersion(Number(b.dataset.id)));
  });
  $("undoBtn").disabled = !d.can_undo;
}

async function switchTake(versionId) {
  if (!state.takeId) return msg($("adoptMsg"), "先选择一个候选", "bad");
  try {
    const r = await apiJSON(`/api/versions/${versionId}/switch`, { take_id: state.takeId });
    msg($("adoptMsg"), `选段切换：版本 #${versionId} 的候选已换为录次 #${state.takeId}，生成版本 #${r.version.id}`, "ok");
    await reloadDetail();
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

async function undo() {
  try {
    await apiJSON(`/api/chapters/${state.chapterId}/undo`);
    await loadChapter(state.chapterId);
    msg($("adoptMsg"), "已撤销到上一个版本", "ok");
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

async function redo() {
  try {
    await apiJSON(`/api/chapters/${state.chapterId}/redo`);
    await loadChapter(state.chapterId);
    msg($("adoptMsg"), "已重做版本", "ok");
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

/* ------------------------------------------------------------ export */

async function renderVersion(versionId) {
  try {
    const r = await apiJSON(`/api/chapters/${state.chapterId}/render`, {
      version_id: versionId == null ? state.detail.chapter.current_version_id : versionId,
      async: true,
    });
    msg($("adoptMsg"),
      `已提交导出任务 #${r.job.id}，快照版本 ${r.job.version_id == null ? "原始" : "#" + r.job.version_id}`, "ok");
    renderJobs();
    schedulePoll();
  } catch (e) {
    msg($("adoptMsg"), e.message, "bad");
  }
}

async function renderJobs() {
  if (!state.chapterId) { $("jobList").innerHTML = ""; return; }
  try {
    const r = await api(`/api/chapters/${state.chapterId}/jobs`);
    state.detail && (state.detail.render_jobs = r.jobs);
    const box = $("jobList");
    if (!r.jobs.length) { box.innerHTML = `<span class="hint">还没有导出任务。</span>`; return; }
    box.innerHTML = r.jobs.map((j) => {
      const rep = j.report || {};
      const stale = j.stale ? `<span class="badge warn">过期：导出完成时当前版本已是 ${rep.current_version_at_finish == null ? "原始" : "#" + rep.current_version_at_finish}</span>` : "";
      const status = j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "warn";
      return `<div class="item">
        <div class="grow">
          <div>任务 #${j.id} <span class="badge ${status}">${j.status}</span>
            快照版本 ${j.version_id == null ? "原始" : "#" + j.version_id} ${stale}</div>
          <div class="hint">${rep.frames_out != null ? `${rep.frames_out} 帧 · 编辑 ${rep.edits ? rep.edits.length : 0} 处 · 链条 ${(rep.chain_version_ids || []).map((x) => "#" + x).join(" → ") || "原始"}` : ""}</div>
          ${j.error ? `<div class="hint bad">${esc(j.error)}</div>` : ""}
        </div>
        ${j.status === "done" ? `<audio controls preload="none" src="/api/render-jobs/${j.id}/wav"></audio>
          <a class="badge ok" download="render_${j.id}.wav" href="/api/render-jobs/${j.id}/wav">下载 WAV</a>` : ""}
      </div>`;
    }).join("");
    if (r.jobs.some((j) => j.status === "running")) schedulePoll();
  } catch (e) {
    $("jobList").innerHTML = `<span class="hint bad">${esc(e.message)}</span>`;
  }
}

function schedulePoll() {
  if (state.pollTimer) return;
  state.pollTimer = setTimeout(async () => {
    state.pollTimer = null;
    await renderJobs();
    if (state.detail && state.detail.render_jobs &&
        state.detail.render_jobs.some((j) => j.status === "running")) schedulePoll();
  }, 1500);
}

/* ------------------------------------------------------------ uploads */

async function uploadRoomtone(file) {
  if (!state.projectId) return;
  try {
    const buf = await file.arrayBuffer();
    await apiRaw(`/api/projects/${state.projectId}/roomtone`, buf);
    msg($("uploadMsg"), "底噪已上传", "ok");
    await loadProject(state.projectId, state.chapterId);
  } catch (e) {
    msg($("uploadMsg"), e.message, "bad");
  }
}

async function uploadChapterSource(file) {
  if (!state.projectId) return;
  try {
    const buf = await file.arrayBuffer();
    const title = file.name.replace(/\.wav$/i, "") || "新章节";
    await apiRaw(`/api/projects/${state.projectId}/chapters?title=${encodeURIComponent(title)}`, buf);
    msg($("uploadMsg"), "章节源已上传", "ok");
    await refreshProjects();
  } catch (e) {
    msg($("uploadMsg"), e.message, "bad");
  }
}

/* ------------------------------------------------------------ wiring */

$("projectSelect").addEventListener("change", (ev) => {
  state.chapterId = null;
  loadProject(Number(ev.target.value));
});
$("newProjectBtn").addEventListener("click", newProject);
$("newDemoBtn").addEventListener("click", newDemoChapter);
$("playChapterBtn").addEventListener("click", () => {
  if (!state.chapterId) return;
  const cur = state.detail.chapter.current_version_id;
  $("chapterAudio").src =
    `/api/chapters/${state.chapterId}/audio?ts=${Date.now()}` +
    (cur == null ? "" : `&version_id=${cur}`);
  $("chapterAudio").play().catch(() => {});
});
$("recordBtn").addEventListener("click", startRecording);
$("stopBtn").addEventListener("click", () => stopRecording(true));
$("interruptBtn").addEventListener("click", () => stopRecording(false));
$("simulateCutBtn").addEventListener("click", simulateUnfinishedTake);
$("takeSelect").addEventListener("change", (ev) => selectTake(Number(ev.target.value)));
$("previewOrig").addEventListener("click", () => preview("orig"));
$("previewTake").addEventListener("click", () => preview("take"));
$("adoptBtn").addEventListener("click", adopt);
$("undoBtn").addEventListener("click", undo);
$("redoBtn").addEventListener("click", redo);
$("renderBtn").addEventListener("click", () => renderVersion(null));
$("roomtoneFile").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  if (f) uploadRoomtone(f);
});
$("chapterFile").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  if (f) uploadChapterSource(f);
});
window.addEventListener("resize", drawWave);

refreshProjects();
