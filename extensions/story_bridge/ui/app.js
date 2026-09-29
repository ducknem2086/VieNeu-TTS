import { createSpeechClient } from "/client/speech-client.js";

const client = createSpeechClient();
const $ = (id) => document.getElementById(id);
const form = $("story-form");
const text = $("story-text");
const voice = $("voice-select");
const start = $("start");
const load = $("load-model");
const reopen = $("reopen");
const status = $("status");
const activeTabs = new Set();
let latestBlob = null;
let busy = false;
let modelLoaded = false;

function setStatus(message, kind = "info") {
  $("status-text").textContent = message;
  status.dataset.kind = kind;
}

function setReadiness(message, state) {
  $("readiness-text").textContent = message;
  $("readiness").dataset.state = state;
}

function setBusy(value) {
  busy = value;
  start.disabled = value;
  load.disabled = value;
  reopen.disabled = value;
  $("workspace").setAttribute("aria-busy", String(value));
  start.querySelector("span").textContent = value
    ? "Đang tạo âm thanh..."
    : "Bắt đầu";
}

function readableError(response, body) {
  let detail = body;
  try {
    const json = JSON.parse(body);
    detail =
      typeof json.detail === "string"
        ? json.detail
        : JSON.stringify(json.detail ?? json);
  } catch {
    /* Plain text is already readable. */
  }
  return new Error(`HTTP ${response.status}: ${detail || response.statusText}`);
}

async function fetchJson(path, options) {
  const response = await fetch(path, options);
  const body = await response.text();
  if (!response.ok) throw readableError(response, body);
  return JSON.parse(body);
}

async function refreshVoices() {
  const selected = voice.value;
  const data = await fetchJson("/api/v1/voices");
  voice.replaceChildren(new Option("Tự động · giọng mặc định", ""));
  for (const entry of data.voices) voice.add(new Option(entry.name, entry.id));
  if ([...voice.options].some((option) => option.value === selected))
    voice.value = selected;
}

async function checkHealth() {
  try {
    const health = await fetchJson("/health");
    modelLoaded = health.model_loaded;
    if (modelLoaded) {
      setReadiness("Sẵn sàng", "ready");
      setStatus(
        "Mô hình đã sẵn sàng. Hãy nhập nội dung để bắt đầu.",
        "success",
      );
      try {
        await refreshVoices();
      } catch (error) {
        setStatus(`Chưa tải được danh sách giọng: ${error.message}`, "error");
      }
    } else {
      setReadiness("Chưa tải mô hình", "waiting");
      setStatus(
        "Bạn có thể tải mô hình trước hoặc bắt đầu để hệ thống tự tải.",
      );
    }
  } catch (error) {
    setReadiness("Chưa kết nối", "error");
    setStatus(`Không kết nối được dịch vụ: ${error.message}`, "error");
  }
}

load.addEventListener("click", async () => {
  if (busy) return;
  setBusy(true);
  setReadiness("Đang tải", "checking");
  setStatus("Đang tải mô hình...");
  try {
    const result = await fetchJson("/api/v1/model/load", { method: "POST" });
    if (!result.model_loaded) throw new Error("Mô hình chưa sẵn sàng.");
    modelLoaded = true;
    setReadiness("Sẵn sàng", "ready");
    setStatus("Mô hình đã sẵn sàng. Bạn có thể chọn giọng đọc.", "success");
    try {
      await refreshVoices();
    } catch (error) {
      setStatus(
        `Đã tải mô hình, nhưng chưa tải được danh sách giọng: ${error.message}`,
        "error",
      );
    }
  } catch (error) {
    setReadiness("Chưa sẵn sàng", "error");
    setStatus(`Không tải được mô hình: ${error.message}`, "error");
  } finally {
    setBusy(false);
  }
});

text.addEventListener("input", () => {
  $("character-count").textContent =
    `${text.value.length.toLocaleString("vi-VN")} / 20.000 ký tự`;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const content = text.value.trim();
  if (!content) {
    setStatus("Hãy nhập nội dung trước khi bắt đầu.", "error");
    text.focus();
    return;
  }
  const payload = {
    text: content,
    temperature: Number($("temperature").value),
    max_chars_per_chunk: Number($("max-chars").value),
    batch_size: Number($("batch-size").value),
    use_batch: $("use-batch").checked,
  };
  if (voice.value) payload.voice_id = voice.value;
  setBusy(true);
  setStatus("Đang tạo âm thanh. Thẻ mới sẽ mở khi hoàn tất...");
  try {
    // The client reserves the tab synchronously before its first await.
    const resultPromise = client.openInNewTab(payload);
    const result = await resultPromise;
    activeTabs.add(result.dispose);
    latestBlob = result.blob;
    reopen.hidden = false;
    setStatus(
      "Âm thanh đã mở trong thẻ mới. Bạn có thể nghe lại kết quả gần nhất.",
      "success",
    );
    if (!modelLoaded) {
      modelLoaded = true;
      setReadiness("Sẵn sàng", "ready");
      try {
        await refreshVoices();
      } catch {
        /* Audio remains usable without the voice list. */
      }
    }
  } catch (error) {
    setStatus(`Không tạo được âm thanh: ${error.message}`, "error");
  } finally {
    setBusy(false);
  }
});

reopen.addEventListener("click", () => {
  if (busy || !latestBlob) return;
  try {
    const tab = client.openBlobInNewTab(latestBlob);
    activeTabs.add(tab.dispose);
    setStatus("Đã mở lại âm thanh gần nhất trong thẻ mới.", "success");
  } catch (error) {
    setStatus(`Không mở được thẻ âm thanh: ${error.message}`, "error");
  }
});

window.addEventListener("beforeunload", () => {
  for (const dispose of activeTabs) dispose();
});

checkHealth();
