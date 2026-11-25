/* VocalForge — CapCut PC-style Audio Editor & 9h+ Demucs Separator */

(() => {
  "use strict";

  const TRACK_HEIGHT = 72;
  const HANDLE_PX = 9;
  const MAX_HISTORY = 50;
  const TRACK_COLORS = [
    "#00d2d3",
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#ec4899",
    "#8b5cf6",
    "#f97316",
  ];

  // ---------------- State ----------------
  const state = {
    env: null,
    setupDismissed: false,
    mediaPool: [], // [{ id, name, path, duration, peaks, tag, isVideo }]
    selectedMediaIds: new Set(),
    importedJobIds: new Set(),
    jobs: [],
    tracks: [], // [{ id, name, color, volume, muted, solo, clips: [...] }]
    selectedTrackId: null,
    selectedClipId: null,
    selectedClipIds: new Set(),
    timelineMarquee: null, // { x1, y1, x2, y2 }
    activeVideoPath: null,
    stickerPool: [], // [{ id, name, path, url }]
    stageOverlays: [], // [{ id, name, path, url, xPct, yPct, widthPct, opacity }]
    selectedOverlayId: null,
    undoStack: [],
    redoStack: [],
    snapEnabled: true,
    playhead: 0,
    pxPerSec: 12,
    isPlaying: false,
    playStartWall: 0,
    playStartHead: 0,
    audioPool: new Map(),
    drag: null,
    exportTaskId: null,
    lastExportPath: null,
  };

  // ---------------- Helpers ----------------
  const $ = (id) => document.getElementById(id);
  const uid = () => Math.random().toString(36).slice(2, 10);
  const isVideoPath = (p) => /\.(mp4|mkv|mov|webm|avi|m4v)$/i.test(String(p || ""));
  const round3 = (n) => Math.round(n * 1000) / 1000;

  function formatTime(sec, withTenths = true) {
    const s = Math.max(0, Number(sec) || 0);
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const secs = Math.floor(s % 60);
    const tenths = Math.floor((s % 1) * 10);
    const pad = (n) => String(n).padStart(2, "0");
    const base = `${pad(hrs)}:${pad(mins)}:${pad(secs)}`;
    return withTenths ? `${base}.${tenths}` : base;
  }

  function formatEta(sec) {
    if (sec == null || !isFinite(sec)) return "";
    if (sec < 60) return `~${Math.ceil(sec)}s`;
    const m = Math.floor(sec / 60);
    const h = Math.floor(m / 60);
    return h > 0 ? `~${h}h${m % 60}p` : `~${m}p`;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function toast(msg, type = "info") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    $("toast-container").appendChild(el);
    setTimeout(() => el.remove(), 3400);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function hasPyWebView() {
    return Boolean(window.pywebview && window.pywebview.api);
  }

  async function uploadBrowserFile(file) {
    const fd = new FormData();
    fd.append("file", file);
    const data = await api("/api/audio/upload", { method: "POST", body: fd });
    return data.path;
  }

  async function resolveDroppedFiles(fileList) {
    const paths = [];
    for (const f of Array.from(fileList || [])) {
      if (f.pywebviewFullPath) {
        paths.push(f.pywebviewFullPath);
      } else if (f.path) {
        paths.push(f.path);
      } else {
        toast(`Đang nạp ${f.name}...`);
        paths.push(await uploadBrowserFile(f));
      }
    }
    return paths;
  }

  // ---------------- Undo / Redo History ----------------
  function cloneTracksSnapshot() {
    return state.tracks.map((tr) => ({
      ...tr,
      clips: tr.clips.map((c) => ({ ...c })), // peaks array is immutable, safe to keep ref
    }));
  }

  function pushHistory() {
    state.undoStack.push({
      tracks: cloneTracksSnapshot(),
      selectedClipId: state.selectedClipId,
      selectedTrackId: state.selectedTrackId,
    });
    if (state.undoStack.length > MAX_HISTORY) state.undoStack.shift();
    state.redoStack.length = 0;
    updateHistoryButtons();
  }

  function undo() {
    if (!state.undoStack.length) return;
    stopPlayback();
    state.redoStack.push({
      tracks: cloneTracksSnapshot(),
      selectedClipId: state.selectedClipId,
      selectedTrackId: state.selectedTrackId,
    });
    const prev = state.undoStack.pop();
    state.tracks = prev.tracks;
    state.selectedClipId = prev.selectedClipId;
    state.selectedTrackId = prev.selectedTrackId;
    afterTimelineMutated();
    toast("Đã hoàn tác (Undo)");
  }

  function redo() {
    if (!state.redoStack.length) return;
    stopPlayback();
    state.undoStack.push({
      tracks: cloneTracksSnapshot(),
      selectedClipId: state.selectedClipId,
      selectedTrackId: state.selectedTrackId,
    });
    const next = state.redoStack.pop();
    state.tracks = next.tracks;
    state.selectedClipId = next.selectedClipId;
    state.selectedTrackId = next.selectedTrackId;
    afterTimelineMutated();
    toast("Đã làm lại (Redo)");
  }

  function updateHistoryButtons() {
    $("btn-undo").disabled = state.undoStack.length === 0;
    $("btn-redo").disabled = state.redoStack.length === 0;
  }

  function afterTimelineMutated() {
    updateHistoryButtons();
    renderTrackControls();
    updateInspector();
    updateProjectSummary();
    renderTimeline();
  }

  $("btn-undo").addEventListener("click", undo);
  $("btn-redo").addEventListener("click", redo);

  // ---------------- Panel Tabs (Media / Tách giọng AI) ----------------
  document.querySelectorAll(".panel-tab").forEach((btn) => {
    btn.addEventListener("click", () => switchLeftTab(btn.dataset.ptab));
  });

  function switchLeftTab(name) {
    document.querySelectorAll(".panel-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.ptab === name);
    });
    document.querySelectorAll(".panel-content").forEach((c) => {
      c.classList.toggle("active", c.id === `ptab-${name}`);
    });
  }

  // ---------------- Auto-Setup Environment ----------------
  async function refreshEnvironment(openModalIfMissing = false) {
    try {
      const env = await api("/api/env");
      state.env = env;
      updateEnvUI(env);
      if (!env.ready && openModalIfMissing && !state.setupDismissed) {
        $("setup-modal").classList.remove("hidden");
      }
      if (env.installer.running) {
        setTimeout(() => refreshEnvironment(false), 900);
      }
    } catch (_) {}
  }

  function updateEnvUI(env) {
    const pill = $("btn-env-status");
    const pillText = $("env-status-text");
    pill.classList.remove("checking", "ready", "missing");

    if (env.installer.running) {
      pill.classList.add("checking");
      pillText.textContent = `Đang cài ${Math.round(env.installer.progress)}%`;
    } else if (env.ready) {
      pill.classList.add("ready");
      pillText.textContent = env.components.ai_runtime.cuda_enabled ? "GPU CUDA" : "CPU sẵn sàng";
    } else {
      pill.classList.add("missing");
      pillText.textContent = "Cần tải AI";
    }

    if (env.gpu.has_gpu) {
      const vram = env.gpu.vram_mb ? ` (${(env.gpu.vram_mb / 1024).toFixed(1)} GB VRAM)` : "";
      $("chk-gpu-desc").textContent = `Phát hiện: ${env.gpu.name}${vram}`;
    } else {
      $("chk-gpu-desc").textContent = "Không thấy GPU NVIDIA — sẽ dùng CPU";
    }

    const ffOk = env.components.ffmpeg.installed;
    $("chk-ffmpeg-badge").textContent = ffOk ? "Đã cài" : "Chưa có";
    $("chk-ffmpeg-badge").className = `check-badge ${ffOk ? "ok" : "missing"}`;

    const aiOk = env.components.ai_runtime.installed;
    $("chk-ai-badge").textContent = aiOk ? "Đã cài" : "Chưa có";
    $("chk-ai-badge").className = `check-badge ${aiOk ? "ok" : "missing"}`;

    const inst = env.installer;
    const showProg = inst.running || inst.stage === "done" || inst.stage === "error";
    $("setup-progress-box").classList.toggle("hidden", !showProg);
    if (showProg) {
      $("setup-message").textContent = inst.message || "Đang xử lý...";
      $("setup-pct").textContent = `${Math.round(inst.progress)}%`;
      $("setup-bar-fill").style.width = `${inst.progress}%`;
      const logsEl = $("setup-logs");
      logsEl.textContent = (inst.logs || []).join("\n");
      logsEl.scrollTop = logsEl.scrollHeight;
    }

    $("btn-run-setup").disabled = inst.running;
    $("btn-cancel-setup").classList.toggle("hidden", !inst.running);

    if (inst.stage === "done" && env.ready && !$("setup-modal").classList.contains("hidden")) {
      toast("Cài đặt môi trường AI hoàn tất!", "success");
      setTimeout(() => $("setup-modal").classList.add("hidden"), 800);
    }
  }

  $("btn-env-status").addEventListener("click", () => {
    refreshEnvironment(false);
    $("setup-modal").classList.remove("hidden");
  });
  $("btn-close-setup").addEventListener("click", () => {
    state.setupDismissed = true;
    $("setup-modal").classList.add("hidden");
  });
  $("btn-skip-setup").addEventListener("click", () => {
    state.setupDismissed = true;
    $("setup-modal").classList.add("hidden");
  });
  $("btn-run-setup").addEventListener("click", async () => {
    try {
      await api("/api/env/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force_cpu: $("chk-force-cpu").checked }),
      });
      refreshEnvironment(false);
    } catch (err) {
      toast(err.message, "error");
    }
  });
  $("btn-cancel-setup").addEventListener("click", async () => {
    await api("/api/env/cancel", { method: "POST" });
  });

  // Shortcuts Modal
  $("btn-shortcuts-modal").addEventListener("click", () => $("shortcuts-modal").classList.remove("hidden"));
  $("btn-close-shortcuts").addEventListener("click", () => $("shortcuts-modal").classList.add("hidden"));

  // ---------------- CapCut Media Pool ----------------
  async function importFilesToMediaPool(paths, tag = null, autoAddToTimeline = false) {
    for (const p of paths) {
      const existing = state.mediaPool.find((m) => m.path === p);
      if (existing) {
        if (autoAddToTimeline) addMediaItemToTimeline(existing);
        continue;
      }
      try {
        toast(`Đang nạp: ${p.split(/[/\\]/).pop()}...`);
        const data = await api(`/api/audio/peaks?path=${encodeURIComponent(p)}&points=2400`);
        const isVid = isVideoPath(data.path || data.name);
        const item = {
          id: uid(),
          name: data.name,
          path: data.path,
          duration: data.duration,
          peaks: data.peaks,
          tag: tag || (isVid ? "VIDEO" : null),
          isVideo: isVid,
        };
        state.mediaPool.unshift(item);
        if (isVid && !state.activeVideoPath) {
          setStageVideoSource(item.path);
        }
        renderMediaPool();
        if (autoAddToTimeline) {
          addMediaItemToTimeline(item);
        }
      } catch (err) {
        toast(`Lỗi nạp file: ${err.message}`, "error");
      }
    }
  }

  function updateMediaDeleteButton() {
    const btn = $("btn-delete-media");
    const n = state.selectedMediaIds.size;
    btn.classList.toggle("hidden", n === 0);
    btn.textContent = n > 1 ? `🗑 Xóa (${n})` : "🗑 Xóa";
  }

  function deleteSelectedMediaItems() {
    if (!state.selectedMediaIds.size) return false;
    const count = state.selectedMediaIds.size;
    state.mediaPool = state.mediaPool.filter((m) => !state.selectedMediaIds.has(m.id));
    state.selectedMediaIds.clear();
    if (state.activeVideoPath && !state.mediaPool.some((m) => m.path === state.activeVideoPath)) {
      const nextVid = state.mediaPool.find((m) => m.isVideo);
      setStageVideoSource(nextVid ? nextVid.path : null);
    }
    renderMediaPool();
    toast(`Đã xóa ${count} tệp khỏi Khay Media.`);
    return true;
  }

  $("btn-delete-media").addEventListener("click", deleteSelectedMediaItems);

  function drawMiniWaveform(canvas, peaks) {
    const ctx = canvas.getContext("2d");
    const w = (canvas.width = 160);
    const h = (canvas.height = 62);
    ctx.clearRect(0, 0, w, h);
    if (!peaks || !peaks.length) return;
    ctx.fillStyle = "rgba(0, 210, 211, 0.72)";
    const bars = 46;
    const step = peaks.length / bars;
    const mid = h / 2;
    for (let i = 0; i < bars; i++) {
      const val = peaks[Math.min(peaks.length - 1, Math.floor(i * step))] || 0.05;
      const bh = Math.max(3, val * (h - 14));
      ctx.fillRect(i * (w / bars) + 1, mid - bh / 2, 2.2, bh);
    }
  }

  function renderMediaPool() {
    const count = state.mediaPool.length;
    $("media-count").textContent = `${count} tệp`;
    $("media-empty").classList.toggle("hidden", count > 0);
    $("media-grid").classList.toggle("hidden", count === 0);
    updateMediaDeleteButton();

    const grid = $("media-grid");
    grid.innerHTML = "";
    for (const item of state.mediaPool) {
      const card = document.createElement("div");
      card.className = `media-card ${state.selectedMediaIds.has(item.id) ? "selected" : ""}`;
      card.dataset.mediaId = item.id;
      card.draggable = true;
      card.title = `${item.name}\nClick chọn (Backspace để xóa) • Kéo xuống Timeline hoặc bấm ＋`;

      card.innerHTML = `
        <div class="media-thumb">
          <canvas></canvas>
          ${item.tag ? `<span class="media-stem-tag">${escapeHtml(item.tag)}</span>` : ""}
          <span class="media-dur-tag">${formatTime(item.duration, false)}</span>
          <button class="media-sep-btn" title="Tách giọng AI (Demucs)">✨ Tách</button>
          <button class="media-add-btn" title="Thêm xuống Timeline (＋)">＋</button>
        </div>
        <div class="media-name">${escapeHtml(item.name)}</div>
      `;

      drawMiniWaveform(card.querySelector("canvas"), item.peaks);

      card.addEventListener("click", (e) => {
        e.stopPropagation();
        state.selectedClipId = null;
        state.selectedClipIds.clear();
        state.selectedOverlayId = null;
        if (e.ctrlKey || e.metaKey) {
          if (state.selectedMediaIds.has(item.id)) state.selectedMediaIds.delete(item.id);
          else state.selectedMediaIds.add(item.id);
        } else {
          state.selectedMediaIds.clear();
          state.selectedMediaIds.add(item.id);
        }
        if (item.isVideo) setStageVideoSource(item.path);
        renderMediaPool();
        updateInspector();
        renderTimeline();
      });

      card.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("application/x-vocalforge-media", item.id);
      });

      card.querySelector(".media-add-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        if (item.isVideo) setStageVideoSource(item.path);
        addMediaItemToTimeline(item);
      });

      card.querySelector(".media-sep-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        triggerSeparateForPath(item.path);
      });

      grid.appendChild(card);
    }
  }

  // Marquee drag-to-select multiple files in Media Pool
  let mediaMarquee = null;
  const mediaWrap = $("media-dropzone");
  const mediaMarqueeEl = $("media-marquee");
  mediaWrap.addEventListener("mousedown", (e) => {
    if (e.target.closest(".media-card") || e.target.closest(".media-empty")) return;
    const rect = mediaWrap.getBoundingClientRect();
    const x = e.clientX - rect.left + mediaWrap.scrollLeft;
    const y = e.clientY - rect.top + mediaWrap.scrollTop;
    mediaMarquee = { x1: x, y1: y, x2: x, y2: y };
    if (!e.ctrlKey && !e.metaKey) state.selectedMediaIds.clear();
    mediaMarqueeEl.classList.remove("hidden");
  });
  window.addEventListener("mousemove", (e) => {
    if (!mediaMarquee) return;
    const rect = mediaWrap.getBoundingClientRect();
    mediaMarquee.x2 = e.clientX - rect.left + mediaWrap.scrollLeft;
    mediaMarquee.y2 = e.clientY - rect.top + mediaWrap.scrollTop;
    const left = Math.min(mediaMarquee.x1, mediaMarquee.x2);
    const top = Math.min(mediaMarquee.y1, mediaMarquee.y2);
    const width = Math.abs(mediaMarquee.x2 - mediaMarquee.x1);
    const height = Math.abs(mediaMarquee.y2 - mediaMarquee.y1);
    Object.assign(mediaMarqueeEl.style, { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` });

    const selRect = mediaMarqueeEl.getBoundingClientRect();
    $("media-grid").querySelectorAll(".media-card").forEach((card) => {
      const cr = card.getBoundingClientRect();
      const overlap = !(cr.right < selRect.left || cr.left > selRect.right || cr.bottom < selRect.top || cr.top > selRect.bottom);
      if (overlap) state.selectedMediaIds.add(card.dataset.mediaId);
    });
    $("media-grid").querySelectorAll(".media-card").forEach((card) => {
      card.classList.toggle("selected", state.selectedMediaIds.has(card.dataset.mediaId));
    });
    updateMediaDeleteButton();
  });
  window.addEventListener("mouseup", () => {
    if (mediaMarquee) {
      mediaMarquee = null;
      mediaMarqueeEl.classList.add("hidden");
    }
  });

  async function pickMediaFiles() {
    if (hasPyWebView()) {
      const picked = await window.pywebview.api.pick_audio_files(true);
      if (picked && picked.length) await importFilesToMediaPool(picked);
    } else {
      $("input-import-media").click();
    }
  }

  $("btn-import-media").addEventListener("click", pickMediaFiles);
  $("media-empty").addEventListener("click", pickMediaFiles);
  $("input-import-media").addEventListener("change", async (e) => {
    const paths = await resolveDroppedFiles(e.target.files);
    await importFilesToMediaPool(paths);
    e.target.value = "";
  });

  const mediaDrop = $("media-dropzone");
  mediaDrop.addEventListener("dragover", (e) => {
    e.preventDefault();
    mediaDrop.classList.add("dragover");
  });
  mediaDrop.addEventListener("dragleave", () => mediaDrop.classList.remove("dragover"));
  mediaDrop.addEventListener("drop", async (e) => {
    e.preventDefault();
    mediaDrop.classList.remove("dragover");
    if (e.dataTransfer.getData("application/x-vocalforge-media")) return;
    try {
      const paths = await resolveDroppedFiles(e.dataTransfer.files);
      await importFilesToMediaPool(paths);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------- Demucs Separation Queue ----------------
  async function triggerSeparateForPath(filePath) {
    if (state.env && !state.env.ready) {
      $("setup-modal").classList.remove("hidden");
      toast("Hãy bấm 'Tải & Cài tự động' trước khi tách giọng.", "error");
      return;
    }
    try {
      await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input_path: filePath,
          stems: $("sep-stems").value,
          model: $("sep-model").value,
          chunk_duration: Number($("sep-chunk").value),
          output_format: $("sep-format").value,
          device: "auto",
        }),
      });
      switchLeftTab("demucs");
      toast("Đã bắt đầu tách giọng bằng Demucs!", "success");
      refreshJobs();
    } catch (err) {
      toast(`Không thể tách: ${err.message}`, "error");
    }
  }

  async function refreshJobs() {
    try {
      const data = await api("/api/jobs");
      state.jobs = data.jobs || [];
      renderJobs();

      // Auto-add completed job stems to Media Pool once
      for (const job of state.jobs) {
        if (job.status === "completed" && job.output_files && !state.importedJobIds.has(job.id)) {
          state.importedJobIds.add(job.id);
          for (const [stem, outPath] of Object.entries(job.output_files)) {
            const label = stem === "vocals" ? "Vocals" : stem === "no_vocals" ? "Beat" : stem;
            importFilesToMediaPool([outPath], label, false);
          }
        }
      }
    } catch (_) {}
  }

  function renderJobs() {
    const activeCount = state.jobs.filter((j) => ["running", "merging", "queued"].includes(j.status)).length;
    const badge = $("jobs-badge");
    badge.textContent = String(activeCount);
    badge.classList.toggle("hidden", activeCount === 0);

    const container = $("jobs-list");
    if (!state.jobs.length) {
      container.innerHTML = `<div class="empty-jobs">Bấm nút <b>✨ Tách</b> trên bất kỳ tệp nào trong tab <b>Media</b> để tách giọng. Tự động lưu từng khúc, hỗ trợ Tạm dừng / Chạy tiếp.</div>`;
      return;
    }

    container.innerHTML = "";
    for (const job of state.jobs) {
      const card = document.createElement("div");
      card.className = "job-card";
      const eta = job.status === "running" && job.eta_seconds ? ` • ${formatEta(job.eta_seconds)}` : "";
      card.innerHTML = `
        <div class="job-top">
          <div class="job-name" title="${escapeHtml(job.input_name)}">${escapeHtml(job.input_name)}</div>
          <span class="job-badge ${job.status}">${job.status === "completed" ? "Xong" : job.status === "running" ? "Đang tách" : job.status === "paused" ? "Tạm dừng" : job.status}</span>
        </div>
        <div class="job-meta">${formatTime(job.total_duration, false)} • ${job.completed_chunks.length}/${job.total_chunks} khúc${eta}</div>
        <div class="progress-bar"><div class="progress-fill" style="width:${Math.min(100, job.progress || 0)}%"></div></div>
        <div class="job-actions"></div>
      `;

      const actions = card.querySelector(".job-actions");
      if (job.status === "running") {
        actions.appendChild(mkBtn("⏸ Tạm dừng", "btn-soft btn-xs", async () => {
          await api(`/api/jobs/${job.id}/pause`, { method: "POST" });
          refreshJobs();
        }));
      }
      if (job.status === "paused" || job.status === "failed") {
        actions.appendChild(mkBtn("▶ Chạy tiếp", "btn-cyan btn-xs", async () => {
          await api(`/api/jobs/${job.id}/resume`, { method: "POST" });
          refreshJobs();
        }));
      }
      if (job.status === "completed" && job.output_files) {
        actions.appendChild(mkBtn("＋ Xuống Timeline", "btn-cyan btn-xs", async () => {
          for (const [stem, p] of Object.entries(job.output_files)) {
            const tag = stem === "vocals" ? "Vocals" : stem === "no_vocals" ? "Beat" : stem;
            await importFilesToMediaPool([p], tag, true);
          }
          zoomToFit();
        }));
        actions.appendChild(mkBtn("📂 Thư mục", "btn-soft btn-xs", async () => {
          await api("/api/system/open-folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: job.output_dir }),
          });
        }));
      }
      actions.appendChild(mkBtn("🗑", "btn-soft btn-xs", async () => {
        await api(`/api/jobs/${job.id}`, { method: "DELETE" });
        refreshJobs();
      }));

      container.appendChild(card);
    }
  }

  function mkBtn(label, cls, fn) {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", fn);
    return b;
  }

  // ---------------- Timeline Core ----------------
  const rulerCanvas = $("ruler-canvas");
  const timelineCanvas = $("timeline-canvas");
  const scrollArea = $("canvas-scroll-area");
  const trackControlsList = $("track-controls-list");

  function addMediaItemToTimeline(item, startAt = 0) {
    pushHistory();
    const color = TRACK_COLORS[state.tracks.length % TRACK_COLORS.length];
    const clipId = uid();
    const track = {
      id: uid(),
      name: item.tag ? `[${item.tag}] ${item.name}` : item.name,
      color,
      volume: 1.0,
      muted: false,
      solo: false,
      clips: [
        {
          id: clipId,
          name: item.name,
          path: item.path,
          duration: item.duration,
          peaks: item.peaks,
          startTime: Math.max(0, round3(startAt)),
          trimStart: 0,
          trimEnd: item.duration,
          volume: 1.0,
          fadeIn: 0,
          fadeOut: 0,
        },
      ],
    };
    state.tracks.push(track);
    state.selectedTrackId = track.id;
    state.selectedClipId = clipId;
    afterTimelineMutated();
    if (state.tracks.length === 1) zoomToFit();
  }

  function getTimelineContentEnd() {
    let maxT = 0;
    for (const tr of state.tracks) {
      for (const c of tr.clips) {
        const end = c.startTime + (c.trimEnd - c.trimStart);
        if (end > maxT) maxT = end;
      }
    }
    return maxT;
  }

  function getTimelineCanvasDuration() {
    return Math.max(30, getTimelineContentEnd() * 1.12);
  }

  function findClipById(clipId) {
    if (!clipId) return null;
    for (const tr of state.tracks) {
      const idx = tr.clips.findIndex((c) => c.id === clipId);
      if (idx !== -1) return { track: tr, clip: tr.clips[idx], index: idx };
    }
    return null;
  }

  function findTargetClipForCut() {
    // Prefer selected clip; otherwise pick first clip intersecting playhead
    const sel = findClipById(state.selectedClipId);
    if (sel) return sel;
    for (const tr of state.tracks) {
      for (let i = 0; i < tr.clips.length; i++) {
        const c = tr.clips[i];
        const end = c.startTime + (c.trimEnd - c.trimStart);
        if (state.playhead > c.startTime && state.playhead < end) {
          return { track: tr, clip: c, index: i };
        }
      }
    }
    return null;
  }

  function updateProjectSummary() {
    const total = getTimelineContentEnd();
    $("time-total").textContent = formatTime(total, false);
    $("timeline-empty").classList.toggle("hidden", state.tracks.length > 0);
    if (!state.tracks.length) {
      $("project-summary").textContent = "Kéo thả file vào Khay Media để bắt đầu";
      $("stage-track-info").textContent = "Chưa có clip trên Timeline";
    } else {
      const clipCount = state.tracks.reduce((a, t) => a + t.clips.length, 0);
      $("project-summary").textContent = `${state.tracks.length} luồng • ${clipCount} đoạn • Tổng ${formatTime(total, false)}`;
      $("stage-track-info").textContent = `${state.tracks.length} luồng âm thanh đang hoạt động`;
    }
  }

  function renderTrackControls() {
    trackControlsList.innerHTML = "";
    state.tracks.forEach((tr, idx) => {
      const row = document.createElement("div");
      row.className = `track-ctrl ${tr.id === state.selectedTrackId ? "selected-track" : ""}`;
      row.innerHTML = `
        <div class="track-ctrl-top">
          <span class="track-dot" style="background:${tr.color}"></span>
          <span class="track-name" title="${escapeHtml(tr.name)}">${escapeHtml(tr.name)}</span>
          <button class="tl-btn" style="padding:1px 4px" data-act="remove" title="Xóa luồng">✕</button>
        </div>
        <div class="track-ctrl-bottom">
          <button class="mini-toggle ${tr.muted ? "active-mute" : ""}" data-act="mute" title="Tắt tiếng (Mute)">M</button>
          <button class="mini-toggle ${tr.solo ? "active-solo" : ""}" data-act="solo" title="Chỉ nghe luồng này (Solo)">S</button>
          <input class="track-vol" type="range" min="0" max="200" value="${Math.round(tr.volume * 100)}" title="Âm lượng luồng (0–200%)" />
          <span class="track-vol-pct">${Math.round(tr.volume * 100)}%</span>
        </div>
      `;

      row.addEventListener("click", () => {
        state.selectedTrackId = tr.id;
        renderTrackControls();
      });

      row.querySelector('[data-act="remove"]').addEventListener("click", (e) => {
        e.stopPropagation();
        stopPlayback();
        pushHistory();
        state.tracks.splice(idx, 1);
        if (state.selectedClipId && !findClipById(state.selectedClipId)) {
          state.selectedClipId = null;
        }
        afterTimelineMutated();
      });

      row.querySelector('[data-act="mute"]').addEventListener("click", (e) => {
        e.stopPropagation();
        pushHistory();
        tr.muted = !tr.muted;
        afterTimelineMutated();
        syncAudioVolumes();
      });

      row.querySelector('[data-act="solo"]').addEventListener("click", (e) => {
        e.stopPropagation();
        pushHistory();
        tr.solo = !tr.solo;
        afterTimelineMutated();
        syncAudioVolumes();
      });

      const volInput = row.querySelector(".track-vol");
      const volPct = row.querySelector(".track-vol-pct");
      volInput.addEventListener("mousedown", () => pushHistory());
      volInput.addEventListener("input", (e) => {
        tr.volume = Number(e.target.value) / 100;
        volPct.textContent = `${Math.round(tr.volume * 100)}%`;
        syncAudioVolumes();
      });

      trackControlsList.appendChild(row);
    });
  }

  // ---------------- CapCut Edit Operations (Split, Trim Left I, Trim Right O, Delete) ----------------
  function splitClipAtPlayhead() {
    const found = findTargetClipForCut();
    if (!found) return toast("Chọn 1 clip và đặt vạch đỏ bên trong clip rồi bấm Tách (Ctrl+B / S).");
    const { track, clip, index } = found;
    const clipEnd = clip.startTime + (clip.trimEnd - clip.trimStart);
    if (state.playhead <= clip.startTime + 0.05 || state.playhead >= clipEnd - 0.05) {
      return toast("Đặt vạch đỏ nằm trong đoạn audio cần tách.");
    }
    pushHistory();
    const splitPt = round3(clip.trimStart + (state.playhead - clip.startTime));
    const rightHalf = {
      ...clip,
      id: uid(),
      startTime: round3(state.playhead),
      trimStart: splitPt,
    };
    clip.trimEnd = splitPt;
    track.clips.splice(index + 1, 0, rightHalf);
    state.selectedClipId = rightHalf.id;
    state.selectedTrackId = track.id;
    afterTimelineMutated();
  }

  function trimLeftToPlayhead() {
    // CapCut shortcut I / Q: remove everything to the left of playhead on target clip
    const found = findTargetClipForCut();
    if (!found) return toast("Chọn clip và đặt vạch đỏ bên trong clip để cắt bỏ bên trái (I).");
    const { track, clip } = found;
    const clipEnd = clip.startTime + (clip.trimEnd - clip.trimStart);
    if (state.playhead <= clip.startTime + 0.05 || state.playhead >= clipEnd - 0.05) {
      return toast("Đặt vạch đỏ nằm bên trong clip rồi bấm I để cắt bỏ bên trái.");
    }
    pushHistory();
    const delta = state.playhead - clip.startTime;
    clip.trimStart = round3(clip.trimStart + delta);
    clip.startTime = round3(state.playhead);
    state.selectedClipId = clip.id;
    state.selectedTrackId = track.id;
    afterTimelineMutated();
  }

  function trimRightToPlayhead() {
    // CapCut shortcut O / W: remove everything to the right of playhead on target clip
    const found = findTargetClipForCut();
    if (!found) return toast("Chọn clip và đặt vạch đỏ bên trong clip để cắt bỏ bên phải (O).");
    const { track, clip } = found;
    const clipEnd = clip.startTime + (clip.trimEnd - clip.trimStart);
    if (state.playhead <= clip.startTime + 0.05 || state.playhead >= clipEnd - 0.05) {
      return toast("Đặt vạch đỏ nằm bên trong clip rồi bấm O để cắt bỏ bên phải.");
    }
    pushHistory();
    const keepDur = state.playhead - clip.startTime;
    clip.trimEnd = round3(clip.trimStart + keepDur);
    state.selectedClipId = clip.id;
    state.selectedTrackId = track.id;
    afterTimelineMutated();
  }

  function duplicateSelectedClip() {
    const found = findClipById(state.selectedClipId);
    if (!found) return;
    pushHistory();
    const { track, clip } = found;
    const dur = clip.trimEnd - clip.trimStart;
    const copy = { ...clip, id: uid(), startTime: round3(clip.startTime + dur + 0.1) };
    track.clips.push(copy);
    state.selectedClipId = copy.id;
    afterTimelineMutated();
  }

  function deleteSelectedClip() {
    const ids = new Set(state.selectedClipIds);
    if (state.selectedClipId) ids.add(state.selectedClipId);
    if (!ids.size) return;
    pushHistory();
    for (const tr of state.tracks) {
      tr.clips = tr.clips.filter((c) => !ids.has(c.id));
    }
    state.selectedClipId = null;
    state.selectedClipIds.clear();
    afterTimelineMutated();
  }

  $("btn-split").addEventListener("click", splitClipAtPlayhead);
  $("btn-trim-left").addEventListener("click", trimLeftToPlayhead);
  $("btn-trim-right").addEventListener("click", trimRightToPlayhead);
  $("btn-insp-trim-left").addEventListener("click", trimLeftToPlayhead);
  $("btn-insp-trim-right").addEventListener("click", trimRightToPlayhead);
  $("btn-duplicate").addEventListener("click", duplicateSelectedClip);
  $("btn-delete-clip").addEventListener("click", deleteSelectedClip);

  $("btn-snap").addEventListener("click", () => {
    state.snapEnabled = !state.snapEnabled;
    $("btn-snap").classList.toggle("active", state.snapEnabled);
  });

  // ---------------- Zoom & Canvas Rendering ----------------
  const MIN_PPS = 0.015;
  const MAX_PPS = 120;

  function sliderToPps(val) {
    return MIN_PPS * Math.pow(MAX_PPS / MIN_PPS, (Number(val) - 1) / 99);
  }
  function ppsToSlider(pps) {
    const c = Math.max(MIN_PPS, Math.min(MAX_PPS, pps));
    return Math.round(1 + (Math.log(c / MIN_PPS) / Math.log(MAX_PPS / MIN_PPS)) * 99);
  }
  function setZoom(newPps) {
    state.pxPerSec = Math.max(MIN_PPS, Math.min(MAX_PPS, newPps));
    $("zoom-slider").value = String(ppsToSlider(state.pxPerSec));
    renderTimeline();
  }
  function zoomToFit() {
    const vw = Math.max(300, scrollArea.clientWidth - 40);
    setZoom(vw / getTimelineCanvasDuration());
    scrollArea.scrollLeft = 0;
  }

  $("zoom-slider").addEventListener("input", (e) => setZoom(sliderToPps(e.target.value)));
  $("btn-zoom-in").addEventListener("click", () => setZoom(state.pxPerSec * 1.45));
  $("btn-zoom-out").addEventListener("click", () => setZoom(state.pxPerSec / 1.45));
  $("btn-zoom-fit").addEventListener("click", zoomToFit);

  scrollArea.addEventListener("scroll", () => {
    trackControlsList.scrollTop = scrollArea.scrollTop;
    renderTimeline();
  });
  scrollArea.addEventListener("wheel", (e) => {
    if (e.ctrlKey) {
      e.preventDefault();
      setZoom(state.pxPerSec * (e.deltaY < 0 ? 1.25 : 0.8));
    }
  }, { passive: false });

  function pickNiceTickStep(pps) {
    const steps = [0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
    for (const s of steps) if (s * pps >= 85) return s;
    return 7200;
  }

  function renderTimeline() {
    const dpr = window.devicePixelRatio || 1;
    const totalDur = getTimelineCanvasDuration();
    const contentW = Math.min(30000, Math.max(scrollArea.clientWidth || 600, Math.ceil(totalDur * state.pxPerSec)));
    const contentH = Math.max(scrollArea.clientHeight || 200, state.tracks.length * TRACK_HEIGHT);

    if (timelineCanvas.style.width !== `${contentW}px`) {
      timelineCanvas.style.width = `${contentW}px`;
      timelineCanvas.style.height = `${contentH}px`;
    }

    const viewW = scrollArea.clientWidth || 800;
    const scrollLeft = scrollArea.scrollLeft;

    // Ruler
    rulerCanvas.width = Math.floor(viewW * dpr);
    rulerCanvas.height = Math.floor(26 * dpr);
    const rctx = rulerCanvas.getContext("2d");
    rctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    rctx.fillStyle = "#14151a";
    rctx.fillRect(0, 0, viewW, 26);

    const step = pickNiceTickStep(state.pxPerSec);
    const startSec = Math.floor(scrollLeft / state.pxPerSec / step) * step;
    const endSec = (scrollLeft + viewW) / state.pxPerSec;

    rctx.strokeStyle = "#313544";
    rctx.fillStyle = "#8b90a0";
    rctx.font = "10.5px Consolas, monospace";
    for (let t = Math.max(0, startSec); t <= endSec; t += step) {
      const x = Math.round(t * state.pxPerSec - scrollLeft) + 0.5;
      rctx.beginPath();
      rctx.moveTo(x, 14);
      rctx.lineTo(x, 26);
      rctx.stroke();
      rctx.fillText(formatTime(t, step < 1), x + 4, 11);
    }

    const phRulerX = state.playhead * state.pxPerSec - scrollLeft;
    if (phRulerX >= -8 && phRulerX <= viewW + 8) {
      rctx.fillStyle = "#00d2d3";
      rctx.beginPath();
      rctx.moveTo(phRulerX - 6, 0);
      rctx.lineTo(phRulerX + 6, 0);
      rctx.lineTo(phRulerX, 10);
      rctx.closePath();
      rctx.fill();
    }

    // Tracks canvas
    timelineCanvas.width = Math.floor(contentW * dpr);
    timelineCanvas.height = Math.floor(contentH * dpr);
    const ctx = timelineCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    ctx.fillStyle = "#101116";
    ctx.fillRect(scrollLeft, 0, viewW + 2, contentH);

    state.tracks.forEach((tr, rowIdx) => {
      const y = rowIdx * TRACK_HEIGHT;
      ctx.fillStyle = rowIdx % 2 === 0 ? "#12141b" : "#0f1117";
      ctx.fillRect(scrollLeft, y, viewW + 2, TRACK_HEIGHT);

      ctx.strokeStyle = "#20232e";
      ctx.beginPath();
      ctx.moveTo(scrollLeft, y + TRACK_HEIGHT - 0.5);
      ctx.lineTo(scrollLeft + viewW + 2, y + TRACK_HEIGHT - 0.5);
      ctx.stroke();

      for (const clip of tr.clips) {
        const clipDur = Math.max(0.05, clip.trimEnd - clip.trimStart);
        const x = clip.startTime * state.pxPerSec;
        const w = Math.max(8, clipDur * state.pxPerSec);
        if (x + w < scrollLeft - 20 || x > scrollLeft + viewW + 20) continue;

        const isSel = clip.id === state.selectedClipId || state.selectedClipIds.has(clip.id);
        const cy = y + 6;
        const ch = TRACK_HEIGHT - 12;

        ctx.fillStyle = tr.muted ? "#232630" : `${tr.color}2e`;
        ctx.strokeStyle = isSel ? "#ffffff" : tr.color;
        ctx.lineWidth = isSel ? 2 : 1;
        roundRect(ctx, x, cy, w, ch, 5);
        ctx.fill();
        ctx.stroke();

        // Peaks
        if (clip.peaks && clip.peaks.length && clip.duration > 0) {
          const visStart = Math.max(0, Math.floor(scrollLeft - x));
          const visEnd = Math.min(Math.ceil(w), Math.ceil(scrollLeft + viewW - x));
          const midY = cy + ch / 2 + 5;
          const maxAmp = (ch - 20) / 2;
          ctx.fillStyle = tr.muted ? "#585e70" : `${tr.color}cc`;
          for (let px = visStart; px < visEnd; px += 2) {
            const srcTime = clip.trimStart + (px / w) * clipDur;
            const idx = Math.min(clip.peaks.length - 1, Math.max(0, Math.floor((srcTime / clip.duration) * clip.peaks.length)));
            const amp = Math.max(1.5, clip.peaks[idx] * maxAmp * Math.min(1.5, clip.volume));
            ctx.fillRect(x + px, midY - amp, 1.4, amp * 2);
          }
        }

        // Trim edges
        ctx.fillStyle = isSel ? "#ffffff" : tr.color;
        ctx.fillRect(x, cy, 3, ch);
        ctx.fillRect(x + w - 3, cy, 3, ch);

        // Label
        ctx.save();
        ctx.beginPath();
        ctx.rect(x + 6, cy + 2, Math.max(0, w - 12), 15);
        ctx.clip();
        ctx.fillStyle = "#f1f3f8";
        ctx.font = "600 11px Segoe UI, sans-serif";
        ctx.fillText(clip.name, x + 7, cy + 13);
        ctx.restore();
      }
    });

    // Draw marquee selection box if dragging on empty timeline area
    if (state.timelineMarquee) {
      const m = state.timelineMarquee;
      const mx = Math.min(m.x1, m.x2);
      const my = Math.min(m.y1, m.y2);
      const mw = Math.abs(m.x2 - m.x1);
      const mh = Math.abs(m.y2 - m.y1);
      ctx.fillStyle = "rgba(0, 210, 211, 0.16)";
      ctx.strokeStyle = "#00d2d3";
      ctx.lineWidth = 1;
      ctx.fillRect(mx, my, mw, mh);
      ctx.strokeRect(mx, my, mw, mh);
    }

    // Playhead vertical cyan line (CapCut style)
    const phX = state.playhead * state.pxPerSec;
    ctx.strokeStyle = "#00d2d3";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(phX, 0);
    ctx.lineTo(phX, contentH);
    ctx.stroke();

    $("stage-time-big").textContent = formatTime(state.playhead, true);
    $("time-current").textContent = formatTime(state.playhead, false);
    if (!state.isPlaying) syncStageVideo(false);
  }

  function roundRect(ctx, x, y, w, h, r) {
    const rad = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rad, y);
    ctx.arcTo(x + w, y, x + w, y + h, rad);
    ctx.arcTo(x + w, y + h, x, y + h, rad);
    ctx.arcTo(x, y + h, x, y, rad);
    ctx.arcTo(x, y, x + w, y, rad);
    ctx.closePath();
  }

  // ---------------- Pointer Interactions & Magnet Snapping ----------------
  function snapTime(sec, ignoreClipId) {
    if (!state.snapEnabled) return sec;
    const thresholdSec = 10 / state.pxPerSec;
    const candidates = [0, state.playhead];
    for (const tr of state.tracks) {
      for (const c of tr.clips) {
        if (c.id === ignoreClipId) continue;
        candidates.push(c.startTime, c.startTime + (c.trimEnd - c.trimStart));
      }
    }
    let best = sec;
    let bestDist = thresholdSec;
    for (const cand of candidates) {
      const d = Math.abs(sec - cand);
      if (d < bestDist) {
        bestDist = d;
        best = cand;
      }
    }
    return best;
  }

  function hitTestClip(canvasX, canvasY) {
    const rowIdx = Math.floor(canvasY / TRACK_HEIGHT);
    if (rowIdx < 0 || rowIdx >= state.tracks.length) return null;
    const track = state.tracks[rowIdx];
    for (let i = track.clips.length - 1; i >= 0; i--) {
      const clip = track.clips[i];
      const w = Math.max(8, (clip.trimEnd - clip.trimStart) * state.pxPerSec);
      const x = clip.startTime * state.pxPerSec;
      if (canvasX >= x - 4 && canvasX <= x + w + 4) {
        if (canvasX <= x + HANDLE_PX) return { track, clip, zone: "trim-left" };
        if (canvasX >= x + w - HANDLE_PX) return { track, clip, zone: "trim-right" };
        return { track, clip, zone: "move" };
      }
    }
    return null;
  }

  rulerCanvas.addEventListener("mousedown", (e) => {
    const rect = rulerCanvas.getBoundingClientRect();
    seekPlayhead(Math.max(0, (e.clientX - rect.left + scrollArea.scrollLeft) / state.pxPerSec));
    state.drag = { mode: "scrub" };
  });

  timelineCanvas.addEventListener("mousedown", (e) => {
    const rect = timelineCanvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const hit = hitTestClip(x, y);

    state.selectedMediaIds.clear();
    state.selectedOverlayId = null;
    updateMediaDeleteButton();
    renderMediaPool();

    if (hit) {
      state.selectedClipId = hit.clip.id;
      if (e.ctrlKey || e.metaKey) {
        if (state.selectedClipIds.has(hit.clip.id)) state.selectedClipIds.delete(hit.clip.id);
        else state.selectedClipIds.add(hit.clip.id);
      } else {
        state.selectedClipIds.clear();
        state.selectedClipIds.add(hit.clip.id);
      }
      state.selectedTrackId = hit.track.id;
      renderTrackControls();
      updateInspector();
      state.drag = {
        mode: hit.zone,
        clip: hit.clip,
        startClientX: e.clientX,
        origStartTime: hit.clip.startTime,
        origTrimStart: hit.clip.trimStart,
        origTrimEnd: hit.clip.trimEnd,
        pushedHistory: false,
      };
      renderTimeline();
    } else {
      state.selectedClipId = null;
      state.selectedClipIds.clear();
      updateInspector();
      state.timelineMarquee = { x1: x, y1: y, x2: x, y2: y };
      state.drag = { mode: "marquee", startX: x, startY: y, moved: false };
      renderTimeline();
    }
  });

  timelineCanvas.addEventListener("mousemove", (e) => {
    if (state.drag) return;
    const rect = timelineCanvas.getBoundingClientRect();
    const hit = hitTestClip(e.clientX - rect.left, e.clientY - rect.top);
    timelineCanvas.style.cursor = !hit
      ? "default"
      : hit.zone === "move"
        ? "grab"
        : "ew-resize";
  });

  window.addEventListener("mousemove", (e) => {
    if (!state.drag) return;
    if (state.drag.mode === "scrub") {
      const rect = rulerCanvas.getBoundingClientRect();
      seekPlayhead(Math.max(0, (e.clientX - rect.left + scrollArea.scrollLeft) / state.pxPerSec));
      return;
    }

    if (state.drag.mode === "marquee") {
      const rect = timelineCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      if (Math.abs(x - state.drag.startX) > 4 || Math.abs(y - state.drag.startY) > 4) {
        state.drag.moved = true;
      }
      state.timelineMarquee = { x1: state.drag.startX, y1: state.drag.startY, x2: x, y2: y };
      const minX = Math.min(state.drag.startX, x);
      const maxX = Math.max(state.drag.startX, x);
      const minY = Math.min(state.drag.startY, y);
      const maxY = Math.max(state.drag.startY, y);
      state.selectedClipIds.clear();
      state.tracks.forEach((tr, rowIdx) => {
        const yTop = rowIdx * TRACK_HEIGHT + 6;
        const yBot = (rowIdx + 1) * TRACK_HEIGHT - 6;
        if (yBot < minY || yTop > maxY) return;
        for (const c of tr.clips) {
          const cx1 = c.startTime * state.pxPerSec;
          const cx2 = cx1 + Math.max(8, (c.trimEnd - c.trimStart) * state.pxPerSec);
          if (!(cx2 < minX || cx1 > maxX)) {
            state.selectedClipIds.add(c.id);
          }
        }
      });
      state.selectedClipId = state.selectedClipIds.size === 1 ? [...state.selectedClipIds][0] : null;
      renderTimeline();
      return;
    }

    if (!state.drag.pushedHistory && Math.abs(e.clientX - state.drag.startClientX) > 2) {
      pushHistory();
      state.drag.pushedHistory = true;
    }

    const dxSec = (e.clientX - state.drag.startClientX) / state.pxPerSec;
    const c = state.drag.clip;
    if (state.drag.mode === "move") {
      const rawStart = Math.max(0, state.drag.origStartTime + dxSec);
      c.startTime = round3(Math.max(0, snapTime(rawStart, c.id)));
    } else if (state.drag.mode === "trim-left") {
      const maxDelta = state.drag.origTrimEnd - state.drag.origTrimStart - 0.1;
      const minDelta = -Math.min(state.drag.origTrimStart, state.drag.origStartTime);
      const clamped = Math.max(minDelta, Math.min(maxDelta, dxSec));
      c.trimStart = round3(state.drag.origTrimStart + clamped);
      c.startTime = round3(state.drag.origStartTime + clamped);
    } else if (state.drag.mode === "trim-right") {
      c.trimEnd = round3(Math.max(c.trimStart + 0.1, Math.min(c.duration, state.drag.origTrimEnd + dxSec)));
    }
    updateInspector();
    updateProjectSummary();
    renderTimeline();
  });

  window.addEventListener("mouseup", () => {
    if (state.drag) {
      if (state.drag.mode === "marquee") {
        const wasMoved = state.drag.moved;
        const clickX = state.drag.startX;
        state.timelineMarquee = null;
        state.drag = null;
        if (!wasMoved) {
          seekPlayhead(Math.max(0, clickX / state.pxPerSec));
        } else {
          if (state.selectedClipIds.size > 0) {
            toast(`Đã bôi đen ${state.selectedClipIds.size} đoạn — bấm Backspace để xóa.`);
          }
          updateInspector();
          renderTimeline();
        }
        return;
      }
      const changedClip = state.drag.mode !== "scrub" && state.drag.pushedHistory;
      state.drag = null;
      if (changedClip && state.isPlaying) restartPlaybackAt(state.playhead);
    }
  });

  // Drop onto Timeline (from Media Pool OR directly from Windows Explorer!)
  const tlDrop = $("timeline-dropzone");
  tlDrop.addEventListener("dragover", (e) => {
    e.preventDefault();
    tlDrop.classList.add("dragover");
  });
  tlDrop.addEventListener("dragleave", () => tlDrop.classList.remove("dragover"));
  tlDrop.addEventListener("drop", async (e) => {
    e.preventDefault();
    tlDrop.classList.remove("dragover");
    const mediaId = e.dataTransfer.getData("application/x-vocalforge-media");
    if (mediaId) {
      const item = state.mediaPool.find((m) => m.id === mediaId);
      if (item) {
        const rect = timelineCanvas.getBoundingClientRect();
        const dropSec = Math.max(0, (e.clientX - rect.left) / state.pxPerSec);
        addMediaItemToTimeline(item, dropSec);
      }
      return;
    }
    try {
      const paths = await resolveDroppedFiles(e.dataTransfer.files);
      await importFilesToMediaPool(paths, null, true);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------- Right Inspector ----------------
  function updateInspector() {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    const found = ov ? null : findClipById(state.selectedClipId);

    $("sticker-inspector").classList.toggle("hidden", !ov);
    $("inspector-body").classList.toggle("hidden", !found);
    $("inspector-empty").classList.toggle("hidden", Boolean(ov || found));

    if (ov) {
      $("inspector-header-title").textContent = "Chi tiết Nhãn dán / Logo";
      $("st-insp-name").textContent = ov.name;
      $("st-insp-size").value = String(Math.round(ov.widthPct));
      $("st-insp-size-val").textContent = `${Math.round(ov.widthPct)}%`;
      $("st-insp-opacity").value = String(Math.round(ov.opacity * 100));
      $("st-insp-opacity-val").textContent = `${Math.round(ov.opacity * 100)}%`;
      return;
    }

    $("inspector-header-title").textContent = "Chi tiết âm thanh";
    if (!found) return;

    const c = found.clip;
    $("insp-name").textContent = c.name;
    $("insp-volume").value = String(Math.round(c.volume * 100));
    $("insp-vol-val").textContent = `${Math.round(c.volume * 100)}%`;
    $("insp-fade-in").value = String(c.fadeIn);
    $("insp-fi-val").textContent = `${c.fadeIn.toFixed(1)}s`;
    $("insp-fade-out").value = String(c.fadeOut);
    $("insp-fo-val").textContent = `${c.fadeOut.toFixed(1)}s`;
    $("insp-start").value = c.startTime.toFixed(2);
    $("insp-duration").textContent = `Độ dài: ${formatTime(c.trimEnd - c.trimStart, true)}`;
  }

  $("insp-volume").addEventListener("mousedown", () => pushHistory());
  $("insp-volume").addEventListener("input", (e) => {
    const f = findClipById(state.selectedClipId);
    if (!f) return;
    f.clip.volume = Number(e.target.value) / 100;
    $("insp-vol-val").textContent = `${Math.round(f.clip.volume * 100)}%`;
    renderTimeline();
    syncAudioVolumes();
  });

  $("insp-fade-in").addEventListener("mousedown", () => pushHistory());
  $("insp-fade-in").addEventListener("input", (e) => {
    const f = findClipById(state.selectedClipId);
    if (!f) return;
    f.clip.fadeIn = Number(e.target.value);
    $("insp-fi-val").textContent = `${f.clip.fadeIn.toFixed(1)}s`;
  });

  $("insp-fade-out").addEventListener("mousedown", () => pushHistory());
  $("insp-fade-out").addEventListener("input", (e) => {
    const f = findClipById(state.selectedClipId);
    if (!f) return;
    f.clip.fadeOut = Number(e.target.value);
    $("insp-fo-val").textContent = `${f.clip.fadeOut.toFixed(1)}s`;
  });

  $("insp-start").addEventListener("change", (e) => {
    const f = findClipById(state.selectedClipId);
    if (!f) return;
    pushHistory();
    f.clip.startTime = Math.max(0, Number(e.target.value) || 0);
    afterTimelineMutated();
  });

  $("btn-insp-separate").addEventListener("click", () => {
    const f = findClipById(state.selectedClipId);
    if (f) triggerSeparateForPath(f.clip.path);
  });

  // ---------------- Synced Multitrack Preview Player (Web Audio Gain up to 200%) ----------------
  let previewAudioCtx = null;

  function ensureAudioContext() {
    if (!previewAudioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) previewAudioCtx = new AC();
    }
    if (previewAudioCtx && previewAudioCtx.state === "suspended") {
      previewAudioCtx.resume().catch(() => {});
    }
    return previewAudioCtx;
  }

  function applyEntryGain(entry, gainVal) {
    const clamped = Math.max(0, Math.min(2.0, gainVal));
    if (entry.gainNode) {
      entry.gainNode.gain.value = clamped;
      entry.audio.volume = 1.0;
    } else {
      entry.audio.volume = Math.min(1.0, clamped);
    }
  }

  function isTrackAudible(track) {
    const anySolo = state.tracks.some((t) => t.solo);
    return anySolo ? track.solo && !track.muted : !track.muted;
  }

  function syncAudioVolumes() {
    for (const tr of state.tracks) {
      const audible = isTrackAudible(tr);
      for (const clip of tr.clips) {
        const entry = state.audioPool.get(clip.id);
        if (entry) {
          applyEntryGain(entry, audible ? tr.volume * clip.volume : 0);
        }
      }
    }
  }

  function releaseAudioEntry(entry) {
    entry.audio.pause();
    entry.audio.src = "";
    try {
      if (entry.sourceNode) entry.sourceNode.disconnect();
      if (entry.gainNode) entry.gainNode.disconnect();
    } catch (_) {}
  }

  function stopPlayback() {
    state.isPlaying = false;
    $("btn-play").textContent = "▶";
    const vidEl = $("stage-video");
    if (vidEl) vidEl.pause();
    for (const entry of state.audioPool.values()) {
      releaseAudioEntry(entry);
    }
    state.audioPool.clear();
    drawStageBars(0);
  }

  function seekPlayhead(sec) {
    state.playhead = Math.max(0, sec);
    if (state.isPlaying) restartPlaybackAt(state.playhead);
    else renderTimeline();
  }

  function restartPlaybackAt(sec) {
    stopPlayback();
    state.playhead = sec;
    startPlayback();
  }

  function startPlayback() {
    if (!state.tracks.length) return;
    ensureAudioContext();
    state.isPlaying = true;
    $("btn-play").textContent = "⏸";
    state.playStartWall = performance.now();
    state.playStartHead = state.playhead;
    tickPlayback();
  }

  function drawStageBars(energy) {
    const canvas = $("stage-meter-canvas");
    const w = (canvas.width = canvas.clientWidth || 320);
    const h = (canvas.height = canvas.clientHeight || 140);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    if (energy <= 0.01) return;

    const bars = 36;
    const bw = w / bars;
    ctx.fillStyle = "#00d2d3";
    const t = performance.now() / 120;
    for (let i = 0; i < bars; i++) {
      const wave = 0.35 + 0.65 * Math.abs(Math.sin(t + i * 0.45));
      const bh = Math.max(4, wave * energy * h * 0.72);
      ctx.fillRect(i * bw + 2, h - bh, Math.max(2, bw - 4), bh);
    }
  }

  function tickPlayback() {
    if (!state.isPlaying) return;
    state.playhead = state.playStartHead + (performance.now() - state.playStartWall) / 1000;

    let anyActiveOrFuture = false;
    let peakEnergy = 0;

    for (const tr of state.tracks) {
      const audible = isTrackAudible(tr);
      for (const clip of tr.clips) {
        const clipStart = clip.startTime;
        const clipEnd = clip.startTime + (clip.trimEnd - clip.trimStart);
        if (clipEnd > state.playhead) anyActiveOrFuture = true;

        const inside = state.playhead >= clipStart && state.playhead < clipEnd;
        let entry = state.audioPool.get(clip.id);

        if (inside) {
          const desired = clip.trimStart + (state.playhead - clipStart);
          if (audible && clip.peaks && clip.peaks.length && clip.duration > 0) {
            const pIdx = Math.min(clip.peaks.length - 1, Math.floor((desired / clip.duration) * clip.peaks.length));
            peakEnergy = Math.max(peakEnergy, clip.peaks[pIdx] * tr.volume * clip.volume);
          }

          if (!entry) {
            const audio = new Audio(`/api/audio/stream?path=${encodeURIComponent(clip.path)}`);
            audio.preload = "auto";
            audio.crossOrigin = "anonymous";
            let sourceNode = null;
            let gainNode = null;
            const ctx = ensureAudioContext();
            if (ctx) {
              try {
                sourceNode = ctx.createMediaElementSource(audio);
                gainNode = ctx.createGain();
                sourceNode.connect(gainNode);
                gainNode.connect(ctx.destination);
              } catch (_) {}
            }
            entry = { audio, sourceNode, gainNode };
            applyEntryGain(entry, audible ? tr.volume * clip.volume : 0);
            audio.currentTime = desired;
            audio.play().catch(() => {});
            state.audioPool.set(clip.id, entry);
          } else {
            applyEntryGain(entry, audible ? tr.volume * clip.volume : 0);
            if (Math.abs(entry.audio.currentTime - desired) > 0.35 && entry.audio.readyState >= 2) {
              entry.audio.currentTime = desired;
            }
          }
        } else if (entry) {
          releaseAudioEntry(entry);
          state.audioPool.delete(clip.id);
        }
      }
    }

    drawStageBars(Math.min(1, peakEnergy));
    syncStageVideo(true);
    renderTimeline();
    if (!anyActiveOrFuture) {
      stopPlayback();
      return;
    }
    requestAnimationFrame(tickPlayback);
  }

  $("btn-play").addEventListener("click", () => (state.isPlaying ? stopPlayback() : startPlayback()));
  $("btn-back-5").addEventListener("click", () => seekPlayhead(state.playhead - 5));
  $("btn-fwd-5").addEventListener("click", () => seekPlayhead(state.playhead + 5));

  // ---------------- CapCut Keyboard Shortcuts ----------------
  window.addEventListener("keydown", (e) => {
    if (e.target && ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
    const key = e.key.toLowerCase();

    if ((e.ctrlKey || e.metaKey) && key === "z" && !e.shiftKey) {
      e.preventDefault();
      undo();
    } else if ((e.ctrlKey || e.metaKey) && (key === "y" || (key === "z" && e.shiftKey))) {
      e.preventDefault();
      redo();
    } else if ((e.ctrlKey || e.metaKey) && (key === "=" || key === "+" || e.code === "NumpadAdd")) {
      e.preventDefault();
      setZoom(state.pxPerSec * 1.35);
    } else if ((e.ctrlKey || e.metaKey) && (key === "-" || key === "_" || e.code === "NumpadSubtract")) {
      e.preventDefault();
      setZoom(state.pxPerSec / 1.35);
    } else if ((e.ctrlKey || e.metaKey) && key === "b") {
      e.preventDefault();
      splitClipAtPlayhead();
    } else if ((e.ctrlKey || e.metaKey) && key === "d") {
      e.preventDefault();
      duplicateSelectedClip();
    } else if (e.shiftKey && key === "z") {
      e.preventDefault();
      zoomToFit();
    } else if (e.code === "Space") {
      e.preventDefault();
      state.isPlaying ? stopPlayback() : startPlayback();
    } else if (key === "i" || key === "q") {
      e.preventDefault();
      trimLeftToPlayhead();
    } else if (key === "o" || key === "w") {
      e.preventDefault();
      trimRightToPlayhead();
    } else if (key === "s" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      splitClipAtPlayhead();
    } else if (key === "n") {
      state.snapEnabled = !state.snapEnabled;
      $("btn-snap").classList.toggle("active", state.snapEnabled);
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      if (state.selectedMediaIds.size > 0) {
        deleteSelectedMediaItems();
      } else if (state.selectedOverlayId) {
        removeSelectedOverlay();
      } else {
        deleteSelectedClip();
      }
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      seekPlayhead(state.playhead - 5);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      seekPlayhead(state.playhead + 5);
    } else if (e.key === "Home") {
      stopPlayback();
      seekPlayhead(0);
    }
  });

  // ---------------- 4-Mode CapCut Export Modal ----------------
  function openExportModal() {
    if (!state.tracks.length) {
      return toast("Chưa có âm thanh nào trên Timeline để xuất.", "error");
    }
    const savedDir = localStorage.getItem("vf_export_dir") || "";
    $("export-dir").value = savedDir;

    const vidSel = $("export-video-source");
    vidSel.innerHTML = "";
    const videos = state.mediaPool.filter((m) => m.isVideo);
    const list = videos.length ? videos : state.mediaPool;
    for (const m of list) {
      const opt = document.createElement("option");
      opt.value = m.path;
      opt.textContent = m.name;
      vidSel.appendChild(opt);
    }
    if (state.activeVideoPath && list.some((m) => m.path === state.activeVideoPath)) {
      vidSel.value = state.activeVideoPath;
    }

    $("export-modal-title").textContent = "Xuất âm thanh / Video";
    $("export-config-view").classList.remove("hidden");
    $("export-progress-view").classList.add("hidden");
    $("export-modal").classList.remove("hidden");
  }

  $("btn-pick-export-dir").addEventListener("click", async () => {
    if (hasPyWebView()) {
      const dir = await window.pywebview.api.pick_folder();
      if (dir) {
        $("export-dir").value = dir;
        localStorage.setItem("vf_export_dir", dir);
      }
    } else {
      toast("Trên bản App Windows (.exe), bấm nút này sẽ mở hộp thoại chọn thư mục Windows. Ở bản web bạn có thể nhập trực tiếp đường dẫn.");
    }
  });

  document.querySelectorAll('input[name="export-mode"]').forEach((r) => {
    r.addEventListener("change", () => {
      const isVid = r.value === "video_mp4" && r.checked;
      $("export-video-source-wrap").classList.toggle("hidden", !isVid);
      $("export-audio-format-wrap").classList.toggle("hidden", isVid);
    });
  });

  $("btn-open-export").addEventListener("click", openExportModal);
  $("btn-close-export-x").addEventListener("click", () => $("export-modal").classList.add("hidden"));
  $("btn-cancel-export-cfg").addEventListener("click", () => $("export-modal").classList.add("hidden"));
  $("btn-close-export-done").addEventListener("click", () => $("export-modal").classList.add("hidden"));

  function collectClipsFromTrack(tr) {
    return tr.clips.map((c) => ({
      path: c.path,
      startTime: c.startTime,
      trimStart: c.trimStart,
      trimEnd: c.trimEnd,
      volume: round3(tr.volume * c.volume),
      fadeIn: c.fadeIn,
      fadeOut: c.fadeOut,
    }));
  }

  $("btn-confirm-export").addEventListener("click", async () => {
    const mode = document.querySelector('input[name="export-mode"]:checked').value;
    const fmt = $("export-format").value;
    const fileName = ($("export-filename").value || "VocalForge_Export").trim();
    const chosenDir = ($("export-dir").value || "").trim() || null;
    if (chosenDir) localStorage.setItem("vf_export_dir", chosenDir);

    const payload = {
      export_mode: mode,
      output_format: fmt,
      file_name: fileName,
      output_dir: chosenDir,
      clips: [],
      tracks: null,
      output_path: null,
      video_source_path: null,
      video_resolution: $("export-video-res").value,
      video_fps: $("export-video-fps").value,
      overlays: mode === "video_mp4" ? state.stageOverlays.map((o) => ({
        path: o.path,
        xPct: o.xPct,
        yPct: o.yPct,
        widthPct: o.widthPct,
        opacity: o.opacity,
      })) : null,
    };

    if (mode === "mix") {
      for (const tr of state.tracks) {
        if (!isTrackAudible(tr)) continue;
        payload.clips.push(...collectClipsFromTrack(tr));
      }
      if (!payload.clips.length) return toast("Tất cả các luồng đang bị Mute!", "error");
    } else if (mode === "selected_track") {
      const selTrack = state.tracks.find((t) => t.id === state.selectedTrackId) || state.tracks[0];
      if (!selTrack || !selTrack.clips.length) return toast("Luồng đang chọn không có clip nào.", "error");
      payload.clips = collectClipsFromTrack(selTrack);
    } else if (mode === "separate_tracks") {
      payload.tracks = state.tracks
        .filter((tr) => isTrackAudible(tr) && tr.clips.length > 0)
        .map((tr) => ({ name: tr.name, clips: collectClipsFromTrack(tr) }));
      if (!payload.tracks.length) return toast("Không có luồng nào đang bật để xuất riêng.", "error");
    } else if (mode === "video_mp4") {
      for (const tr of state.tracks) {
        if (!isTrackAudible(tr)) continue;
        payload.clips.push(...collectClipsFromTrack(tr));
      }
      payload.video_source_path = $("export-video-source").value;
      if (!payload.video_source_path) return toast("Chưa có file Video gốc trong Khay Media.", "error");
      if (!payload.clips.length) return toast("Tất cả các luồng đang bị Mute!", "error");
    }

    if (hasPyWebView() && !chosenDir) {
      if (mode === "separate_tracks") {
        const dir = await window.pywebview.api.pick_folder();
        if (!dir) return;
        payload.output_path = dir;
      } else if (mode === "video_mp4") {
        const file = await window.pywebview.api.pick_save_file(`${fileName}.mp4`, "mp4");
        if (!file) return;
        payload.output_path = file;
      } else {
        const file = await window.pywebview.api.pick_save_file(`${fileName}.${fmt}`, fmt);
        if (!file) return;
        payload.output_path = file;
      }
    }

    $("export-config-view").classList.add("hidden");
    $("export-progress-view").classList.remove("hidden");
    $("export-modal-title").textContent = "Đang xuất âm thanh...";
    $("export-message").textContent = "Đang xử lý bằng FFmpeg...";
    $("export-pct").textContent = "0%";
    $("export-bar-fill").style.width = "0%";
    $("export-path-preview").classList.add("hidden");
    $("btn-open-export-folder").classList.add("hidden");
    $("btn-close-export-done").classList.add("hidden");

    try {
      const data = await api("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.exportTaskId = data.task.id;
      pollExport();
    } catch (err) {
      $("export-modal-title").textContent = "Lỗi xuất file";
      $("export-message").textContent = err.message;
      $("btn-close-export-done").classList.remove("hidden");
    }
  });

  async function pollExport() {
    if (!state.exportTaskId) return;
    try {
      const { task } = await api(`/api/export/${state.exportTaskId}`);
      $("export-message").textContent = task.message;
      $("export-pct").textContent = `${Math.round(task.progress)}%`;
      $("export-bar-fill").style.width = `${task.progress}%`;

      if (task.status === "running") {
        setTimeout(pollExport, 500);
      } else if (task.status === "completed") {
        state.lastExportPath = task.output_path;
        $("export-modal-title").textContent = "Xuất thành công!";
        $("export-path-preview").textContent = task.output_path;
        $("export-path-preview").classList.remove("hidden");
        $("btn-open-export-folder").classList.remove("hidden");
        $("btn-close-export-done").classList.remove("hidden");
      } else {
        $("export-modal-title").textContent = "Lỗi xuất file";
        $("export-message").textContent = task.error || task.message;
        $("btn-close-export-done").classList.remove("hidden");
      }
    } catch (err) {
      $("export-message").textContent = err.message;
      $("btn-close-export-done").classList.remove("hidden");
    }
  }

  $("btn-open-export-folder").addEventListener("click", async () => {
    if (!state.lastExportPath) return;
    await api("/api/system/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.lastExportPath }),
    });
  });

  // ---------------- Edge-TTS (.SRT & .TXT) ----------------
  const ttsState = {
    selectedFilePath: null,
    selectedFileName: null,
    jobs: [],
    importedIds: new Set(),
  };

  $("btn-pick-tts-file").addEventListener("click", async (e) => {
    if (hasPyWebView() && window.pywebview.api.pick_text_files) {
      e.preventDefault();
      const picked = await window.pywebview.api.pick_text_files();
      if (picked && picked.length) {
        ttsState.selectedFilePath = picked[0];
        ttsState.selectedFileName = picked[0].split(/[/\\]/).pop();
        $("tts-file-label").textContent = ttsState.selectedFileName;
        $("tts-text-input").value = "";
        toast(`Đã chọn tệp: ${ttsState.selectedFileName}`);
      }
    }
    // Otherwise native <label for="input-tts-file"> opens the file chooser directly
  });

  async function loadTextFileFromBrowser(file) {
    const text = await file.text();
    ttsState.selectedFilePath = null;
    ttsState.selectedFileName = file.name;
    $("tts-file-label").textContent = file.name;
    $("tts-text-input").value = text;
    toast(`Đã nạp ${file.name} (${text.length.toLocaleString()} ký tự)`);
  }

  $("input-tts-file").addEventListener("change", async (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) await loadTextFileFromBrowser(f);
    e.target.value = "";
  });

  const ttsTextArea = $("tts-text-input");
  ttsTextArea.addEventListener("dragover", (e) => e.preventDefault());
  ttsTextArea.addEventListener("drop", async (e) => {
    e.preventDefault();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    if (f.pywebviewFullPath) {
      ttsState.selectedFilePath = f.pywebviewFullPath;
      ttsState.selectedFileName = f.name;
      $("tts-file-label").textContent = f.name;
      $("tts-text-input").value = "";
      toast(`Đã chọn tệp: ${f.name}`);
    } else {
      await loadTextFileFromBrowser(f);
    }
  });

  function formatSigned(val, unit) {
    const n = Number(val) || 0;
    return `${n >= 0 ? "+" : ""}${n}${unit}`;
  }

  $("tts-rate").addEventListener("input", (e) => {
    $("tts-rate-val").textContent = formatSigned(e.target.value, "%");
  });

  $("tts-pitch").addEventListener("input", (e) => {
    $("tts-pitch-val").textContent = formatSigned(e.target.value, "Hz");
  });

  $("btn-start-tts").addEventListener("click", async () => {
    const rawText = $("tts-text-input").value.trim();
    if (!ttsState.selectedFilePath && !rawText) {
      return toast("Hãy chọn file .SRT / .TXT hoặc dán văn bản vào ô trước.", "error");
    }

    $("btn-start-tts").disabled = true;
    try {
      await api("/api/tts/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_name: ttsState.selectedFileName || "van_ban.txt",
          file_path: rawText ? null : ttsState.selectedFilePath,
          content: rawText || null,
          mode: "auto",
          voice: $("tts-voice").value,
          rate: formatSigned($("tts-rate").value, "%"),
          pitch: formatSigned($("tts-pitch").value, "Hz"),
          auto_fit: $("tts-autofit").checked,
          output_format: "mp3",
        }),
      });
      toast("Đã bắt đầu tạo giọng đọc Edge-TTS!", "success");
      await refreshTtsJobs();
    } catch (err) {
      toast(`Lỗi Edge-TTS: ${err.message}`, "error");
    } finally {
      $("btn-start-tts").disabled = false;
    }
  });

  async function refreshTtsJobs() {
    try {
      const data = await api("/api/tts/jobs");
      ttsState.jobs = data.jobs || [];
      renderTtsJobs();

      for (const job of ttsState.jobs) {
        if (job.status === "completed" && job.output_file && !ttsState.importedIds.has(job.id)) {
          ttsState.importedIds.add(job.id);
          importFilesToMediaPool([job.output_file], "TTS", false);
        }
      }
    } catch (_) {}
  }

  function renderTtsJobs() {
    const activeCount = ttsState.jobs.filter((j) => ["running", "merging", "queued"].includes(j.status)).length;
    const badge = $("tts-badge");
    badge.textContent = String(activeCount);
    badge.classList.toggle("hidden", activeCount === 0);

    const container = $("tts-jobs-list");
    if (!ttsState.jobs.length) {
      container.innerHTML = `<div class="empty-jobs">Hỗ trợ file <b>.SRT</b> (khớp chuẩn mốc giờ từng câu) và file <b>.TXT</b> (đọc liền mạch). Chạy xong tự động thêm vào Khay Media.</div>`;
      return;
    }

    container.innerHTML = "";
    for (const job of ttsState.jobs) {
      const card = document.createElement("div");
      card.className = "job-card";
      const eta = job.status === "running" && job.eta_seconds ? ` • ${formatEta(job.eta_seconds)}` : "";
      const stText =
        job.status === "completed"
          ? "Xong"
          : job.status === "running"
            ? `${Math.round(job.progress || 0)}%`
            : job.status === "paused"
              ? "Tạm dừng"
              : job.status;
      card.innerHTML = `
        <div class="job-top">
          <div class="job-name" title="${escapeHtml(job.source_name)}">[${job.mode.toUpperCase()}] ${escapeHtml(job.source_name)}</div>
          <span class="job-badge ${job.status}">${stText}</span>
        </div>
        <div class="job-meta">${job.completed_items.length}/${job.total_items} câu${eta} • ${escapeHtml(job.message || "")}</div>
        <div class="progress-bar"><div class="progress-fill" style="width:${Math.min(100, job.progress || 0)}%"></div></div>
        <div class="job-actions"></div>
      `;

      const actions = card.querySelector(".job-actions");
      if (job.status === "running") {
        actions.appendChild(mkBtn("⏸ Tạm dừng", "btn-soft btn-xs", async () => {
          await api(`/api/tts/jobs/${job.id}/pause`, { method: "POST" });
          refreshTtsJobs();
        }));
      }
      if (job.status === "paused" || job.status === "failed") {
        actions.appendChild(mkBtn("▶ Chạy tiếp", "btn-cyan btn-xs", async () => {
          await api(`/api/tts/jobs/${job.id}/resume`, { method: "POST" });
          refreshTtsJobs();
        }));
      }
      if (job.status === "completed" && job.output_file) {
        actions.appendChild(mkBtn("＋ Xuống Timeline", "btn-cyan btn-xs", async () => {
          await importFilesToMediaPool([job.output_file], "TTS", true);
          zoomToFit();
        }));
        actions.appendChild(mkBtn("📂 Thư mục", "btn-soft btn-xs", async () => {
          await api("/api/system/open-folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: job.output_file }),
          });
        }));
      }
      actions.appendChild(mkBtn("🗑", "btn-soft btn-xs", async () => {
        await api(`/api/tts/jobs/${job.id}`, { method: "DELETE" });
        refreshTtsJobs();
      }));

      container.appendChild(card);
    }
  }

  // ---------------- Video Stage Preview Sync ----------------
  function setStageVideoSource(videoPath) {
    const vidEl = $("stage-video");
    state.activeVideoPath = videoPath || null;
    if (!videoPath) {
      vidEl.pause();
      vidEl.removeAttribute("src");
      vidEl.load();
      vidEl.classList.add("hidden");
      $("stage-time-big").classList.remove("hidden");
      $("stage-track-info").classList.remove("hidden");
      return;
    }
    const streamUrl = `/api/audio/stream?path=${encodeURIComponent(videoPath)}`;
    if (vidEl.dataset.srcPath !== videoPath) {
      vidEl.dataset.srcPath = videoPath;
      vidEl.src = streamUrl;
    }
    vidEl.classList.remove("hidden");
    $("stage-time-big").classList.add("hidden");
    $("stage-track-info").classList.add("hidden");
    syncStageVideo(state.isPlaying);
  }

  function findActiveVideoClipAt(sec) {
    for (const tr of state.tracks) {
      for (const c of tr.clips) {
        if (!isVideoPath(c.path)) continue;
        const end = c.startTime + (c.trimEnd - c.trimStart);
        if (sec >= c.startTime && sec < end) {
          return { clip: c, videoTime: c.trimStart + (sec - c.startTime) };
        }
      }
    }
    return null;
  }

  function syncStageVideo(isPlayingNow) {
    const vidEl = $("stage-video");
    if (!state.activeVideoPath || vidEl.classList.contains("hidden")) return;

    const hit = findActiveVideoClipAt(state.playhead);
    const desiredTime = hit ? hit.videoTime : state.playhead;

    if (hit && vidEl.dataset.srcPath !== hit.clip.path) {
      setStageVideoSource(hit.clip.path);
    }

    if (vidEl.readyState >= 1) {
      const drift = Math.abs((vidEl.currentTime || 0) - desiredTime);
      if (drift > (isPlayingNow ? 0.35 : 0.04)) {
        try {
          vidEl.currentTime = Math.max(0, desiredTime);
        } catch (_) {}
      }
    }

    if (isPlayingNow) {
      if (vidEl.paused) vidEl.play().catch(() => {});
    } else {
      if (!vidEl.paused) vidEl.pause();
    }
  }

  // ---------------- Nhãn dán / Logo Overlay ----------------
  async function importStickerPaths(paths) {
    for (const p of paths) {
      if (state.stickerPool.some((s) => s.path === p)) continue;
      const name = p.split(/[/\\]/).pop() || "logo.png";
      state.stickerPool.unshift({
        id: uid(),
        name,
        path: p,
        url: `/api/audio/stream?path=${encodeURIComponent(p)}`,
      });
    }
    renderStickerPool();
  }

  function renderStickerPool() {
    const n = state.stickerPool.length;
    $("sticker-count").textContent = `${n} logo`;
    $("sticker-empty").classList.toggle("hidden", n > 0);
    $("sticker-grid").classList.toggle("hidden", n === 0);

    const grid = $("sticker-grid");
    grid.innerHTML = "";
    for (const st of state.stickerPool) {
      const card = document.createElement("div");
      card.className = "media-card";
      card.title = `${st.name}\nBấm nút ＋ để đặt lên Video`;
      card.innerHTML = `
        <div class="media-thumb">
          <img class="sticker-img" src="${st.url}" alt="${escapeHtml(st.name)}" />
          <button class="media-add-btn" title="Đặt logo lên Video">＋</button>
        </div>
        <div class="media-name">${escapeHtml(st.name)}</div>
      `;
      card.querySelector(".media-add-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        addStickerToStage(st);
      });
      grid.appendChild(card);
    }
  }

  function addStickerToStage(sticker) {
    const ov = {
      id: uid(),
      name: sticker.name,
      path: sticker.path,
      url: sticker.url,
      xPct: 76,
      yPct: 5,
      widthPct: 18,
      opacity: 1.0,
    };
    state.stageOverlays.push(ov);
    state.selectedOverlayId = ov.id;
    state.selectedClipId = null;
    state.selectedClipIds.clear();
    renderStageOverlays();
    updateInspector();
    toast(`Đã đặt "${sticker.name}" lên khung hình — kéo để chỉnh vị trí.`);
  }

  function removeSelectedOverlay() {
    if (!state.selectedOverlayId) return;
    state.stageOverlays = state.stageOverlays.filter((o) => o.id !== state.selectedOverlayId);
    state.selectedOverlayId = null;
    renderStageOverlays();
    updateInspector();
  }

  function renderStageOverlays() {
    const badge = $("stickers-badge");
    badge.textContent = String(state.stageOverlays.length);
    badge.classList.toggle("hidden", state.stageOverlays.length === 0);

    const layer = $("stage-overlay-layer");
    layer.innerHTML = "";

    for (const ov of state.stageOverlays) {
      const el = document.createElement("div");
      el.className = `stage-sticker-item ${ov.id === state.selectedOverlayId ? "selected" : ""}`;
      el.style.left = `${ov.xPct}%`;
      el.style.top = `${ov.yPct}%`;
      el.style.width = `${ov.widthPct}%`;
      el.style.opacity = String(ov.opacity);
      el.innerHTML = `<img src="${ov.url}" alt="${escapeHtml(ov.name)}" />`;

      el.addEventListener("mousedown", (e) => {
        e.stopPropagation();
        state.selectedOverlayId = ov.id;
        state.selectedClipId = null;
        state.selectedClipIds.clear();
        state.selectedMediaIds.clear();
        updateInspector();
        renderStageOverlays();

        const box = $("stage-canvas-box").getBoundingClientRect();
        const startX = e.clientX;
        const startY = e.clientY;
        const origX = ov.xPct;
        const origY = ov.yPct;

        function onMove(ev) {
          const dxPct = ((ev.clientX - startX) / Math.max(1, box.width)) * 100;
          const dyPct = ((ev.clientY - startY) / Math.max(1, box.height)) * 100;
          ov.xPct = Math.max(0, Math.min(100 - ov.widthPct, round3(origX + dxPct)));
          ov.yPct = Math.max(0, Math.min(92, round3(origY + dyPct)));
          el.style.left = `${ov.xPct}%`;
          el.style.top = `${ov.yPct}%`;
        }
        function onUp() {
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
        }
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
      });

      layer.appendChild(el);
    }
  }

  async function pickStickerFiles() {
    if (hasPyWebView() && window.pywebview.api.pick_image_files) {
      const picked = await window.pywebview.api.pick_image_files();
      if (picked && picked.length) await importStickerPaths(picked);
    } else {
      $("input-import-sticker").click();
    }
  }

  $("btn-import-sticker").addEventListener("click", pickStickerFiles);
  $("sticker-empty").addEventListener("click", pickStickerFiles);
  $("input-import-sticker").addEventListener("change", async (e) => {
    const paths = await resolveDroppedFiles(e.target.files);
    await importStickerPaths(paths);
    e.target.value = "";
  });

  const stickerDrop = $("sticker-dropzone");
  stickerDrop.addEventListener("dragover", (e) => {
    e.preventDefault();
    stickerDrop.classList.add("dragover");
  });
  stickerDrop.addEventListener("dragleave", () => stickerDrop.classList.remove("dragover"));
  stickerDrop.addEventListener("drop", async (e) => {
    e.preventDefault();
    stickerDrop.classList.remove("dragover");
    try {
      const paths = await resolveDroppedFiles(e.dataTransfer.files);
      await importStickerPaths(paths);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // Sticker Inspector controls
  $("st-insp-size").addEventListener("input", (e) => {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    if (!ov) return;
    ov.widthPct = Number(e.target.value);
    $("st-insp-size-val").textContent = `${Math.round(ov.widthPct)}%`;
    renderStageOverlays();
  });
  $("st-insp-opacity").addEventListener("input", (e) => {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    if (!ov) return;
    ov.opacity = Number(e.target.value) / 100;
    $("st-insp-opacity-val").textContent = `${Math.round(ov.opacity * 100)}%`;
    renderStageOverlays();
  });
  $("btn-st-tl").addEventListener("click", () => {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    if (ov) { ov.xPct = 3; ov.yPct = 4; renderStageOverlays(); }
  });
  $("btn-st-tr").addEventListener("click", () => {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    if (ov) { ov.xPct = Math.max(0, 97 - ov.widthPct); ov.yPct = 4; renderStageOverlays(); }
  });
  $("btn-st-bl").addEventListener("click", () => {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    if (ov) { ov.xPct = 3; ov.yPct = 78; renderStageOverlays(); }
  });
  $("btn-st-br").addEventListener("click", () => {
    const ov = state.stageOverlays.find((o) => o.id === state.selectedOverlayId);
    if (ov) { ov.xPct = Math.max(0, 97 - ov.widthPct); ov.yPct = 78; renderStageOverlays(); }
  });
  $("btn-st-remove").addEventListener("click", removeSelectedOverlay);

  // ---------------- Auto-Update 1-Click Popup ----------------
  async function checkForUpdates() {
    try {
      const info = await api("/api/update/check");
      if (info && info.available) {
        $("update-version-label").textContent = `v${info.current_version} → v${info.latest_version}`;
        $("update-notes").textContent = info.notes || "Bản cập nhật mới đã sẵn sàng.";
        $("update-progress-box").classList.add("hidden");
        $("btn-apply-update").disabled = false;
        $("update-modal").classList.remove("hidden");
      }
    } catch (_) {}
  }

  $("btn-close-update").addEventListener("click", () => $("update-modal").classList.add("hidden"));
  $("btn-later-update").addEventListener("click", () => $("update-modal").classList.add("hidden"));

  $("btn-apply-update").addEventListener("click", async () => {
    $("btn-apply-update").disabled = true;
    $("update-progress-box").classList.remove("hidden");
    try {
      await api("/api/update/start", { method: "POST" });
      pollUpdateProgress();
    } catch (err) {
      $("update-message").textContent = err.message;
      $("btn-apply-update").disabled = false;
    }
  });

  async function pollUpdateProgress() {
    try {
      const st = await api("/api/update/status");
      $("update-message").textContent = st.message || "Đang cập nhật...";
      $("update-pct").textContent = `${Math.round(st.progress || 0)}%`;
      $("update-bar-fill").style.width = `${st.progress || 0}%`;
      if (st.updating) {
        setTimeout(pollUpdateProgress, 450);
      } else if (st.error) {
        $("btn-apply-update").disabled = false;
      }
    } catch (_) {
      // Server exited cleanly to swap VocalForge.exe and relaunch
      $("update-message").textContent = "Đang khởi động lại phiên bản mới...";
      $("update-pct").textContent = "100%";
      $("update-bar-fill").style.width = "100%";
    }
  }

  // ---------------- Boot ----------------
  window.addEventListener("resize", () => renderTimeline());
  renderTimeline();
  refreshEnvironment(true);
  refreshJobs();
  refreshTtsJobs();
  setTimeout(checkForUpdates, 1500);
  setInterval(() => {
    if (state.jobs.some((j) => ["running", "merging", "queued"].includes(j.status))) {
      refreshJobs();
    }
    if (ttsState.jobs.some((j) => ["running", "merging", "queued"].includes(j.status))) {
      refreshTtsJobs();
    }
  }, 1500);
})();
