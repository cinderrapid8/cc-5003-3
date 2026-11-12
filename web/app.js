"use strict";

const state = {
  project: null,
  chapterBuffer: null,   // AudioBuffer of original chapter
  selection: null,       // drag range in frames {start,end}
  activeMarkId: null,
  activeTakeId: null,
  mediaRecorder: null,
  audioCtx: null,
  renderToken: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const resp = await fetch(path, opt);
  const ct = resp.headers.get("Content-Type") || "";
  const data = ct.includes("application/json") ? await resp.json() : await resp.arrayBuffer();
  if (!resp.ok) throw Object.assign(new Error(data.error || "request failed"),
                                    { status: resp.status, data });
  return data;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve("base64:" + fr.result.split(",")[1]);
    fr.onerror = reject;
    fr.readAsDataURL(file);
  });
}

function pcm16ToWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const dv = new DataView(buffer);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, "RIFF");
  dv.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE"); writeStr(12, "fmt ");
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true); dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  writeStr(36, "data"); dv.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    dv.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve("base64:" + fr.result.split(",")[1]);
    fr.onerror = reject;
    fr.readAsDataURL(blob);
  });
}

function ctx() {
  if (!state.audioCtx) state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return state.audioCtx;
}

// ------------------------------------------------------------- project open
$("btn-create").onclick = async () => {
  const file = $("chapter-file").files[0];
  if (!file) return showMsg("render-msg", "请先选择章节 WAV", "err");
  const chapterWav = await fileToBase64(file);
  state.project = await api("/api/projects", { name: $("new-name").value, chapter_wav: chapterWav });
  const ab = await file.arrayBuffer();
  state.chapterBuffer = await ctx().decodeAudioData(ab.slice(0));
  enterProject();
};

function enterProject() {
  const p = state.project;
  $("project-name").textContent = `${p.name} · ${p.chapter.sample_rate} Hz · ${p.chapter.frames} 帧`;
  $("project-meta").textContent =
    `工程ID ${p.id} · 时长 ${p.chapter.duration_ms.toFixed(1)} ms · 时长按实际帧数计算`;
  for (const id of ["mark-panel", "take-panel", "select-panel", "noise-panel", "render-panel"])
    $(id).hidden = false;
  drawWaveform();
  refreshProject();
}

async function refreshProject() {
  state.project = await api(`/api/projects/${state.project.id}`);
  renderMarks(); renderTakes(); renderCandidates(); renderSelectionOptions();
  renderNoise(); renderVersions();
}

// ---------------------------------------------------------------- waveform
function drawWaveform() {
  const cv = $("wave"), c = cv.getContext("2d");
  const data = state.chapterBuffer.getChannelData(0);
  const w = cv.width, h = cv.height;
  c.clearRect(0, 0, w, h);
  const step = Math.ceil(data.length / w);
  c.strokeStyle = "#7fa8e0"; c.beginPath();
  for (let x = 0; x < w; x++) {
    let min = 1, max = -1;
    for (let i = 0; i < step; i++) {
      const v = data[x * step + i] || 0;
      if (v < min) min = v; if (v > max) max = v;
    }
    c.moveTo(x + .5, (1 + min) * h / 2);
    c.lineTo(x + .5, (1 + max) * h / 2);
  }
  c.stroke();
  // marks
  for (const m of state.project.marks) {
    const x1 = m.range_start / data.length * w, x2 = m.range_end / data.length * w;
    c.fillStyle = "rgba(255,180,84,.18)"; c.fillRect(x1, 0, x2 - x1, h);
    c.fillStyle = "#ffb454"; c.fillRect(x1, 0, 2, h); c.fillRect(x2, 0, 2, h);
    c.fillStyle = "#ffd9a0"; c.font = "11px sans-serif";
    c.fillText(m.sentence_label, x1 + 3, 12);
  }
  // drag selection
  if (state.selection) {
    const x1 = state.selection.start / data.length * w;
    const x2 = state.selection.end / data.length * w;
    c.fillStyle = "rgba(90,162,255,.25)"; c.fillRect(x1, 0, x2 - x1, h);
    c.strokeStyle = "#5aa2ff"; c.strokeRect(x1, 0, x2 - x1, h);
  }
}

let dragging = false;
$("wave").addEventListener("mousedown", (e) => {
  dragging = true;
  state.selection = { start: xToFrame(e), end: xToFrame(e) };
  drawWaveform();
});
$("wave").addEventListener("mousemove", (e) => {
  if (!dragging) return;
  state.selection.end = xToFrame(e);
  drawWaveform();
  updateRangeLabel();
});
window.addEventListener("mouseup", () => {
  if (dragging) { dragging = false; updateRangeLabel(); }
});
function xToFrame(e) {
  const r = $("wave").getBoundingClientRect();
  const ratio = (e.clientX - r.left) / r.width;
  return Math.max(0, Math.min(state.chapterBuffer.length - 1,
    Math.round(ratio * state.chapterBuffer.length)));
}
function updateRangeLabel() {
  if (!state.selection) return;
  const s = Math.min(state.selection.start, state.selection.end);
  const en = Math.max(state.selection.start, state.selection.end);
  const sr = state.chapterBuffer.sampleRate;
  $("mark-range").textContent = `帧 ${s} → ${en}（${((en - s) * 1000 / sr).toFixed(1)} ms）`;
}

$("btn-mark").onclick = async () => {
  if (!state.selection) return;
  const start = Math.min(state.selection.start, state.selection.end);
  const end = Math.max(state.selection.start, state.selection.end);
  const mark = await api(`/api/projects/${state.project.id}/marks`, {
    sentence_label: $("mark-label").value || "未命名句子", range_start: start, range_end: end });
  state.activeMarkId = mark.id;
  await refreshProject();
};

function renderMarks() {
  const tb = $("marks-body"); tb.innerHTML = "";
  const sr = state.project.chapter.sample_rate;
  for (const m of state.project.marks) {
    const tr = document.createElement("tr");
    const sel = m.selection;
    tr.innerHTML = `<td>${m.sentence_label}</td>
      <td class="cue">${m.range_start}…${m.range_end}</td>
      <td>${((m.range_end - m.range_start) * 1000 / sr).toFixed(1)} ms</td>
      <td>${sel ? sel.candidate_id.slice(0, 8) : "—"}</td>
      <td>${sel ? `<span class="tag ${sel.mode}">${sel.mode}</span>` : "—"}</td>
      <td></td>`;
    const btn = document.createElement("button");
    btn.className = "secondary"; btn.textContent = "选中此标记";
    btn.onclick = () => { state.activeMarkId = m.id; renderTakes(); renderCandidates(); };
    tr.lastChild.appendChild(btn);
    tb.appendChild(tr);
  }
  drawWaveform();
}

function activeMark() {
  return state.project.marks.find((m) => m.id === state.activeMarkId) || null;
}

// ---------------------------------------------------------------- recording
$("btn-rec").onclick = async () => {
  const mark = activeMark();
  if (!mark) return showMsg("render-msg", "请先选择一个标记", "err");
  const begin = await api(`/api/projects/${state.project.id}/takes`, {
    mark_id: mark.id,
    // Device-side clock: stored verbatim, never used for frame timing.
    device_started_at: Date.now() / 1000,
    client_clock_ms: performance.now(),
    first_frame_device_offset_ms: null,
  });
  state.activeTakeId = begin.take_id;

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ac = ctx();
  const source = ac.createMediaStreamSource(stream);
  const node = ac.createScriptProcessor(4096, 1, 1);
  const collected = [];
  const firstFrameAt = performance.now();
  node.onaudioprocess = (e) => collected.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  source.connect(node); node.connect(ac.destination);
  $("rec-state").textContent = `录音中… 录次 ${begin.take_id.slice(0, 8)}`;
  $("btn-rec").disabled = true; $("btn-stop").disabled = false; $("btn-interrupt").disabled = false;

  state.mediaRecorder = {
    stream, node, source, collected, sampleRate: ac.sampleRate,
    firstFrameOffset: firstFrameAt - performance.now(),
    stop(complete, reason) {
      node.disconnect(); source.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      const merged = new Float32Array(collected.reduce((n, a) => n + a.length, 0));
      let off = 0;
      for (const a of collected) { merged.set(a, off); off += a.length; }
      const wav = pcm16ToWav(merged, ac.sampleRate);
      return finishUpload(begin.take_id, wav, complete, reason, merged.length);
    },
  };
};

async function finishUpload(takeId, wavBlob, complete, reason, frameCount) {
  const payload = { wav: await blobToBase64(wavBlob), complete };
  if (reason) payload.reason = reason;
  try {
    const r = await api(`/api/projects/${state.project.id}/takes/${takeId}/upload`, payload);
    showMsg("render-msg",
      complete ? `录次完成：实际采集 ${r.frames} 帧（${r.duration_ms?.toFixed(1) ?? "?"} ms，按帧数计时）`
               : `录次中断已保存但不可渲染：${r.frames} 帧`,
      complete ? "ok" : "warn");
  } catch (e) {
    showMsg("render-msg", `上传失败：${e.data?.error || e.message}`, "err");
  }
  $("btn-rec").disabled = false; $("btn-stop").disabled = true; $("btn-interrupt").disabled = true;
  state.mediaRecorder = null;
  await refreshProject();
}

$("btn-stop").onclick = () => state.mediaRecorder?.stop(true);
$("btn-interrupt").onclick = () =>
  state.mediaRecorder?.stop(false, "用户模拟网络中断，候选未写完");

function renderTakes() {
  const mark = activeMark();
  const tb = $("takes-body"); tb.innerHTML = "";
  if (!mark) return;
  const sr = state.project.chapter.sample_rate;
  for (const t of mark.takes) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="cue">${t.id.slice(0, 8)}</td>
      <td><span class="tag ${t.state}">${t.state}</span></td>
      <td>${t.frame_count}</td>
      <td>${(t.frame_count * 1000 / sr).toFixed(1)}</td>
      <td class="cue">${t.device_started_at ?? "—"}</td>
      <td>${t.first_frame_device_offset_ms ?? "—"}</td><td></td>`;
    const use = document.createElement("button");
    use.className = "secondary"; use.textContent = "选择此录次";
    use.disabled = t.state !== "complete";
    use.onclick = () => {
      state.activeTakeId = t.id;
      $("trim-start").value = 0; $("trim-end").value = t.frame_count;
      playTake(t.id);
    };
    tr.lastChild.appendChild(use);
    tb.appendChild(tr);
  }
}

async function playTake(takeId) {
  const take = (await api(`/api/projects/${state.project.id}`)).marks
    .flatMap((m) => m.takes).find((x) => x.id === takeId);
  // take rows don't carry blob id; fetch full take via candidates parent is not
  // available pre-candidate, so use a tiny internal lookup endpoint through
  // project blobs is impossible; instead create an object URL from the blob
  // route using the take blob resolved below.
  const resp = await fetch(`/api/takes/${takeId}/wav`);
  if (resp.ok) $("ab-candidate").src = URL.createObjectURL(await resp.blob());
}

// ------------------------------------------------------------- candidates
$("btn-candidate").onclick = async () => {
  const mark = activeMark();
  if (!mark || !state.activeTakeId) return showMsg("render-msg", "请选择标记和已完成录次", "err");
  try {
    await api(`/api/projects/${state.project.id}/candidates`, {
      take_id: state.activeTakeId, mark_id: mark.id,
      trim_start: Number($("trim-start").value), trim_end: Number($("trim-end").value),
    });
    await refreshProject();
    showMsg("render-msg", "候选已生成（前导静音仅按显式 trim 处理）", "ok");
  } catch (e) {
    showMsg("render-msg", `候选被拒绝：${e.data?.error}（只有 complete 录次可进入渲染）`, "err");
  }
};

function renderCandidates() {
  const mark = activeMark();
  const tb = $("cands-body"); tb.innerHTML = "";
  if (!mark) return;
  for (const cd of mark.candidates) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="cue">${cd.id.slice(0, 8)}</td>
      <td class="cue">${cd.take_id.slice(0, 8)}</td>
      <td>${cd.trim_start}…${cd.trim_end}</td><td>${cd.frame_count}</td><td></td><td></td>`;
    const play = document.createElement("button");
    play.className = "secondary"; play.textContent = "试听";
    play.onclick = () => playTake(cd.take_id);
    tr.children[4].appendChild(play);
    const del = document.createElement("button");
    del.className = "danger"; del.textContent = "删除";
    del.onclick = async () => {
      try {
        await api(`/api/candidates/${cd.id}/delete`, {});
        await refreshProject();
      } catch (e) {
        showMsg("render-msg", `删除被拒绝：${e.data?.error}（历史版本仍引用其字节）`, "err");
      }
    };
    tr.children[5].appendChild(del);
    tb.appendChild(tr);
  }
  renderContextAB();
}

async function renderContextAB() {
  const mark = activeMark();
  if (!mark) return;
  const sr = state.chapterBuffer.sampleRate;
  const win = Math.floor(sr * 0.5);
  $("ab-before").src = URL.createObjectURL(sliceChapter(
    Math.max(0, mark.range_start - win), mark.range_start));
  $("ab-after").src = URL.createObjectURL(sliceChapter(
    mark.range_end, Math.min(state.chapterBuffer.length, mark.range_end + win)));
}

function sliceChapter(start, end) {
  const ac = ctx();
  const buf = ac.createBuffer(1, end - start, state.chapterBuffer.sampleRate);
  buf.copyToChannel(state.chapterBuffer.getChannelData(0).slice(start, end), 0);
  const nodes = ac.createBufferSource();
  // Offline render to WAV via manual PCM extraction (buffer already holds PCM).
  return pcm16ToWav(buf.getChannelData(0), buf.sampleRate);
}

// ------------------------------------------------------------- selection
function renderSelectionOptions() {
  const mark = activeMark();
  const sel = $("sel-candidate");
  sel.innerHTML = "";
  if (!mark) return;
  for (const cd of mark.candidates) {
    const o = document.createElement("option");
    o.value = cd.id; o.textContent = `${cd.id.slice(0, 8)} · ${cd.frame_count} 帧`;
    sel.appendChild(o);
  }
  if (mark.selection) {
    sel.value = mark.selection.candidate_id;
    $("sel-mode").value = mark.selection.mode;
    $("sel-fade").value = mark.selection.fade_ms;
  }
}

$("btn-save-selection").onclick = async () => {
  const mark = activeMark();
  if (!mark || !$("sel-candidate").value) return;
  try {
    await api(`/api/projects/${state.project.id}/selection`, {
      mark_id: mark.id, candidate_id: $("sel-candidate").value,
      mode: $("sel-mode").value, fade_ms: Number($("sel-fade").value),
    });
    state.renderToken = null; $("btn-commit").disabled = true;
    showMsg("render-msg", "采用关系已保存；导出快照需重新准备", "ok");
    await refreshProject();
  } catch (e) {
    showMsg("render-msg", `保存失败：${e.data?.error || e.message}`, "err");
  }
};

// ---------------------------------------------------------------- noise
$("btn-noise").onclick = async () => {
  const f = $("noise-file").files[0];
  if (!f) return;
  const r = await api(`/api/projects/${state.project.id}/noise`,
                      { wav: await fileToBase64(f) });
  $("noise-state").textContent =
    `底噪 ${r.frame_count} 帧 / RMS ${r.rms.toFixed(1)} / ` +
    (r.sufficient ? "充足，可用于 keep_duration" : "不足（需 ≥200ms 且非数字静音）");
  $("noise-state").style.color = r.sufficient ? "var(--ok)" : "var(--warn)";
  await refreshProject();
};
function renderNoise() {
  const list = state.project.noise_excerpts || [];
  if (list.length) {
    const n = list[list.length - 1];
    $("noise-state").textContent = `当前底噪：${n.frame_count} 帧，RMS ${n.rms.toFixed(1)}`;
  }
}

// ---------------------------------------------------------------- render
$("btn-prepare").onclick = async () => {
  const noise = state.project.noise_excerpts.slice(-1)[0];
  try {
    const prep = await api(`/api/projects/${state.project.id}/render/prepare`,
                           noise ? { noise_id: noise.id } : {});
    state.renderToken = prep.render_token;
    $("btn-commit").disabled = false;
    showMsg("render-msg", `已准备导出快照 ${prep.snapshot.length} 条采用关系；改选会使令牌失效`, "ok");
  } catch (e) {
    showMsg("render-msg", `准备失败：${e.data?.error || e.message}`, "err");
  }
};

$("btn-commit").onclick = async () => {
  if (!state.renderToken) return;
  try {
    const r = await api(`/api/renders/commit?token=${state.renderToken}`,
                        { label: `version-${new Date().toISOString().slice(0, 19)}` });
    state.renderToken = null; $("btn-commit").disabled = true;
    showMsg("render-msg",
      `渲染完成：${r.frames} 帧，长度变化 ${r.delta_frames} 帧；接缝 ${r.events.length} 处`, "ok");
    await refreshProject();
  } catch (e) {
    state.renderToken = null; $("btn-commit").disabled = true;
    const code = e.data?.error || e.message;
    showMsg("render-msg",
      code === "SELECTION_CHANGED_DURING_EXPORT"
        ? "导出期间发生改选，快照已失效，请重新准备后再渲染"
        : `渲染被拒绝：${code} ${JSON.stringify(e.data?.details || {})}`,
      code === "SELECTION_CHANGED_DURING_EXPORT" ? "warn" : "err");
  }
};

function renderVersions() {
  const tb = $("versions-body"); tb.innerHTML = "";
  for (const v of state.project.versions) {
    const tr = document.createElement("tr");
    const cues = JSON.parse(v.cue_json).filter(([, l]) => l.startsWith("SEAM"));
    const delta = v.frame_count - state.project.chapter.frames;
    tr.innerHTML = `<td>${v.label}</td><td>${v.frame_count}</td>
      <td class="${delta === 0 ? "muted" : ""}">${delta > 0 ? "+" : ""}${delta}</td>
      <td class="cue">${cues.map(([, l]) => l).join("<br>") || "—"}</td><td></td>`;
    const a = document.createElement("a");
    a.href = `/api/projects/${state.project.id}/versions/${v.id}`;
    a.textContent = "下载带 cue WAV"; a.className = "secondary";
    a.style.padding = "6px 10px"; a.style.borderRadius = "7px";
    a.style.background = "#2c333f"; a.style.color = "var(--fg)"; a.style.textDecoration = "none";
    tr.lastChild.appendChild(a);
    tb.appendChild(tr);
  }
}

function showMsg(elId, text, kind) {
  const el = $(elId);
  el.innerHTML = `<div class="msg ${kind}">${text}</div>`;
}
