const API_BASE = "";

const state = {
  user: readServerRenderedUser(),
  audioFile: null,
  audioId: null,
  rawTranscript: "",
  diagnosisOptions: [],
  confirmed: false,
  generatedAt: null,
  recordId: null,
  evidence: [],
  mediaRecorder: null,
  mediaStream: null,
  audioContext: null,
  audioSource: null,
  audioProcessor: null,
  asrSocket: null,
  streamingFinalText: "",
  streamingPartialText: "",
  chunkQueue: [],
  processingChunk: false,
  recordingStartedAt: null,
  recordingActive: false,
  segmentTimer: null,
  currentSegmentChunks: [],
};

const demoTranscript = `医生：您这次主要哪里不舒服？
患者：我咳嗽三天了，还有点发烧。
医生：最高体温多少？
患者：昨天晚上量到三十八度五。
医生：有没有咳痰、胸痛或者喘不上气？
患者：有一点黄痰，没有胸痛，也没有呼吸困难。
医生：以前有高血压、糖尿病吗？
患者：没有高血压，也没有糖尿病。
医生：有没有药物过敏？
患者：我对青霉素过敏。
医生：这几天自己吃过什么药吗？
患者：吃过一次布洛芬，退烧效果还可以。`;

const el = {
  workspaceApp: document.querySelector("#workspaceApp"),
  visitStatus: document.querySelector("#visitStatus"),
  logoutBtn: document.querySelector("#logoutBtn"),
  doctorName: document.querySelector("#doctorName"),
  auditBtn: document.querySelector("#auditBtn"),
  auditPanel: document.querySelector("#auditPanel"),
  auditList: document.querySelector("#auditList"),
  closeAuditBtn: document.querySelector("#closeAuditBtn"),
  audioFile: document.querySelector("#audioFile"),
  audioMeta: document.querySelector("#audioMeta"),
  transcribeBtn: document.querySelector("#transcribeBtn"),
  startRecordBtn: document.querySelector("#startRecordBtn"),
  stopRecordBtn: document.querySelector("#stopRecordBtn"),
  recordingHint: document.querySelector("#recordingHint"),
  demoBtn: document.querySelector("#demoBtn"),
  clearTranscriptBtn: document.querySelector("#clearTranscriptBtn"),
  transcriptInput: document.querySelector("#transcriptInput"),
  generateBtn: document.querySelector("#generateBtn"),
  confirmBtn: document.querySelector("#confirmBtn"),
  exportTxtBtn: document.querySelector("#exportTxtBtn"),
  exportJsonBtn: document.querySelector("#exportJsonBtn"),
  printBtn: document.querySelector("#printBtn"),
  printRecord: document.querySelector("#printRecord"),
  chiefComplaint: document.querySelector("#chiefComplaint"),
  diagnosis: document.querySelector("#diagnosis"),
  diagnosisOptions: document.querySelector("#diagnosisOptions"),
  hpi: document.querySelector("#hpi"),
  pastHistory: document.querySelector("#pastHistory"),
  allergyHistory: document.querySelector("#allergyHistory"),
  physicalExam: document.querySelector("#physicalExam"),
  plan: document.querySelector("#plan"),
  missingList: document.querySelector("#missingList"),
  riskList: document.querySelector("#riskList"),
  evidenceList: document.querySelector("#evidenceList"),
};

const patientFields = {
  patientName: document.querySelector("#patientName"),
  patientGender: document.querySelector("#patientGender"),
  patientAge: document.querySelector("#patientAge"),
  department: document.querySelector("#department"),
  visitNo: document.querySelector("#visitNo"),
};

el.logoutBtn.addEventListener("click", () => {
  state.user = null;
  fetch("/api/auth/logout", { method: "POST" }).finally(() => {
    window.location.href = "/login";
  });
});

el.auditBtn.addEventListener("click", async () => {
  try {
    const result = await api("/api/audit-logs?limit=100");
    el.auditList.innerHTML = result.items
      .map((item) => `<div class="audit-item"><strong>${escapeHtml(item.action)}</strong> · ${escapeHtml(item.user_id || "-")} · ${escapeHtml(item.resource_type)}:${escapeHtml(item.resource_id || "-")}<br>${escapeHtml(item.created_at)}</div>`)
      .join("");
    el.auditPanel.hidden = false;
  } catch (error) {
    showError(error);
  }
});

el.closeAuditBtn.addEventListener("click", () => {
  el.auditPanel.hidden = true;
});

el.audioFile.addEventListener("change", () => {
  const file = el.audioFile.files?.[0];
  state.audioFile = file || null;
  state.audioId = null;
  el.audioMeta.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "未选择文件";
  el.transcribeBtn.disabled = !file;
  setStatus(file ? "录音已选择" : "待上传录音");
});

el.demoBtn.addEventListener("click", () => {
  el.transcriptInput.value = demoTranscript;
  state.rawTranscript = demoTranscript;
  setStatus("示例已载入");
});

el.clearTranscriptBtn.addEventListener("click", () => {
  el.transcriptInput.value = "";
  state.rawTranscript = "";
  setStatus("转写已清空");
});

el.transcribeBtn.addEventListener("click", async () => {
  if (!state.audioFile) return;

  try {
    setBusy(el.transcribeBtn, "上传中...");
    setStatus("录音上传中");
    const upload = await uploadAudio(state.audioFile);
    state.audioId = upload.audio_id;

    el.transcribeBtn.textContent = "转写中...";
    setStatus("语音转写中");
    const result = await api(`/api/audio/${state.audioId}/transcribe`, { method: "POST" });
    el.transcriptInput.value = cleanTranscriptForDisplay(result.transcript || "");
    state.rawTranscript = result.raw_transcript || result.transcript || "";
    setStatus("转写完成");
  } catch (error) {
    showError(error);
    setStatus("转写失败");
  } finally {
    setReady(el.transcribeBtn, "开始转写");
  }
});

el.startRecordBtn.addEventListener("click", async () => {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext && !window.webkitAudioContext) {
    alert("当前浏览器不支持实时流式录音，请使用 Chrome、Edge 或改用上传录音。");
    return;
  }

  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.recordingActive = true;
    state.recordingStartedAt = new Date();
    state.streamingFinalText = el.transcriptInput.value.trim();
    state.streamingPartialText = "";
    el.startRecordBtn.disabled = true;
    el.stopRecordBtn.disabled = false;
    el.recordingHint.textContent = "实时录音中：正在边说边转写，文本会持续更新。";
    el.recordingHint.classList.add("active");
    setStatus("实时流式转写连接中");
    await startStreamingAsr();
  } catch (error) {
    showError(error);
    setStatus("录音失败");
  }
});

el.stopRecordBtn.addEventListener("click", () => {
  stopStreamingAsr();
  el.startRecordBtn.disabled = false;
  el.stopRecordBtn.disabled = true;
  el.recordingHint.textContent = "录音已停止。";
  el.recordingHint.classList.remove("active");
  setStatus("录音已停止");
});

el.generateBtn.addEventListener("click", async () => {
  const transcript = el.transcriptInput.value.trim();
  if (!transcript) {
    alert("请先上传录音转写，或粘贴问诊对话。");
    return;
  }

  try {
    setBusy(el.generateBtn, "生成中...");
    setStatus("AI 正在整理转写并生成专业病历草稿");
    const result = await api("/api/emr/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient: getPatientInfo(),
        transcript,
      }),
    });

    fillEmrDraft(result.emr);
    renderQuality(result);

    state.generatedAt = new Date();
    state.confirmed = false;
    state.recordId = null;
    el.confirmBtn.disabled = false;
    el.exportTxtBtn.disabled = true;
    el.exportJsonBtn.disabled = true;
    el.printBtn.disabled = true;
    const seconds = result.processing_seconds ? `，耗时 ${result.processing_seconds} 秒` : "";
    setStatus(`草稿待确认${seconds}`);
  } catch (error) {
    showError(error);
    setStatus("生成失败");
  } finally {
    setReady(el.generateBtn, "生成草稿", false);
  }
});

el.confirmBtn.addEventListener("click", async () => {
  try {
    setBusy(el.confirmBtn, "确认中...");
    const result = await api("/api/emr/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient: getPatientInfo(),
        transcript: el.transcriptInput.value,
        raw_transcript: state.rawTranscript || el.transcriptInput.value,
        emr: getEmrFields(),
        evidence: state.evidence,
      }),
    });

    state.recordId = result.record_id;
    state.confirmed = true;
    el.exportTxtBtn.disabled = false;
    el.exportJsonBtn.disabled = false;
    el.printBtn.disabled = false;
    setStatus("医生已确认");
  } catch (error) {
    showError(error);
    setStatus("确认失败");
  } finally {
    el.confirmBtn.textContent = "医生确认";
    el.confirmBtn.disabled = false;
  }
});

el.exportTxtBtn.addEventListener("click", () => {
  if (!state.recordId) return;
  downloadProtected(`/api/emr/${state.recordId}/export.txt`, `${patientFields.visitNo.value}-门诊病历.txt`);
});

el.exportJsonBtn.addEventListener("click", () => {
  if (!state.recordId) return;
  downloadProtected(`/api/emr/${state.recordId}/export.json`, `${patientFields.visitNo.value}-门诊病历.json`);
});

el.printBtn.addEventListener("click", () => {
  el.auditPanel.hidden = true;
  renderPrintRecord();
  window.print();
});

async function uploadAudio(file) {
  const formData = new FormData();
  formData.append("file", file);
  return api("/api/audio", {
    method: "POST",
    body: formData,
  });
}

async function startStreamingAsr() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/asr/stream`);
  socket.binaryType = "arraybuffer";
  state.asrSocket = socket;

  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("实时转写连接失败")), { once: true });
  });

  socket.addEventListener("message", (event) => {
    try {
      const data = JSON.parse(event.data);
      handleStreamingAsrMessage(data);
    } catch (error) {
      console.error(error);
    }
  });
  socket.addEventListener("close", () => {
    state.asrSocket = null;
    if (state.recordingActive) {
      setStatus("实时转写连接已断开");
      stopStreamingAsr();
    }
  });

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  state.audioContext = new AudioCtx();
  state.audioSource = state.audioContext.createMediaStreamSource(state.mediaStream);
  state.audioProcessor = state.audioContext.createScriptProcessor(4096, 1, 1);
  state.audioProcessor.onaudioprocess = (event) => {
    if (!state.recordingActive || state.asrSocket?.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const pcm = downsampleTo16BitPcm(input, state.audioContext.sampleRate, 16000);
    if (pcm.byteLength > 0) {
      state.asrSocket.send(pcm);
    }
  };
  state.audioSource.connect(state.audioProcessor);
  state.audioProcessor.connect(state.audioContext.destination);
  setStatus("实时流式转写中");
}

function stopStreamingAsr() {
  state.recordingActive = false;
  stopAudioCapture();
  if (state.asrSocket?.readyState === WebSocket.OPEN) {
    state.asrSocket.send("__stop__");
  } else {
    state.asrSocket = null;
    state.streamingPartialText = "";
  }
}

function stopAudioCapture() {
  if (state.audioProcessor) {
    state.audioProcessor.disconnect();
    state.audioProcessor.onaudioprocess = null;
    state.audioProcessor = null;
  }
  if (state.audioSource) {
    state.audioSource.disconnect();
    state.audioSource = null;
  }
  if (state.audioContext) {
    state.audioContext.close();
    state.audioContext = null;
  }
  state.mediaStream?.getTracks().forEach((track) => track.stop());
  state.mediaStream = null;
}

function handleStreamingAsrMessage(data) {
  if (data.type === "error") {
    appendTranscript(`系统：实时转写错误：${data.message || "未知错误"}`);
    return;
  }
  if (data.type === "partial") {
    state.streamingPartialText = cleanTranscriptForDisplay(data.text || "");
    renderStreamingTranscript();
    return;
  }
  if (data.type === "final") {
    const text = cleanTranscriptForDisplay(data.text || "");
    if (text) {
      const existing = state.streamingFinalText.trim();
      if (!existing) {
        state.streamingFinalText = text;
      } else if (existing.includes(text)) {
        state.streamingFinalText = existing;
      } else if (text.includes(existing)) {
        state.streamingFinalText = text;
      } else {
        state.streamingFinalText = [existing, text].join("\n");
      }
      if (!state.rawTranscript.includes(text)) {
        appendRawTranscript(text);
      }
    }
    state.streamingPartialText = "";
    renderStreamingTranscript();
    setStatus("实时转写已更新");
    return;
  }
  if (data.type === "done") {
    state.streamingPartialText = "";
    renderStreamingTranscript();
    setStatus("实时录音转写完成");
    state.asrSocket?.close();
  }
}

function renderStreamingTranscript() {
  const pieces = [state.streamingFinalText];
  if (state.streamingPartialText) {
    pieces.push(`（识别中）${state.streamingPartialText}`);
  }
  el.transcriptInput.value = pieces.filter(Boolean).join("\n");
  el.transcriptInput.scrollTop = el.transcriptInput.scrollHeight;
}

function downsampleTo16BitPcm(input, inputSampleRate, outputSampleRate) {
  if (outputSampleRate === inputSampleRate) {
    return floatTo16BitPcm(input);
  }
  const ratio = inputSampleRate / outputSampleRate;
  const newLength = Math.floor(input.length / ratio);
  const result = new Float32Array(newLength);
  let offset = 0;
  for (let i = 0; i < newLength; i += 1) {
    const nextOffset = Math.round((i + 1) * ratio);
    let accum = 0;
    let count = 0;
    for (let j = offset; j < nextOffset && j < input.length; j += 1) {
      accum += input[j];
      count += 1;
    }
    result[i] = count ? accum / count : 0;
    offset = nextOffset;
  }
  return floatTo16BitPcm(result);
}

function floatTo16BitPcm(float32Array) {
  const buffer = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < float32Array.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, float32Array[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return buffer;
}

function startRecordingSegment() {
  if (!state.recordingActive || !state.mediaStream) return;

  const mimeType = getSupportedMimeType();
  state.currentSegmentChunks = [];
  state.mediaRecorder = new MediaRecorder(state.mediaStream, mimeType ? { mimeType } : undefined);

  state.mediaRecorder.addEventListener("dataavailable", (event) => {
    if (event.data && event.data.size > 0) {
      state.currentSegmentChunks.push(event.data);
    }
  });

  state.mediaRecorder.addEventListener("stop", () => {
    if (state.segmentTimer) {
      clearTimeout(state.segmentTimer);
      state.segmentTimer = null;
    }
    const type = state.mediaRecorder?.mimeType || mimeType || "audio/webm";
    if (state.currentSegmentChunks.length) {
      const chunk = new Blob(state.currentSegmentChunks, { type });
      state.chunkQueue.push(chunk);
      processChunkQueue();
    }
    state.currentSegmentChunks = [];

    if (state.recordingActive) {
      startRecordingSegment();
      return;
    }

    state.mediaStream?.getTracks().forEach((track) => track.stop());
    state.mediaStream = null;
    state.mediaRecorder = null;
  });

  state.mediaRecorder.start();
  state.segmentTimer = setTimeout(() => {
    if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
      state.mediaRecorder.stop();
    }
  }, 8000);
}

async function processChunkQueue() {
  if (state.processingChunk || !state.chunkQueue.length) return;
  state.processingChunk = true;

  while (state.chunkQueue.length) {
    const chunk = state.chunkQueue.shift();
    try {
      const extension = chunk.type.includes("ogg") ? "ogg" : chunk.type.includes("mp4") ? "m4a" : "webm";
      const file = new File([chunk], `realtime-${Date.now()}.${extension}`, { type: chunk.type || "audio/webm" });
      const formData = new FormData();
      formData.append("file", file);
      setStatus("实时转写中");
      const result = await api("/api/audio-chunks/transcribe", {
        method: "POST",
        body: formData,
      });

      if (result.skipped) {
        setStatus(result.skip_reason || "该片段无有效语音，已跳过");
        continue;
      }

      const text = (result.transcript || "").trim();
      const cleaned = cleanTranscriptForDisplay(text);
      if (cleaned && !cleaned.includes("未识别到有效语音")) {
        appendTranscript(cleaned);
        appendRawTranscript(result.raw_transcript || text);
        setStatus("转写已追加");
      }
    } catch (error) {
      console.error(error);
      appendTranscript(`系统：实时转写片段失败：${error.message || "未知错误"}`);
    }
  }

  state.processingChunk = false;
  if (state.mediaRecorder?.state === "recording") {
    setStatus("实时录音中");
  } else {
    setStatus("转写完成");
  }
}

function appendTranscript(text) {
  if (isDuplicateTranscript(text)) return;
  const prefix = el.transcriptInput.value.trim() ? "\n" : "";
  el.transcriptInput.value += `${prefix}${text}`;
  el.transcriptInput.scrollTop = el.transcriptInput.scrollHeight;
}

function appendRawTranscript(text) {
  const raw = String(text || "").trim();
  if (!raw) return;
  const prefix = state.rawTranscript.trim() ? "\n" : "";
  state.rawTranscript += `${prefix}${raw}`;
}

function cleanTranscriptForDisplay(text) {
  const cleaned = String(text)
    .replaceAll("�", "")
    .replace(/[］\]\)）】》>]{3,}/g, "")
    .replace(/[［\[\(（【《<]{3,}/g, "")
    .replace(/([，。！？；：、,.!?;:])\1{2,}/g, "$1")
    .replaceAll("１", "，")
    .replace(/\s+/g, " ")
    .trim();
  if (isBadTranscriptDisplay(cleaned)) return "";
  return toSimplifiedChinese(compressRepeatedDisplayText(cleaned));
}

function isBadTranscriptDisplay(text) {
  const compact = String(text || "").trim().toLowerCase();
  if (!compact) return true;
  const badPhrases = [
    "thank you for watching",
    "thanks for watching",
    "like and subscribe",
    "subscribe",
    "字幕组",
    "下期再见",
  ];
  if (badPhrases.some((phrase) => compact.includes(phrase))) return true;
  const hasChinese = /[\u4e00-\u9fff]/.test(text);
  return !hasChinese && /^[a-z\s.,!?'-]{3,80}$/i.test(text);
}

function toSimplifiedChinese(text) {
  const map = {
    醫: "医", 門: "门", 問: "问", 診: "诊", 歷: "历", 轉: "转",
    頭: "头", 暈: "晕", 陣: "阵", 發: "发", 續: "续", 悶: "闷",
    噁: "恶", 嘔: "呕", 過: "过", 黴: "霉", 檢: "检", 斷: "断",
    療: "疗", 體: "体", 溫: "温", 輔: "辅", 經: "经", 統: "统",
    腦: "脑", 頸: "颈", 電: "电", 質: "质", 規: "规", 圖: "图",
    語: "语", 識: "识", 燒: "烧", 氣: "气", 難: "难", 無: "无",
    認: "认", 聽: "听", 視: "视", 藥: "药", 處: "处", 記: "记",
    錄: "录", 風: "风", 險: "险", 隨: "随", 訪: "访",
  };
  return String(text).replace(/[醫門問診歷轉頭暈陣發續悶噁嘔過黴檢斷療體溫輔經統腦頸電質規圖語識燒氣難無認聽視藥處記錄風險隨訪]/g, (char) => map[char] || char);
}

function compressRepeatedDisplayText(text) {
  const compacted = compressRepeatedSubstrings(text);
  const parts = compacted.split(/[，。！？；、,.!?;:\n]+/).map((part) => part.trim()).filter(Boolean);
  const result = [];
  for (const part of parts) {
    const last = result[result.length - 1] || "";
    if (part === last) continue;
    if (part.length <= 14 && last.length <= 14 && (part.includes(last) || last.includes(part))) continue;
    result.push(part);
  }
  return result.join("，");
}

function compressRepeatedSubstrings(text) {
  let cleaned = String(text || "");
  for (let size = 2; size <= 10; size += 1) {
    cleaned = cleaned.replace(new RegExp(`([\\u4e00-\\u9fff]{${size}})(?:\\1){2,}`, "g"), "$1");
  }
  for (let size = 2; size <= 12; size += 1) {
    cleaned = cleaned.replace(new RegExp(`([\\u4e00-\\u9fff]{${size}})(?:[\\s，,、。；;：:]+\\1){1,}`, "g"), "$1");
  }
  const phrases = ["持续什么样", "还是持续什么样", "一阵一阵的", "阵发性头晕", "尤其是", "试试"];
  for (const phrase of phrases) {
    cleaned = cleaned.replace(new RegExp(`(${phrase})(?:[\\s，,、。；;：:]*\\1)+`, "g"), "$1");
  }
  return cleaned;
}

function isDuplicateTranscript(text) {
  const current = el.transcriptInput.value.trim();
  if (!current) return false;
  const tail = current.slice(-120);
  return tail.includes(text) || text.includes(tail.slice(-60));
}

function getSupportedMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function api(path, options = {}) {
  const fetchOptions = withAuth(options);
  delete fetchOptions.skipAuth;
  const response = await fetchWithRetry(`${API_BASE}${path}`, fetchOptions);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    const message = typeof payload === "string" ? payload : payload.detail || "请求失败";
    throw new Error(message);
  }

  return payload;
}

function withAuth(options = {}) {
  const headers = new Headers(options.headers || {});
  return { ...options, headers };
}

async function downloadProtected(path, filename) {
  const response = await fetchWithRetry(`${API_BASE}${path}`, withAuth({ method: "GET" }));
  if (!response.ok) {
    throw new Error("导出失败");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function fetchWithRetry(url, options, retries = 1) {
  try {
    return await fetch(url, options);
  } catch (error) {
    if (retries > 0) {
      await wait(800);
      return fetchWithRetry(url, options, retries - 1);
    }
    throw new Error(`网络请求失败，请确认后端服务仍在运行：${error.message || error}`);
  }
}

function fillEmrDraft(emr) {
  el.chiefComplaint.value = emr.chief_complaint || "";
  el.diagnosis.value = emr.diagnosis || "";
  state.diagnosisOptions = emr.diagnosis_options || [];
  renderDiagnosisOptions(state.diagnosisOptions);
  el.hpi.value = emr.history_of_present_illness || "";
  el.pastHistory.value = emr.past_history || "";
  el.allergyHistory.value = emr.allergy_history || "";
  el.physicalExam.value = emr.physical_exam || "";
  el.plan.value = emr.plan || "";
}

function renderQuality(data) {
  state.evidence = data.evidence || [];
  renderList(el.missingList, data.missing_items?.length ? data.missing_items : ["暂无明显缺失项"], "warning");
  renderList(el.riskList, data.risk_alerts?.length ? data.risk_alerts : ["暂无高风险提示"], "risk");

  if (!state.evidence.length) {
    el.evidenceList.textContent = "暂无可展示依据";
    return;
  }

  el.evidenceList.innerHTML = state.evidence
    .map((item) => `<div class="evidence-item"><strong>${escapeHtml(item.label)}：</strong>${escapeHtml(item.text)}</div>`)
    .join("");
}

function renderDiagnosisOptions(options) {
  if (!el.diagnosisOptions) return;
  const items = Array.isArray(options) ? options.filter((item) => item && item.name) : [];
  if (!items.length) {
    el.diagnosisOptions.textContent = "DeepSeek 未给出明确候选项，医生可直接编辑初步诊断。";
    return;
  }

  el.diagnosisOptions.innerHTML = items
    .map((item, index) => {
      const basis = item.basis ? `<span>${escapeHtml(item.basis)}</span>` : "";
      const checks = item.suggested_checks ? `<span>建议：${escapeHtml(item.suggested_checks)}</span>` : "";
      return `<button class="diagnosis-option" type="button" data-diagnosis-index="${index}"><strong>${escapeHtml(item.name)}</strong>${basis}${checks}</button>`;
    })
    .join("");
}

el.diagnosisOptions?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-diagnosis-index]");
  if (!button) return;
  const option = state.diagnosisOptions[Number(button.dataset.diagnosisIndex)];
  if (!option) return;
  const text = `初步考虑：${option.name}。${option.basis ? `依据：${option.basis}` : ""}${option.suggested_checks ? ` 建议完善：${option.suggested_checks}` : ""}`.trim();
  el.diagnosis.value = text;
  setStatus("已选择候选诊断，医生可继续编辑");
});

function renderList(target, items, className) {
  target.innerHTML = items.map((item) => `<li class="${className}">${escapeHtml(item)}</li>`).join("");
}

function getPatientInfo() {
  return {
    name: patientFields.patientName.value,
    gender: patientFields.patientGender.value,
    age: patientFields.patientAge.value,
    department: patientFields.department.value,
    visit_no: patientFields.visitNo.value,
  };
}

function getEmrFields() {
  return {
    chief_complaint: el.chiefComplaint.value,
    history_of_present_illness: el.hpi.value,
    past_history: el.pastHistory.value,
    allergy_history: el.allergyHistory.value,
    physical_exam: el.physicalExam.value,
    diagnosis: el.diagnosis.value,
    diagnosis_options: state.diagnosisOptions || [],
    plan: el.plan.value,
  };
}

function renderPrintRecord() {
  const patient = getPatientInfo();
  const emr = getEmrFields();
  const today = new Date().toLocaleString("zh-CN", { hour12: false });
  el.printRecord.innerHTML = `
    <article class="print-page">
      <header class="print-header">
        <h1>门诊电子病历</h1>
        <p>AI 辅助生成，医生确认后打印</p>
      </header>

      <section class="print-meta">
        <div><strong>患者姓名：</strong>${escapeHtml(patient.name)}</div>
        <div><strong>性别：</strong>${escapeHtml(patient.gender)}</div>
        <div><strong>年龄：</strong>${escapeHtml(patient.age)}</div>
        <div><strong>科室：</strong>${escapeHtml(patient.department)}</div>
        <div><strong>就诊号：</strong>${escapeHtml(patient.visit_no)}</div>
        <div><strong>打印时间：</strong>${escapeHtml(today)}</div>
      </section>

      ${printSection("主诉", emr.chief_complaint)}
      ${printSection("现病史", emr.history_of_present_illness)}
      ${printSection("既往史", emr.past_history)}
      ${printSection("过敏史", emr.allergy_history)}
      ${printSection("体格检查", emr.physical_exam)}
      ${printSection("初步诊断", emr.diagnosis)}
      ${printSection("处理意见", emr.plan)}

      <footer class="print-footer">
        <div>接诊医生签名：________________</div>
        <div>确认状态：${state.confirmed ? "医生已确认" : "未确认"}</div>
      </footer>
    </article>
  `;
}

function printSection(title, content) {
  return `
    <section class="print-section">
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(content || "未提及")}</p>
    </section>
  `;
}

function setStatus(text) {
  el.visitStatus.textContent = text;
}

function readServerRenderedUser() {
  const node = document.querySelector("#doctorName");
  const text = node?.textContent || "";
  if (!text || text === "未登录") return null;
  return { username: "cookie", role: "admin", doctor_name: text.replace("当前医生：", "").trim() };
}

function setBusy(button, text) {
  button.dataset.originalText = button.textContent;
  button.textContent = text;
  button.disabled = true;
}

function setReady(button, text, dependsOnAudio = true) {
  button.textContent = text || button.dataset.originalText || button.textContent;
  button.disabled = dependsOnAudio ? !state.audioFile : false;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showError(error) {
  console.error(error);
  alert(error.message || "操作失败，请刷新页面后重试。");
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
