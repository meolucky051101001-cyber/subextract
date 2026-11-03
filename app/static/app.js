const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const els = {
  gpuBadge: $("#gpuBadge"),
  ffmpegBadge: $("#ffmpegBadge"),
  uploadCard: $("#uploadCard"),
  dropZone: $("#dropZone"),
  fileInput: $("#fileInput"),
  pickBtn: $("#pickBtn"),
  uploadProgress: $("#uploadProgress"),
  uploadBar: $("#uploadBar"),
  uploadText: $("#uploadText"),
  workspace: $("#workspace"),
  fileName: $("#fileName"),
  fileMeta: $("#fileMeta"),
  newVideoBtn: $("#newVideoBtn"),
  video: $("#video"),
  videoWrap: $("#videoWrap"),
  videoError: $("#videoError"),
  previewBtn: $("#previewBtn"),
  previewState: $("#previewState"),
  roiLayer: $("#roiLayer"),
  roiHint: $("#roiHint"),
  roiSelectBtn: $("#roiSelectBtn"),
  roiDup: $("#roiDup"),
  roiDel: $("#roiDel"),
  roiReset: $("#roiReset"),
  roiFull: $("#roiFull"),
  roiText: $("#roiText"),
  langVi: $("#langVi"),
  langEn: $("#langEn"),
  langZh: $("#langZh"),
  translateTo: $("#translateTo"),
  geminiBox: $("#geminiBox"),
  geminiKey: $("#geminiKey"),
  geminiModel: $("#geminiModel"),
  glossaryInput: $("#glossaryInput"),
  geminiState: $("#geminiState"),
  saveKeyBtn: $("#saveKeyBtn"),
  checkGeminiBtn: $("#checkGeminiBtn"),
  fpsSelect: $("#fpsSelect"),
  confRange: $("#confRange"),
  confVal: $("#confVal"),
  startBtn: $("#startBtn"),
  cancelBtn: $("#cancelBtn"),
  progressPanel: $("#progressPanel"),
  stageLabel: $("#stageLabel"),
  progressPct: $("#progressPct"),
  progressBar: $("#progressBar"),
  progressMsg: $("#progressMsg"),
  statsLine: $("#statsLine"),
  resultCard: $("#resultCard"),
  cueCount: $("#cueCount"),
  saveState: $("#saveState"),
  transWarn: $("#transWarn"),
  retransAllBtn: $("#retransAllBtn"),
  addCueBtn: $("#addCueBtn"),
  undoBtn: $("#undoBtn"),
  exportSrt: $("#exportSrt"),
  exportVtt: $("#exportVtt"),
  exportVariant: $("#exportVariant"),
  downloadDubBtn: $("#downloadDubBtn"),
  dubResult: $("#dubResult"),
  dubEnabled: $("#dubEnabled"),
  dubHint: $("#dubHint"),
  toolV1Url: $("#toolV1Url"),
  saveToolV1Btn: $("#saveToolV1Btn"),
  toolV1State: $("#toolV1State"),
  handoffBtn: $("#handoffBtn"),
  handoffPanel: $("#handoffPanel"),
  handoffStage: $("#handoffStage"),
  handoffPct: $("#handoffPct"),
  handoffBar: $("#handoffBar"),
  handoffNote: $("#handoffNote"),
  handoffOut: $("#handoffOut"),
  handoffCancelBtn: $("#handoffCancelBtn"),
  handoffCloseBtn: $("#handoffCloseBtn"),
  cueList: $("#cueList"),
  findInput: $("#findInput"),
  replaceInput: $("#replaceInput"),
  replaceAllBtn: $("#replaceAllBtn"),
  findCount: $("#findCount"),
  toast: $("#toast"),
};

const STAGE_LABELS = {
  detect: "Dò vùng phụ đề",
  load_model: "Nạp model OCR",
  ocr: "OCR chữ trong video",
  translate: "Dịch phụ đề",
  dub: "Lồng tiếng AI",
  done: "Hoàn tất",
};

const PLAY_ICON =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor" aria-hidden="true"><path d="M8 5.3v13.4L19.5 12z"/></svg>';
const TRASH_ICON =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 7h14M10 7V5h4v2M9 11v6M15 11v6M7 7l1 12h8l1-12"/></svg>';
const RETRANS_ICON =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 2.8-6.5L3 8"/><path d="M3 3v5h5"/></svg>';
const SPLIT_ICON =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v18" stroke-dasharray="3 3"/><path d="M8 9l-4 3 4 3"/><path d="M16 9l4 3-4 3"/></svg>';
const MERGE_ICON =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4l7 7M20 4l-7 7"/><path d="M12 11v9"/><path d="M9 17l3 3 3-3"/></svg>';

const DEFAULT_ROIS = [{ x: 0, y: 0.68, w: 1, h: 0.32 }];

const state = {
  job: null,
  cues: [],
  rois: DEFAULT_ROIS.map((r) => ({ ...r })),
  activeRoi: 0,
  running: false,
  selecting: false,
  drag: null,
  events: null,
  saveTimer: null,
  pollTimer: null,
  previewTimer: null,
  activeCue: -1,
  gpuName: "",
  uploading: false,
  geminiConfigured: false,
  translateModel: "",
  modelStatus: null,
  dubOutputDir: "D:\\banve",
  dubVoiceLabel: "Chí Mai",
  toolV1Url: "http://127.0.0.1:8088",
  handoff: null,
  handoffTimer: null,
  handoffSending: false,
  handoffFails: 0,
  showTrans: false,
  untranslated: [],
  retranslating: false,
  undoStack: [],
};

function showToast(message, type = "") {
  els.toast.textContent = message;
  els.toast.className = `toast ${type}`.trim();
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => els.toast.classList.add("hidden"), 4200);
}

function fmtClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtTime(seconds) {
  const totalMs = Math.max(0, Math.round(seconds * 1000));
  const h = Math.floor(totalMs / 3600000);
  const m = Math.floor((totalMs % 3600000) / 60000);
  const s = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")},${String(ms).padStart(3, "0")}`;
}

function parseTime(input) {
  const raw = String(input).trim().replace(",", ".");
  if (!raw) return null;
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const value = parseFloat(raw);
    return Number.isFinite(value) ? value : null;
  }
  const match = raw.match(/^(\d+):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,3}))?)?$/);
  if (!match) return null;
  const h = match[3] !== undefined ? parseInt(match[1], 10) : 0;
  const m = match[3] !== undefined ? parseInt(match[2], 10) : parseInt(match[1], 10);
  const s = match[3] !== undefined ? parseInt(match[3], 10) : parseInt(match[2], 10);
  const ms = match[4] ? parseInt(match[4].padEnd(3, "0"), 10) : 0;
  if (m > 59 && match[3] !== undefined) return null;
  return h * 3600 + m * 60 + s + ms / 1000;
}

function getLangs() {
  const langs = [];
  if (els.langVi.checked) langs.push("vi");
  if (els.langEn.checked) langs.push("en");
  if (els.langZh.checked) langs.push("zh");
  return langs;
}

async function loadHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (data.gpu && data.gpu.available) {
      els.gpuBadge.textContent = `GPU · ${data.gpu.name}`;
      els.gpuBadge.classList.remove("error");
      state.gpuName = data.gpu.name;
    } else {
      els.gpuBadge.textContent = "Chỉ có CPU — kiểm tra CUDA";
      els.gpuBadge.classList.add("error");
    }
    if (data.ffmpeg && data.ffprobe) {
      els.ffmpegBadge.textContent = "ffmpeg OK";
      els.ffmpegBadge.classList.remove("error");
    } else {
      els.ffmpegBadge.textContent = "Thiếu ffmpeg";
      els.ffmpegBadge.classList.add("error");
    }
  } catch (err) {
    els.gpuBadge.textContent = "Không kết nối được server";
    els.gpuBadge.classList.add("error");
  }
}

/* ---------------- settings ---------------- */

function setGeminiState(text, tone = "") {
  els.geminiState.textContent = text;
  els.geminiState.className = `small muted ${tone}`.trim();
}

const MODEL_STATUS_TEXT = {
  quota: "hết hạn mức",
  unavailable: "đang quá tải",
  unreachable: "không kết nối được",
  not_found: "không có trong tài khoản",
  auth: "key không dùng được",
  no_key: "chưa có key",
};

function modelStatusText(item) {
  return MODEL_STATUS_TEXT[item.reason] || "không dùng được";
}

function applyModelStatus(data) {
  state.modelStatus = data;
  const byId = {};
  (data.models || []).forEach((item) => { byId[item.id] = item; });
  Array.from(els.geminiModel.options).forEach((option) => {
    if (!option.dataset.base) option.dataset.base = option.textContent;
    if (!option.dataset.name) option.dataset.name = option.dataset.base.split("—")[0].trim();
    const item = byId[option.value];
    option.title = "";
    if (item && !item.ok) {
      option.classList.add("bad");
      // Cột cài đặt hẹp nên chỉ giữ tên model + lý do, bỏ mô tả dài.
      option.textContent = `${option.dataset.name} — ⚠ ${modelStatusText(item)}`;
      option.title = item.detail || modelStatusText(item);
    } else {
      option.classList.remove("bad");
      option.textContent = option.dataset.base;
    }
  });
  if (!data.key_valid) {
    setGeminiState(`API key không dùng được: ${data.key_error || "key bị từ chối"}.`, "bad");
    return;
  }
  const current = byId[els.geminiModel.value];
  if (current && !current.ok) {
    const hint = data.best_model ? ` Nên chuyển sang ${data.best_model}.` : "";
    setGeminiState(
      `Model đang chọn ${modelStatusText(current)}.` +
        `${hint} Khi dịch gặp lỗi này, app sẽ tự chuyển sang model đang chạy được.`,
      "bad"
    );
    return;
  }
  const good = (data.models || []).filter((item) => item.ok).map((item) => item.id);
  setGeminiState(`Key hợp lệ · model dùng được: ${good.join(" · ")}.`, "ok");
}

async function checkGeminiStatus(refresh = false) {
  els.checkGeminiBtn.disabled = true;
  setGeminiState("Đang kiểm tra API key và model…");
  try {
    const res = await fetch(`/api/gemini/status?probe=1${refresh ? "&refresh=1" : ""}`);
    if (!res.ok) throw new Error("status failed");
    applyModelStatus(await res.json());
  } catch (err) {
    setGeminiState("Không kiểm tra được Gemini (mạng hoặc server lỗi).", "bad");
  } finally {
    els.checkGeminiBtn.disabled = els.geminiModel.disabled;
  }
}

function updateTranslateUI() {
  els.geminiBox.classList.toggle("hidden", !els.translateTo.value);
}

function updateExportUI() {
  els.exportVariant.classList.toggle("hidden", !state.showTrans);
}

function updateTransWarn() {
  const count = state.showTrans ? state.untranslated.length : 0;
  els.transWarn.textContent = count > 0 ? `${count} cue chưa dịch` : "";
  els.transWarn.classList.toggle("hidden", count === 0);
  els.retransAllBtn.classList.toggle("hidden", count === 0);
}

function parseGlossary(text) {
  const pairs = [];
  let skipped = 0;
  for (const line of String(text || "").split(/\r?\n/)) {
    const raw = line.trim();
    if (!raw) continue;
    const match = raw.match(/^(.+?)\s*(?:=|→|->)\s*(.+)$/);
    if (match && match[1].trim() && match[2].trim()) {
      pairs.push({ src: match[1].trim(), dst: match[2].trim() });
    } else {
      skipped += 1;
    }
  }
  return { pairs, skipped };
}

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error("settings failed");
    const data = await res.json();
    state.geminiConfigured = Boolean(data.gemini_configured);
    state.translateModel = data.translate_model || "gemini-3.5-flash-lite";
    els.geminiModel.value = state.translateModel;
    state.dubVoiceLabel = data.dub_voice_label || "Chí Mai";
    state.dubOutputDir = data.dub_output_dir || "D:\\banve";
    els.dubHint.textContent = `Video lồng tiếng xuất vào ${state.dubOutputDir}`;
    state.toolV1Url = data.tool_v1_url || "http://127.0.0.1:8088";
    els.toolV1Url.value = state.toolV1Url;
    els.toolV1State.textContent = "Tool V1 sẽ lồng tiếng Việt và che phụ đề gốc khi render.";
    const glossary = Array.isArray(data.glossary) ? data.glossary : [];
    els.glossaryInput.value = glossary
      .map((item) => `${item.src} = ${item.dst}`)
      .join("\n");
    if (state.geminiConfigured) {
      setGeminiState("Đã lưu API key trên máy.", "ok");
      checkGeminiStatus();
    } else {
      setGeminiState("Chưa có API key — lấy tại aistudio.google.com/apikey.");
    }
  } catch (err) {
    setGeminiState("Không đọc được cấu hình dịch.", "bad");
  }
}

async function saveGeminiSettings() {
  const key = els.geminiKey.value.trim();
  const model = els.geminiModel.value;
  const { pairs, skipped } = parseGlossary(els.glossaryInput.value);
  if (!key && !state.geminiConfigured && !pairs.length) {
    showToast("Nhập Gemini API key trước", "error");
    els.geminiKey.focus();
    return;
  }
  els.saveKeyBtn.disabled = true;
  setGeminiState("Đang lưu…");
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gemini_api_key: key || null, translate_model: model, glossary: pairs }),
    });
    if (!res.ok) throw new Error("save failed");
    const data = await res.json();
    state.geminiConfigured = Boolean(data.gemini_configured);
    state.translateModel = data.translate_model || model;
    els.geminiKey.value = "";
    showToast(
      "Đã lưu cài đặt dịch" + (skipped ? ` (bỏ qua ${skipped} dòng sai định dạng)` : ""),
      skipped ? "error" : "success"
    );
    checkGeminiStatus(Boolean(key));
  } catch (err) {
    setGeminiState("Lưu thất bại.", "bad");
    showToast("Không lưu được cài đặt dịch", "error");
  } finally {
    els.saveKeyBtn.disabled = state.running;
  }
}

async function saveToolV1Url() {
  const url = els.toolV1Url.value.trim();
  els.saveToolV1Btn.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_v1_url: url }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "save failed");
    state.toolV1Url = data.tool_v1_url || url || "http://127.0.0.1:8088";
    els.toolV1Url.value = state.toolV1Url;
    els.toolV1State.textContent = `Sẽ gửi sang ${state.toolV1Url}`;
    els.toolV1State.className = "small muted";
    showToast("Đã lưu địa chỉ Tool V1", "success");
  } catch (err) {
    els.toolV1State.textContent = `Không lưu được: ${String(err.message || err).slice(0, 120)}`;
    els.toolV1State.className = "small bad";
    showToast("Địa chỉ Tool V1 không hợp lệ", "error");
  } finally {
    els.saveToolV1Btn.disabled = false;
  }
}

/* ---------------- handoff sang Tool V1 ---------------- */

const HANDOFF_ACTIVE = ["queued", "running", "cancelling"];

function stopHandoffPolling() {
  if (state.handoffTimer) {
    clearTimeout(state.handoffTimer);
    state.handoffTimer = null;
  }
}

function renderHandoff(record) {
  state.handoff = record || null;
  state.handoffFails = 0;
  const panel = els.handoffPanel;
  if (!record) {
    stopHandoffPolling();
    panel.classList.add("hidden");
    panel.classList.remove("ok", "bad");
    els.handoffNote.textContent = "";
    els.handoffOut.textContent = "";
    els.handoffBtn.textContent = "Gửi sang Tool V1 · lồng tiếng + che sub";
    els.handoffBtn.disabled = state.running || state.handoffSending;
    return;
  }

  const status = String(record.status || "queued");
  const active = HANDOFF_ACTIVE.includes(status);
  const pct = Math.max(0, Math.min(100, Number(record.progress) || 0));
  panel.classList.remove("hidden");
  panel.classList.toggle("ok", status === "done");
  panel.classList.toggle("bad", status === "error" || status === "cancelled");
  els.handoffBar.style.width = status === "done" ? "100%" : `${pct}%`;
  els.handoffPct.textContent = status === "done" ? "100%" : `${pct}%`;
  els.handoffStage.textContent = record.stage || "Đang chờ Tool V1 phản hồi…";
  els.handoffCancelBtn.classList.toggle("hidden", !active || status === "cancelling");

  const notes = [];
  if (record.cue_count) notes.push(`${record.cue_count} câu`);
  if (record.translated_count != null) notes.push(`${record.translated_count} bản dịch`);
  const result = record.result || {};
  if (status === "done") {
    const bits = [];
    if (result.voice) bits.push(`giọng ${result.voice}`);
    if (result.voiced_count != null) bits.push(`lồng tiếng ${result.voiced_count} câu`);
    if ((result.failed_cues || []).length) bits.push(`lỗi ${result.failed_cues.length} câu`);
    if (result.seconds != null) bits.push(`${result.seconds}s`);
    notes.push(bits.join(" · "));
  }
  const problem = record.error || record.unreachable;
  els.handoffNote.textContent = problem ? problem : notes.join(" · ");
  els.handoffOut.textContent = status === "done" ? String(result.delivered || result.final_video || "") : "";

  els.handoffBtn.textContent = active
    ? "Đang render ở Tool V1…"
    : status === "done" ? "Gửi lại sang Tool V1" : "Gửi sang Tool V1 · lồng tiếng + che sub";
  els.handoffBtn.disabled = state.running || state.handoffSending || active;

  if (active) pollHandoff();
  else stopHandoffPolling();
}

function pollHandoff(delay = 2000) {
  stopHandoffPolling();
  if (!state.job) return;
  const jobId = state.job.id;
  state.handoffTimer = setTimeout(async () => {
    state.handoffTimer = null;
    if (!state.job || state.job.id !== jobId) return;
    try {
      const res = await fetch(`/api/jobs/${jobId}/handoff`);
      const data = await res.json().catch(() => ({}));
      if (res.ok) {
        renderHandoff(data.handoff);
        return;
      }
      state.handoffFails += 1;
    } catch (err) {
      state.handoffFails += 1;
    }
    // Tool V1 đang bận render có thể chậm phản hồi: thử lại thưa dần rồi dừng.
    if (state.handoffFails < 5) pollHandoff(5000);
    else stopHandoffPolling();
  }, delay);
}

async function sendHandoff() {
  if (!state.job || state.running || state.handoffSending) return;
  const translated = state.cues.filter((cue) => (cue.trans || "").trim()).length;
  if (!translated && !window.confirm(
    "Chưa có bản dịch tiếng Việt cho video này.\n\nTool V1 vẫn render và che phụ đề gốc nhưng sẽ không có giọng đọc Việt. Gửi tiếp?"
  )) return;
  if (state.saveTimer) await saveCues(true);

  state.handoffSending = true;
  els.handoffBtn.disabled = true;
  els.handoffBtn.textContent = "Đang gửi…";
  try {
    const res = await fetch(`/api/jobs/${state.job.id}/handoff`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_lang: els.translateTo.value || null }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Tool V1 không phản hồi (${res.status})`);
    renderHandoff(data.handoff);
    showToast("Đã gửi sang Tool V1 — đang lồng tiếng và che phụ đề gốc", "success");
  } catch (err) {
    const message = String(err.message || err).slice(0, 200);
    // Lần gửi hỏng: bỏ trạng thái cũ (có thể còn hiện "Hoàn tất" của lần trước) và báo lỗi ngay trên panel.
    renderHandoff({ ...(state.handoff || {}), status: "error", stage: "Không gửi được sang Tool V1", error: message });
    showToast(message, "error");
  } finally {
    state.handoffSending = false;
    els.handoffBtn.disabled = state.running || Boolean(state.handoff && HANDOFF_ACTIVE.includes(state.handoff.status));
  }
}

/* ---------------- upload ---------------- */

function uploadFile(file) {
  if (!file || state.uploading) return;
  state.uploading = true;
  els.uploadProgress.classList.remove("hidden");
  els.uploadBar.style.width = "0%";
  els.uploadText.textContent = `Đang tải lên ${file.name}…`;

  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (event) => {
    if (event.lengthComputable) {
      const pct = Math.round((event.loaded / event.total) * 100);
      els.uploadBar.style.width = `${pct}%`;
      els.uploadText.textContent = `Đang tải lên… ${pct}%`;
    }
  };
  xhr.onload = () => {
    state.uploading = false;
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const job = JSON.parse(xhr.responseText);
        openJob(job, false);
      } catch (err) {
        showToast("Phản hồi không hợp lệ từ server", "error");
      }
    } else {
      let message = "Tải video thất bại";
      try {
        const body = JSON.parse(xhr.responseText);
        if (body.detail) message = body.detail;
      } catch (err) {}
      showToast(message, "error");
      els.uploadText.textContent = "Tải thất bại";
    }
  };
  xhr.onerror = () => {
    state.uploading = false;
    showToast("Không kết nối được server", "error");
  };
  xhr.send(form);
}

/* ---------------- job lifecycle ---------------- */

function openJob(job, restored) {
  closeEvents();
  state.job = job;
  state.cues = job.cues || [];
  state.activeCue = -1;
  state.undoStack = [];
  updateUndoUI();
  state.untranslated = (job.status === "done" && job.stats && job.stats.untranslated_indices) || [];
  state.showTrans = Boolean(
    (job.params && job.params.translate_to) || state.cues.some((cue) => cue.trans)
  );
  if (job.params && job.params.translate_to) {
    els.translateTo.value = job.params.translate_to;
  }
  updateTranslateUI();
  updateExportUI();
  if (state.showTrans) els.exportVariant.value = "translated";
  try {
    localStorage.setItem("subx_job", job.id);
  } catch (err) {}

  els.uploadCard.classList.add("hidden");
  els.workspace.classList.remove("hidden");
  els.resultCard.classList.add("hidden");
  els.fileName.textContent = job.filename;
  const info = job.info || {};
  els.fileMeta.textContent = [
    info.duration ? fmtClock(info.duration) : null,
    info.width && info.height ? `${info.width}×${info.height}` : null,
    info.codec ? info.codec.toUpperCase() : null,
  ].filter(Boolean).join(" · ");

  state.rois = parseRois(job.params && job.params.roi);
  state.activeRoi = 0;
  renderRoi();
  updateDubUI(job);
  renderHandoff((job.stats || {}).handoff);

  els.videoError.classList.add("hidden");
  els.previewState.textContent = "";
  els.video.src = `/api/jobs/${job.id}/video${restored ? `?v=${Date.now()}` : ""}`;
  els.video.load();

  setRunning(job.status === "running");
  els.progressPanel.classList.toggle("hidden", !["running", "done", "error", "cancelled"].includes(job.status));

  if (job.status === "running") {
    els.stageLabel.textContent = STAGE_LABELS[job.stage] || "Đang xử lý…";
    updateProgress(job);
    listenJob();
  } else if (job.status === "done") {
    els.progressPanel.classList.remove("hidden");
    els.progressBar.style.width = "100%";
    els.progressPct.textContent = "100%";
    els.stageLabel.textContent = "Hoàn tất";
    els.progressMsg.textContent = job.message || "";
    state.untranslated = (job.stats && job.stats.untranslated_indices) || [];
    renderCues();
    updateTransWarn();
    if (job.stats) showStats(job.stats);
  } else if (job.status === "error") {
    els.stageLabel.textContent = "Lỗi";
    els.progressMsg.textContent = job.error || job.message || "";
  } else {
    els.stageLabel.textContent = "Sẵn sàng";
  }

  requestAnimationFrame(() => {
    positionRoiLayer();
    setTimeout(positionRoiLayer, 250);
  });
}

function startJob() {
  if (!state.job || state.running) return;
  const langs = getLangs();
  if (!langs.length) {
    showToast("Chọn ít nhất một ngôn ngữ phụ đề", "error");
    return;
  }
  const translateTo = els.translateTo.value;
  if (translateTo && !state.geminiConfigured) {
    els.geminiKey.focus();
    showToast("Lưu Gemini API key trong Cài đặt trước khi dịch", "error");
    return;
  }

  state.cues = [];
  state.showTrans = Boolean(translateTo);
  state.untranslated = [];
  state.undoStack = [];
  updateUndoUI();
  updateExportUI();
  updateTransWarn();
  els.resultCard.classList.add("hidden");
  els.cueList.innerHTML = "";
  els.saveState.textContent = "";
  els.progressPanel.classList.remove("hidden");
  els.progressBar.style.width = "0%";
  els.stageLabel.textContent = "Đang bắt đầu…";
  els.progressPct.textContent = "0%";
  els.progressMsg.textContent = "";
  els.statsLine.textContent = "";
  renderHandoff(null);
  setRunning(true);

  fetch(`/api/jobs/${state.job.id}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      roi: state.rois.map((r) => [r.x, r.y, r.w, r.h]),
      sample_fps: parseFloat(els.fpsSelect.value),
      langs,
      min_conf: parseInt(els.confRange.value, 10) / 100,
      translate_to: translateTo,
      dub: els.dubEnabled.checked,
    }),
  })
    .then(async (res) => {
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || "Không bắt đầu được job");
      }
      listenJob();
    })
    .catch((err) => {
      setRunning(false);
      els.stageLabel.textContent = "Lỗi";
      els.progressMsg.textContent = err.message;
      showToast(err.message, "error");
    });
}

function setRunning(running) {
  state.running = running;
  els.startBtn.classList.toggle("hidden", running);
  els.cancelBtn.classList.toggle("hidden", !running);
  els.cancelBtn.disabled = false;
  els.startBtn.disabled = running;
  els.roiSelectBtn.disabled = running;
  els.roiDup.disabled = running || state.rois.length >= MAX_ROIS;
  els.roiDel.disabled = running || state.rois.length <= 1;
  els.translateTo.disabled = running;
  els.geminiKey.disabled = running;
  els.geminiModel.disabled = running;
  els.saveKeyBtn.disabled = running;
  els.checkGeminiBtn.disabled = running;
  els.dubEnabled.disabled = running;
  els.addCueBtn.disabled = running;
  els.handoffBtn.disabled = running || state.handoffSending
    || Boolean(state.handoff && HANDOFF_ACTIVE.includes(String(state.handoff.status)));
  els.roiLayer.classList.toggle("running", running);
  updateUndoUI();
  if (running) {
    state.selecting = false;
    state.drag = null;
    els.roiLayer.classList.remove("selecting");
    els.roiSelectBtn.classList.remove("active");
    els.roiSelectBtn.textContent = "Chọn vùng phụ đề";
    els.roiHint.classList.add("hidden");
  }
}

function updateProgress(snapshot) {
  if (!snapshot) return;
  els.stageLabel.textContent = STAGE_LABELS[snapshot.stage] || "Đang xử lý…";
  els.progressMsg.textContent = snapshot.message || "";
  const pct = Math.round((snapshot.progress || 0) * 100);
  els.progressBar.style.width = `${pct}%`;
  els.progressPct.textContent = `${pct}%`;
}

function showStats(stats) {
  const bits = [];
  if (stats.cues != null) bits.push(`${stats.cues} cue`);
  if (stats.detected_groups != null) bits.push(`${stats.detected_groups} đoạn chữ`);
  if (stats.translated_cues) bits.push(`${stats.translated_cues} bản dịch`);
  if (stats.untranslated_cues) bits.push(`chưa dịch ${stats.untranslated_cues}`);
  if (stats.dub_cues) bits.push(`lồng tiếng ${stats.dub_cues} câu`);
  if (stats.total_seconds != null) bits.push(`${stats.total_seconds}s`);
  bits.push(stats.gpu ? "GPU" : "CPU");
  els.statsLine.textContent = bits.join(" · ");
}

function updateDubUI(job) {
  const stats = (job && job.stats) || {};
  const output = String(stats.dub_output || "");
  const label = String(stats.dub_voice_label || state.dubVoiceLabel || "Chí Mai");
  els.downloadDubBtn.classList.toggle("hidden", !output);
  if (output) {
    els.downloadDubBtn.href = `/api/jobs/${job.id}/dubbed`;
  } else {
    els.downloadDubBtn.removeAttribute("href");
  }
  if (output) {
    els.dubResult.textContent = `Video lồng tiếng ${label}: ${output}`;
    els.dubResult.classList.remove("hidden");
  } else if (stats.dub_enabled && stats.dub_error) {
    els.dubResult.textContent = `Lỗi lồng tiếng: ${stats.dub_error}`;
    els.dubResult.classList.remove("hidden");
  } else {
    els.dubResult.textContent = "";
    els.dubResult.classList.add("hidden");
  }
}

function closeEvents() {
  if (state.events) {
    state.events.close();
    state.events = null;
  }
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function listenJob() {
  closeEvents();
  if (!state.job) return;
  const es = new EventSource(`/api/jobs/${state.job.id}/events`);
  state.events = es;
  es.onmessage = (event) => {
    let snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    els.progressPanel.classList.remove("hidden");
    updateProgress(snapshot);
    if (snapshot.status === "done" || snapshot.status === "error" || snapshot.status === "cancelled") {
      closeEvents();
      onJobFinished(snapshot);
    }
  };
  es.onerror = () => {
    if (!state.running) return;
    es.close();
    state.events = null;
    pollJob();
  };
}

function pollJob() {
  if (!state.job || state.pollTimer) return;
  const tick = async () => {
    state.pollTimer = null;
    if (!state.job) return;
    try {
      const res = await fetch(`/api/jobs/${state.job.id}`);
      const snapshot = await res.json();
      if (res.ok) {
        updateProgress(snapshot);
        if (["done", "error", "cancelled"].includes(snapshot.status)) {
          onJobFinished(snapshot);
          return;
        }
      }
    } catch (err) {}
    if (state.running) state.pollTimer = setTimeout(tick, 1200);
  };
  state.pollTimer = setTimeout(tick, 800);
}

async function onJobFinished(snapshot) {
  setRunning(false);
  if (snapshot.status === "done") {
    try {
      const res = await fetch(`/api/jobs/${state.job.id}`);
      const job = await res.json();
      state.job = job;
      state.cues = job.cues || [];
      state.showTrans = Boolean(
        (job.params && job.params.translate_to) || state.cues.some((cue) => cue.trans)
      );
      updateExportUI();
      if (state.showTrans) els.exportVariant.value = "translated";
      els.stageLabel.textContent = "Hoàn tất";
      els.progressBar.style.width = "100%";
      els.progressPct.textContent = "100%";
      els.progressMsg.textContent = job.message || `Đã tạo ${state.cues.length} cue.`;
      if (job.stats) showStats(job.stats);
      updateDubUI(job);
      renderCues();
      state.untranslated = (job.stats && job.stats.untranslated_indices) || [];
      updateTransWarn();
      els.resultCard.classList.remove("hidden");
      els.resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
      const translated = state.cues.filter((cue) => cue.trans).length;
      if (job.stats && job.stats.translate_error) {
        showToast(`Đã bóc phụ đề nhưng dịch lỗi: ${job.stats.translate_error}`, "error");
      } else if (
        job.stats && job.stats.translate_total === 0 &&
        job.params && job.params.translate_to
      ) {
        const labels = { vi: "tiếng Việt", en: "English", zh: "tiếng Trung" };
        const label = labels[job.params.translate_to] || job.params.translate_to;
        showToast(`Phụ đề gốc đã là ${label} nên không có bản dịch — chọn đích khác nếu muốn dịch`, "error");
      } else {
        showToast(
          `Xong: ${state.cues.length} cue` + (translated ? ` · ${translated} bản dịch` : ""),
          "success"
        );
      }
    } catch (err) {
      showToast("Không tải được kết quả", "error");
    }
  } else if (snapshot.status === "cancelled") {
    els.stageLabel.textContent = "Đã hủy";
    els.progressMsg.textContent = "Bạn có thể chạy lại với cài đặt khác.";
    showToast("Đã hủy xử lý");
  } else if (snapshot.status === "error") {
    els.stageLabel.textContent = "Lỗi";
    els.progressMsg.textContent = snapshot.error || snapshot.message || "Xử lý thất bại";
    showToast(snapshot.error || "Xử lý thất bại", "error");
  }
}

function cancelJob() {
  if (!state.job || !state.running) return;
  els.cancelBtn.disabled = true;
  fetch(`/api/jobs/${state.job.id}/cancel`, { method: "POST" }).catch(() => {});
  showToast("Đang hủy…");
}

function resetWorkspace() {
  closeEvents();
  clearTimeout(state.saveTimer);
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.job = null;
  state.cues = [];
  state.running = false;
  renderHandoff(null);
  state.selecting = false;
  state.drag = null;
  state.activeCue = -1;
  state.showTrans = false;
  state.untranslated = [];
  state.retranslating = false;
  state.rois = DEFAULT_ROIS.map((r) => ({ ...r }));
  state.activeRoi = 0;
  updateExportUI();
  updateTransWarn();
  try {
    localStorage.removeItem("subx_job");
  } catch (err) {}
  els.video.pause();
  els.video.removeAttribute("src");
  els.video.load();
  els.fileInput.value = "";
  els.uploadProgress.classList.add("hidden");
  els.uploadBar.style.width = "0%";
  els.workspace.classList.add("hidden");
  els.uploadCard.classList.remove("hidden");
  els.resultCard.classList.add("hidden");
  els.progressPanel.classList.add("hidden");
  els.translateTo.disabled = false;
  els.geminiKey.disabled = false;
  els.geminiModel.disabled = false;
  els.saveKeyBtn.disabled = false;
  els.checkGeminiBtn.disabled = false;
  els.roiLayer.classList.remove("selecting", "running");
  els.roiHint.classList.add("hidden");
  els.roiSelectBtn.classList.remove("active");
  els.startBtn.classList.remove("hidden");
  els.cancelBtn.classList.add("hidden");
  els.stageLabel.textContent = "Sẵn sàng";
}

/* ---------------- ROI ---------------- */

const MIN_ROI = 0.02;
const MAX_ROIS = 4;

function parseRois(raw) {
  if (Array.isArray(raw) && raw.length && Array.isArray(raw[0])) {
    const rois = raw
      .filter((r) => Array.isArray(r) && r.length === 4 && r.every((v) => Number.isFinite(Number(v))))
      .map(([x, y, w, h]) => ({ x: Number(x), y: Number(y), w: Number(w), h: Number(h) }));
    if (rois.length) return rois.slice(0, MAX_ROIS);
  }
  if (Array.isArray(raw) && raw.length === 4 && raw.every((v) => Number.isFinite(Number(v)))) {
    return [{ x: Number(raw[0]), y: Number(raw[1]), w: Number(raw[2]), h: Number(raw[3]) }];
  }
  return DEFAULT_ROIS.map((r) => ({ ...r }));
}

const ROI_HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

function renderRoi() {
  $$(".roi-box", els.roiLayer).forEach((el) => el.remove());
  state.rois.forEach((r, index) => {
    const box = document.createElement("div");
    box.className = `roi-box${index === state.activeRoi ? " active" : ""}${r.y < 0.14 ? " top" : ""}`;
    box.dataset.index = String(index);
    box.style.left = `${r.x * 100}%`;
    box.style.top = `${r.y * 100}%`;
    box.style.width = `${r.w * 100}%`;
    box.style.height = `${r.h * 100}%`;
    const label = document.createElement("span");
    label.className = "roi-label";
    label.textContent = state.rois.length > 1 ? `Vùng ${index + 1}` : "Vùng phụ đề";
    box.appendChild(label);
    ROI_HANDLES.forEach((dir) => {
      const handle = document.createElement("span");
      handle.className = "roi-handle";
      handle.dataset.dir = dir;
      box.appendChild(handle);
    });
    els.roiLayer.appendChild(box);
  });
  const a = state.rois[state.activeRoi] || state.rois[0];
  els.roiText.textContent =
    `${state.rois.length} vùng · đang chọn: x ${Math.round(a.x * 100)}% · y ${Math.round(a.y * 100)}% · ` +
    `${Math.round(a.w * 100)}% × ${Math.round(a.h * 100)}%`;
  els.roiDel.disabled = state.rois.length <= 1;
  els.roiDup.disabled = state.rois.length >= MAX_ROIS;
}

function positionRoiLayer() {
  if (!els.video.videoWidth || !els.video.videoHeight) return;
  const wrapRect = els.videoWrap.getBoundingClientRect();
  const videoRect = els.video.getBoundingClientRect();
  const scale = Math.min(videoRect.width / els.video.videoWidth, videoRect.height / els.video.videoHeight);
  const width = els.video.videoWidth * scale;
  const height = els.video.videoHeight * scale;
  const left = videoRect.left - wrapRect.left + (videoRect.width - width) / 2;
  const top = videoRect.top - wrapRect.top + (videoRect.height - height) / 2;
  els.roiLayer.style.left = `${left}px`;
  els.roiLayer.style.top = `${top}px`;
  els.roiLayer.style.width = `${width}px`;
  els.roiLayer.style.height = `${height}px`;
}

function setSelecting(selecting) {
  if (state.running) return;
  state.selecting = selecting;
  els.roiLayer.classList.toggle("selecting", selecting);
  els.roiSelectBtn.classList.toggle("active", selecting);
  els.roiHint.classList.toggle("hidden", !selecting);
  els.roiSelectBtn.textContent = selecting ? "Xong" : "Chọn vùng phụ đề";
}

function roiPoint(event) {
  const rect = els.roiLayer.getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
    y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
  };
}

/* ---------------- cues ---------------- */

function renderCues() {
  els.cueList.innerHTML = "";
  els.cueCount.textContent = `${state.cues.length} cue`;
  els.resultCard.classList.toggle("hidden", state.cues.length === 0 && !state.job);
  if (!state.cues.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Không tìm thấy phụ đề trong vùng đã chọn. Thử mở rộng vùng hoặc tăng độ nhạy.";
    els.cueList.appendChild(empty);
    state.activeCue = -1;
    return;
  }

  const frag = document.createDocumentFragment();
  state.cues.forEach((cue, index) => {
    const row = document.createElement("div");
    row.className = "cue";
    row.dataset.index = String(index);

    const play = document.createElement("button");
    play.type = "button";
    play.className = "icon-btn play-btn";
    play.title = "Tua tới cue này";
    play.innerHTML = PLAY_ICON;

    const body = document.createElement("div");
    body.className = "cue-body";

    const times = document.createElement("div");
    times.className = "cue-times";
    const startInput = document.createElement("input");
    startInput.className = "time-input mono";
    startInput.value = fmtTime(cue.start);
    startInput.dataset.field = "start";
    startInput.spellcheck = false;
    const arrow = document.createElement("span");
    arrow.textContent = "→";
    const endInput = document.createElement("input");
    endInput.className = "time-input mono";
    endInput.value = fmtTime(cue.end);
    endInput.dataset.field = "end";
    endInput.spellcheck = false;
    times.append(startInput, arrow, endInput);

    const text = document.createElement("textarea");
    text.className = "cue-text";
    text.rows = 1;
    text.value = cue.text;
    text.spellcheck = false;
    text.title = "Nhấp để sửa nội dung gốc";
    text.placeholder = "Nội dung phụ đề…";

    body.append(times, text);

    if (state.showTrans) {
      const trans = document.createElement("textarea");
      trans.className = "cue-text cue-trans";
      trans.rows = 1;
      trans.value = cue.trans || "";
      trans.placeholder = "Bản dịch — để trống sẽ dùng bản gốc khi xuất";
      trans.spellcheck = false;
      trans.title = "Nhấp để sửa bản dịch";
      trans.dataset.field = "trans";
      body.appendChild(trans);
    }

    const actions = document.createElement("div");
    actions.className = "cue-actions";
    if (state.showTrans) {
      const retrans = document.createElement("button");
      retrans.type = "button";
      retrans.className = "icon-btn retrans-btn";
      retrans.title = "Dịch lại cue này bằng Gemini";
      retrans.innerHTML = RETRANS_ICON;
      actions.appendChild(retrans);
    }
    const split = document.createElement("button");
    split.type = "button";
    split.className = "icon-btn split-btn";
    split.title = "Tách dòng tại vị trí con trỏ";
    split.innerHTML = SPLIT_ICON;
    const merge = document.createElement("button");
    merge.type = "button";
    merge.className = "icon-btn merge-btn";
    merge.title = index < state.cues.length - 1 ? "Gộp với dòng dưới" : "Không có dòng dưới để gộp";
    merge.disabled = index >= state.cues.length - 1;
    merge.innerHTML = MERGE_ICON;
    actions.append(split, merge);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-btn del-btn";
    del.title = "Xóa cue";
    del.innerHTML = TRASH_ICON;
    actions.appendChild(del);

    row.append(play, body, actions);
    frag.appendChild(row);
  });
  els.cueList.appendChild(frag);
  $$(".cue-text", els.cueList).forEach(autoGrow);
}

function autoGrow(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight + 2}px`;
}

function findMatches(needle) {
  if (!needle) return 0;
  const lower = needle.toLowerCase();
  let count = 0;
  state.cues.forEach((cue) => {
    for (const field of ["text", "trans"]) {
      const value = String(cue[field] || "");
      let pos = value.toLowerCase().indexOf(lower);
      while (pos !== -1) {
        count += 1;
        pos = value.toLowerCase().indexOf(lower, pos + lower.length);
      }
    }
  });
  return count;
}

function updateFindCount() {
  if (!els.findCount) return;
  const needle = (els.findInput.value || "").trim();
  els.findCount.textContent = needle ? `${findMatches(needle)} chỗ khớp` : "";
}

function replaceAllInCues() {
  const needle = (els.findInput.value || "").trim();
  const replacement = els.replaceInput.value || "";
  if (!needle) {
    showToast("Nhập nội dung cần tìm trước", "error");
    els.findInput.focus();
    return;
  }
  const lower = needle.toLowerCase();
  let count = 0;
  const replaceIn = (value) => {
    let out = "";
    let rest = String(value || "");
    let pos = rest.toLowerCase().indexOf(lower);
    while (pos !== -1) {
      count += 1;
      out += rest.slice(0, pos) + replacement;
      rest = rest.slice(pos + lower.length);
      pos = rest.toLowerCase().indexOf(lower);
    }
    return out + rest;
  };
  snapshotCues("tìm & thay");
  state.cues.forEach((cue) => {
    cue.text = replaceIn(cue.text);
    if (cue.trans) cue.trans = replaceIn(cue.trans);
  });
  renderCues();
  markDirty();
  showToast(count ? `Đã thay ${count} chỗ` : "Không tìm thấy nội dung nào khớp", count ? "success" : "error");
}

function markDirty() {
  els.saveState.textContent = "Chưa lưu…";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveCues, 700);
}

async function saveCues(immediate = false) {
  if (!state.job) return;
  clearTimeout(state.saveTimer);
  state.saveTimer = null;
  const cues = state.cues
    .filter((cue) => cue.text.trim().length > 0)
    .map((cue) => ({
      start: cue.start,
      end: cue.end,
      text: cue.text,
      trans: cue.trans || "",
      region: Number.isInteger(cue.region) ? cue.region : null,
    }));
  els.saveState.textContent = "Đang lưu…";
  try {
    const res = await fetch(`/api/jobs/${state.job.id}/cues`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cues }),
    });
    if (!res.ok) throw new Error("save failed");
    els.saveState.textContent = "Đã lưu";
    setTimeout(() => {
      if (els.saveState.textContent === "Đã lưu") els.saveState.textContent = "";
    }, 2200);
  } catch (err) {
    els.saveState.textContent = "Lưu thất bại";
    showToast("Không lưu được chỉnh sửa", "error");
  }
}

function seekTo(seconds) {
  if (!Number.isFinite(seconds)) return;
  els.video.currentTime = Math.min(seconds, els.video.duration || seconds);
  els.video.focus();
}

function handleCueInput(event) {
  const target = event.target;
  const row = target.closest(".cue");
  if (!row) return;
  const index = Number(row.dataset.index);
  const cue = state.cues[index];
  if (!cue) return;

  if (target.classList.contains("time-input")) {
    const value = parseTime(target.value);
    if (value === null) {
      target.classList.add("invalid");
      return;
    }
    target.classList.remove("invalid");
    cue[target.dataset.field] = Math.max(0, value);
    if (cue.end <= cue.start) {
      cue.end = cue.start + 0.2;
      const endInput = $('.time-input[data-field="end"]', row);
      if (endInput) endInput.value = fmtTime(cue.end);
    }
    markDirty();
  } else if (target.classList.contains("cue-trans")) {
    cue.trans = target.value;
    autoGrow(target);
    markDirty();
  } else if (target.classList.contains("cue-text")) {
    cue.text = target.value;
    autoGrow(target);
    markDirty();
  }
}

function handleTimeBlur(event) {
  const target = event.target;
  if (!target.classList.contains("time-input")) return;
  const row = target.closest(".cue");
  const index = Number(row.dataset.index);
  const cue = state.cues[index];
  if (!cue) return;
  const value = parseTime(target.value);
  if (value === null) {
    target.value = fmtTime(cue[target.dataset.field]);
    target.classList.remove("invalid");
    return;
  }
  target.value = fmtTime(cue[target.dataset.field]);
}

function handleCueClick(event) {
  const row = event.target.closest(".cue");
  if (!row) return;
  const index = Number(row.dataset.index);
  const cue = state.cues[index];
  if (!cue) return;
  if (event.target.closest(".del-btn")) return;
  const isPlay = Boolean(event.target.closest(".play-btn"));
  if (!isPlay && event.target.closest("button, input, textarea")) return;
  seekTo(cue.start);
  if (isPlay) {
    els.video.play().catch(() => {});
  }
}

function snapshotCues(label) {
  state.undoStack.push({
    cues: state.cues.map((cue) => ({ ...cue })),
    untranslated: [...state.untranslated],
    label,
  });
  if (state.undoStack.length > 40) state.undoStack.shift();
  updateUndoUI();
}

function updateUndoUI() {
  const top = state.undoStack[state.undoStack.length - 1];
  els.undoBtn.classList.toggle("hidden", !top);
  els.undoBtn.disabled = !top || state.running;
  els.undoBtn.textContent = top ? `↶ Hoàn tác ${top.label}` : "↶ Hoàn tác";
  els.undoBtn.title = top ? `Hoàn tác ${top.label} (Ctrl+Z)` : "Hoàn tác thao tác dòng gần nhất (Ctrl+Z)";
}

function undoEdit() {
  const entry = state.undoStack.pop();
  if (!entry || state.running) return;
  state.cues = entry.cues.map((cue) => ({ ...cue }));
  state.untranslated = [...entry.untranslated];
  renderCues();
  updateTransWarn();
  updateUndoUI();
  markDirty();
  showToast(`Đã hoàn tác ${entry.label}`);
}

function captureUntranslated() {
  return new Set(state.untranslated.map((i) => state.cues[i]).filter(Boolean));
}

function restoreUntranslated(keep, extra = []) {
  if (!state.showTrans) return;
  const indices = new Set(state.cues.map((cue, index) => (keep.has(cue) ? index : -1)).filter((index) => index >= 0));
  for (const index of extra) {
    if (index >= 0 && index < state.cues.length) indices.add(index);
  }
  state.untranslated = [...indices].sort((a, b) => a - b);
  updateTransWarn();
}

function cueDuration() {
  const fromStats = Number(state.job && state.job.stats && state.job.stats.duration);
  if (Number.isFinite(fromStats) && fromStats > 0) return fromStats;
  return Number.isFinite(els.video.duration) ? els.video.duration : 0;
}

function addCue() {
  if (!state.job || state.running) return;
  snapshotCues("thêm dòng");
  const anchorIndex = state.activeCue >= 0 ? state.activeCue : state.cues.length - 1;
  const anchor = state.cues[anchorIndex] || null;
  const duration = cueDuration();
  let start = anchor ? anchor.end + 0.15 : 0;
  if (duration > 0.5) start = Math.min(start, duration - 0.4);
  let end = start + 1.5;
  if (duration > 0.5) end = Math.min(end, duration);
  if (end - start < 0.3) end = start + 0.3;
  const cue = {
    start: round3(start), end: round3(end), text: "", trans: "",
    region: anchor && Number.isInteger(anchor.region) ? anchor.region : null,
  };
  state.cues.splice(anchorIndex + 1, 0, cue);
  renderCues();
  markDirty();
  const index = anchorIndex + 1;
  const ta = $(`.cue[data-index="${index}"] .cue-text`, els.cueList);
  if (ta) {
    ta.focus();
    ta.scrollIntoView({ block: "nearest" });
  }
}

function round3(value) {
  return Math.round(value * 1000) / 1000;
}

function splitTextAt(text, ratio) {
  const raw = String(text || "");
  if (!raw.trim()) return ["", ""];
  let cut = Math.round(raw.length * ratio);
  const window = Math.max(4, Math.round(raw.length * 0.2));
  let best = -1;
  for (let i = Math.max(1, cut - window); i < Math.min(raw.length, cut + window); i += 1) {
    if (/\s/.test(raw[i]) && (best < 0 || Math.abs(i - cut) < Math.abs(best - cut))) best = i;
  }
  if (best > 0) cut = best;
  return [raw.slice(0, cut).trim(), raw.slice(cut).trim()];
}

function splitCue(event) {
  const button = event.target.closest(".split-btn");
  if (!button || state.running) return;
  const row = button.closest(".cue");
  const index = Number(row.dataset.index);
  const cue = state.cues[index];
  const ta = $(".cue-text", row);
  if (!cue || !ta) return;
  const caret = ta.selectionStart ?? 0;
  const left = ta.value.slice(0, caret).trimEnd();
  const right = ta.value.slice(caret).trimStart();
  if (!left || !right) {
    showToast("Đặt con trỏ vào giữa dòng rồi bấm tách", "error");
    ta.focus();
    return;
  }
  if (cue.end - cue.start < 0.4) {
    showToast("Dòng quá ngắn để tách — hãy sửa thời gian trước", "error");
    return;
  }
  const keep = captureUntranslated();
  const wasUntranslated = keep.has(cue);
  snapshotCues("tách dòng");
  const ratio = left.length / (left.length + right.length);
  const mid = round3(Math.min(cue.end - 0.2, Math.max(cue.start + 0.2, cue.start + (cue.end - cue.start) * ratio)));
  const [transLeft, transRight] = splitTextAt(cue.trans || "", ratio);
  state.cues.splice(index, 1,
    { ...cue, end: mid, text: left, trans: transLeft },
    { start: mid, end: cue.end, text: right, trans: transRight, region: cue.region });
  renderCues();
  restoreUntranslated(keep, wasUntranslated ? [index, index + 1] : []);
  markDirty();
  const rightTa = $(`.cue[data-index="${index + 1}"] .cue-trans`, els.cueList)
    || $(`.cue[data-index="${index + 1}"] .cue-text`, els.cueList);
  if (rightTa) {
    rightTa.focus();
    if (typeof rightTa.setSelectionRange === "function") rightTa.setSelectionRange(0, 0);
  }
}

function mergeCue(event) {
  const button = event.target.closest(".merge-btn");
  if (!button || state.running) return;
  const row = button.closest(".cue");
  const index = Number(row.dataset.index);
  const cue = state.cues[index];
  const next = state.cues[index + 1];
  if (!cue || !next) return;
  const keep = captureUntranslated();
  const wasUntranslated = keep.has(cue) && keep.has(next);
  snapshotCues("gộp dòng");
  const join = (a, b) => [String(a || "").trim(), String(b || "").trim()].filter(Boolean).join(" ");
  state.cues.splice(index, 2, {
    ...cue,
    end: next.end,
    text: join(cue.text, next.text),
    trans: join(cue.trans, next.trans),
  });
  renderCues();
  restoreUntranslated(keep, wasUntranslated ? [index] : []);
  markDirty();
}

function deleteCue(event) {
  const button = event.target.closest(".del-btn");
  if (!button) return;
  const row = button.closest(".cue");
  const index = Number(row.dataset.index);
  if (state.running) return;
  const keep = captureUntranslated();
  snapshotCues("xóa dòng");
  state.cues.splice(index, 1);
  renderCues();
  restoreUntranslated(keep);
  markDirty();
}

async function retranslateCueCore(index) {
  const res = await fetch(`/api/jobs/${state.job.id}/retranslate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    const cue = state.cues[index];
    if (cue) cue.trans = data.trans || "";
    const ta = $(`.cue[data-index="${index}"] .cue-trans`);
    if (ta) {
      ta.value = data.trans || "";
      autoGrow(ta);
    }
    state.untranslated = state.untranslated.filter((i) => i !== index);
    updateTransWarn();
    return;
  }
  throw new Error(data.detail || "Dịch lại thất bại");
}

async function retranslateCue(index, btn) {
  if (!state.job || state.running || state.retranslating) return;
  await saveCues(true);
  btn.disabled = true;
  btn.classList.add("spin");
  try {
    await retranslateCueCore(index);
    showToast("Đã dịch lại cue", "success");
  } catch (err) {
    showToast(err.message || "Dịch lại thất bại", "error");
  } finally {
    btn.disabled = false;
    btn.classList.remove("spin");
  }
}

async function retranslateMissing() {
  if (!state.job || state.running || state.retranslating) return;
  await saveCues(true);
  state.retranslating = true;
  els.retransAllBtn.disabled = true;
  const list = [...state.untranslated];
  let okCount = 0;
  let failed = 0;
  for (let i = 0; i < list.length; i += 1) {
    els.retransAllBtn.textContent = `Đang dịch lại ${i + 1}/${list.length}…`;
    try {
      await retranslateCueCore(list[i]);
      okCount += 1;
    } catch (err) {
      failed += 1;
    }
  }
  state.retranslating = false;
  els.retransAllBtn.disabled = false;
  els.retransAllBtn.textContent = "Dịch lại chỗ thiếu";
  updateTransWarn();
  showToast(
    `Dịch lại xong: ${okCount} cue` + (failed ? ` · lỗi ${failed}` : ""),
    failed ? "error" : "success"
  );
}

function handleCueBlur(event) {
  if (event.target.classList.contains("cue-trans")) {
    const row = event.target.closest(".cue");
    const cue = state.cues[Number(row.dataset.index)];
    if (cue) cue.trans = event.target.value;
    return;
  }
  if (!event.target.classList.contains("cue-text")) return;
  const row = event.target.closest(".cue");
  const index = Number(row.dataset.index);
  const cue = state.cues[index];
  if (!cue) return;
  if (!event.target.value.trim()) {
    snapshotCues("xóa dòng");
    state.cues.splice(index, 1);
    renderCues();
    markDirty();
    return;
  }
  cue.text = event.target.value;
}

function updateActiveCue() {
  const time = els.video.currentTime;
  let index = -1;
  for (let i = 0; i < state.cues.length; i += 1) {
    if (time >= state.cues[i].start && time < state.cues[i].end) {
      index = i;
      break;
    }
  }
  if (index === state.activeCue) return;
  const rows = els.cueList.children;
  if (state.activeCue >= 0 && rows[state.activeCue]) rows[state.activeCue].classList.remove("active");
  if (index >= 0 && rows[index]) rows[index].classList.add("active");
  state.activeCue = index;
}

/* ---------------- export ---------------- */

async function exportCues(format) {
  if (!state.job) return;
  if (!state.cues.length) {
    showToast("Chưa có phụ đề để xuất", "error");
    return;
  }
  await saveCues(true);
  const variant = state.showTrans ? els.exportVariant.value : "original";
  const link = document.createElement("a");
  link.href = `/api/jobs/${state.job.id}/export?format=${format}&variant=${variant}&v=${Date.now()}`;
  link.download = "";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/* ---------------- video fallback ---------------- */

async function requestPreview() {
  if (!state.job) return;
  els.previewBtn.disabled = true;
  els.previewState.textContent = "Đang tạo bản xem trước…";
  try {
    await fetch(`/api/jobs/${state.job.id}/preview`, { method: "POST" });
    pollPreview();
  } catch (err) {
    els.previewBtn.disabled = false;
    els.previewState.textContent = "Không gửi được yêu cầu.";
  }
}

async function pollPreview() {
  if (!state.job) return;
  try {
    const res = await fetch(`/api/jobs/${state.job.id}`);
    const job = await res.json();
    if (job.preview_status === "ready") {
      els.videoError.classList.add("hidden");
      els.previewBtn.disabled = false;
      els.previewState.textContent = "";
      els.video.src = `/api/jobs/${job.id}/video?preview=1&v=${Date.now()}`;
      els.video.load();
      showToast("Đã tạo bản xem trước", "success");
      return;
    }
    if (job.preview_status === "error") {
      els.previewBtn.disabled = false;
      els.previewState.textContent = "Tạo bản xem trước thất bại.";
      return;
    }
    els.previewState.textContent = "Đang chuyển mã, vui lòng chờ…";
  } catch (err) {}
  state.previewTimer = setTimeout(pollPreview, 1500);
}

/* ---------------- events ---------------- */

function bindEvents() {
  els.pickBtn.addEventListener("click", () => els.fileInput.click());
  els.fileInput.addEventListener("change", () => {
    if (els.fileInput.files && els.fileInput.files[0]) uploadFile(els.fileInput.files[0]);
  });

  ["dragenter", "dragover"].forEach((name) => {
    els.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      els.dropZone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    els.dropZone.addEventListener(name, (event) => {
      event.preventDefault();
      els.dropZone.classList.remove("drag");
    });
  });
  els.dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) uploadFile(file);
  });

  els.newVideoBtn.addEventListener("click", () => {
    if (state.running) {
      if (!window.confirm("Video đang được xử lý. Dừng lại và chọn video khác?")) return;
      cancelJob();
    }
    resetWorkspace();
  });

  els.video.addEventListener("loadedmetadata", positionRoiLayer);
  window.addEventListener("resize", positionRoiLayer);
  if (window.ResizeObserver) {
    new ResizeObserver(positionRoiLayer).observe(els.video);
  }
  els.video.addEventListener("timeupdate", updateActiveCue);
  els.video.addEventListener("error", () => {
    if (els.video.getAttribute("src")) els.videoError.classList.remove("hidden");
  });
  els.previewBtn.addEventListener("click", requestPreview);

  els.roiSelectBtn.addEventListener("click", () => setSelecting(!state.selecting));
  els.roiReset.addEventListener("click", () => {
    state.rois = DEFAULT_ROIS.map((r) => ({ ...r }));
    state.activeRoi = 0;
    renderRoi();
  });
  els.roiFull.addEventListener("click", () => {
    state.rois = [{ x: 0, y: 0, w: 1, h: 1 }];
    state.activeRoi = 0;
    renderRoi();
  });
  els.roiDup.addEventListener("click", () => {
    if (state.running || state.rois.length >= MAX_ROIS) return;
    const base = state.rois[state.activeRoi] || state.rois[0];
    // Nhân bản sang dải đối diện: vùng đang ở đáy thì lên đỉnh, đang ở đỉnh thì xuống đáy.
    const clone = { x: base.x, w: base.w, h: base.h };
    clone.y = base.y > 0.5 ? 0.02 : Math.min(0.98 - base.h, 1 - base.h - 0.02);
    state.rois.push(clone);
    state.activeRoi = state.rois.length - 1;
    renderRoi();
    showToast("Đã nhân bản vùng — kéo về chỗ phụ đề nằm", "success");
  });
  els.roiDel.addEventListener("click", () => {
    if (state.running || state.rois.length <= 1) return;
    state.rois.splice(state.activeRoi, 1);
    state.activeRoi = Math.max(0, Math.min(state.activeRoi, state.rois.length - 1));
    renderRoi();
  });

  els.roiLayer.addEventListener("pointerdown", (event) => {
    if (state.running) return;
    const handle = event.target.closest(".roi-handle");
    const box = event.target.closest(".roi-box");
    if (!state.selecting && !handle) {
      if (box) {
        state.activeRoi = Number(box.dataset.index);
        renderRoi();
      }
      return;
    }
    event.preventDefault();
    els.video.pause();
    const point = roiPoint(event);
    if (handle && box) {
      const index = Number(box.dataset.index);
      state.activeRoi = index;
      renderRoi();
      state.drag = {
        mode: "resize",
        index,
        dir: handle.dataset.dir || "",
        roi: { ...state.rois[index] },
        x0: point.x,
        y0: point.y,
      };
    } else {
      // Vẽ thêm một vùng mới (không thay thế các vùng đã có)
      state.rois.push({ x: point.x, y: point.y, w: 0.01, h: 0.01 });
      state.activeRoi = state.rois.length - 1;
      renderRoi();
      state.drag = { mode: "draw", index: state.activeRoi, x0: point.x, y0: point.y };
    }
    els.roiLayer.setPointerCapture(event.pointerId);
  });
  els.roiLayer.addEventListener("pointermove", (event) => {
    if (!state.drag) return;
    const point = roiPoint(event);
    const drag = state.drag;
    if (drag.mode === "resize") {
      let { x, y, w, h } = drag.roi;
      const dx = point.x - drag.x0;
      const dy = point.y - drag.y0;
      if (drag.dir.includes("w")) {
        const nx = Math.min(x + w - MIN_ROI, Math.max(0, x + dx));
        w += x - nx;
        x = nx;
      }
      if (drag.dir.includes("e")) {
        w = Math.min(1 - x, Math.max(MIN_ROI, w + dx));
      }
      if (drag.dir.includes("n")) {
        const ny = Math.min(y + h - MIN_ROI, Math.max(0, y + dy));
        h += y - ny;
        y = ny;
      }
      if (drag.dir.includes("s")) {
        h = Math.min(1 - y, Math.max(MIN_ROI, h + dy));
      }
      state.rois[drag.index] = { x, y, w, h };
      renderRoi();
      return;
    }
    const x = Math.min(drag.x0, point.x);
    const y = Math.min(drag.y0, point.y);
    const w = Math.abs(point.x - drag.x0);
    const h = Math.abs(point.y - drag.y0);
    state.rois[drag.index] = { x, y, w: Math.max(0.01, w), h: Math.max(0.01, h) };
    renderRoi();
  });
  els.roiLayer.addEventListener("pointerup", (event) => {
    if (!state.drag) return;
    const mode = state.drag.mode;
    const index = state.drag.index;
    state.drag = null;
    try {
      els.roiLayer.releasePointerCapture(event.pointerId);
    } catch (err) {}
    if (mode === "draw") {
      const drawn = state.rois[index];
      if (!drawn || drawn.w < MIN_ROI || drawn.h < MIN_ROI) {
        state.rois.splice(index, 1);
        state.activeRoi = Math.max(0, Math.min(state.activeRoi, state.rois.length - 1));
        showToast("Vùng chọn quá nhỏ, đã bỏ", "error");
      } else {
        state.activeRoi = index;
      }
      setSelecting(false);
      renderRoi();
    }
  });
  els.roiLayer.addEventListener("pointercancel", () => {
    state.drag = null;
  });

  els.translateTo.addEventListener("change", updateTranslateUI);
  els.saveKeyBtn.addEventListener("click", saveGeminiSettings);
  els.checkGeminiBtn.addEventListener("click", () => checkGeminiStatus(true));
  els.geminiModel.addEventListener("change", () => {
    if (state.geminiConfigured) saveGeminiSettings();
  });
  els.glossaryInput.addEventListener("change", () => {
    if (state.geminiConfigured) saveGeminiSettings();
  });
  els.geminiKey.addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveGeminiSettings();
  });

  els.confRange.addEventListener("input", () => {
    els.confVal.textContent = `${els.confRange.value}%`;
  });

  els.startBtn.addEventListener("click", startJob);
  els.cancelBtn.addEventListener("click", cancelJob);

  els.cueList.addEventListener("input", handleCueInput);
  els.cueList.addEventListener("click", handleCueClick);
  els.cueList.addEventListener("click", deleteCue);
  els.cueList.addEventListener("click", splitCue);
  els.cueList.addEventListener("click", mergeCue);
  els.cueList.addEventListener("click", (event) => {
    const btn = event.target.closest(".retrans-btn");
    if (!btn) return;
    const row = btn.closest(".cue");
    if (row) retranslateCue(Number(row.dataset.index), btn);
  });
  els.cueList.addEventListener("blur", handleTimeBlur, true);
  els.findInput.addEventListener("input", updateFindCount);
  els.replaceAllBtn.addEventListener("click", replaceAllInCues);
  els.cueList.addEventListener("blur", handleCueBlur, true);

  els.addCueBtn.addEventListener("click", addCue);
  els.undoBtn.addEventListener("click", undoEdit);
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || String(event.key).toLowerCase() !== "z") return;
    if (!state.undoStack.length) return;
    const tag = (event.target && event.target.tagName || "").toLowerCase();
    if (tag === "textarea" || tag === "input") return;
    event.preventDefault();
    undoEdit();
  });

  els.retransAllBtn.addEventListener("click", retranslateMissing);

  els.exportSrt.addEventListener("click", () => exportCues("srt"));
  els.exportVtt.addEventListener("click", () => exportCues("vtt"));

  els.saveToolV1Btn.addEventListener("click", saveToolV1Url);
  els.toolV1Url.addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveToolV1Url();
  });
  els.handoffBtn.addEventListener("click", sendHandoff);
  els.handoffCloseBtn.addEventListener("click", () => renderHandoff(null));
  els.handoffCancelBtn.addEventListener("click", async () => {
    if (!state.job || !state.handoff || String(state.handoff.status) === "cancelling") return;
    els.handoffCancelBtn.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${state.job.id}/handoff/cancel`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Không hủy được render");
      renderHandoff(data.handoff);
      showToast("Đã yêu cầu Tool V1 dừng render", "success");
    } catch (err) {
      showToast(String(err.message || err).slice(0, 200), "error");
    } finally {
      els.handoffCancelBtn.disabled = false;
    }
  });
}

async function restoreLastJob() {
  let id = null;
  try {
    id = localStorage.getItem("subx_job");
  } catch (err) {}
  if (!id) return;
  try {
    const res = await fetch(`/api/jobs/${id}`);
    if (!res.ok) {
      localStorage.removeItem("subx_job");
      return;
    }
    const job = await res.json();
    if (job.status === "uploaded") {
      localStorage.removeItem("subx_job");
      return;
    }
    openJob(job, true);
  } catch (err) {}
}

function init() {
  if (new URLSearchParams(location.search).get("embed") === "1") {
    document.body.classList.add("embed");
  }
  bindEvents();
  loadHealth();
  loadSettings();
  updateTranslateUI();
  restoreLastJob();
}

init();
