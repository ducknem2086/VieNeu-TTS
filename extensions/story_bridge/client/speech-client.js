const WAV_TYPES = new Set([
  "audio/wav",
  "audio/x-wav",
  "audio/wave",
  "audio/vnd.wave",
]);

function responseDetail(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value))
    return value.map((entry) => responseDetail(entry.msg ?? entry)).join("; ");
  if (value && typeof value === "object")
    return value.msg ?? JSON.stringify(value);
  return "";
}

async function httpError(response) {
  const body = await response.text();
  let message = body;
  if (response.headers.get("Content-Type")?.toLowerCase().includes("json")) {
    try {
      const json = JSON.parse(body);
      message = responseDetail(json.detail ?? json.message ?? json);
    } catch {
      // A malformed JSON error still has useful status and response text.
    }
  }
  return new Error(
    `HTTP ${response.status}: ${(message || response.statusText || "Request failed").slice(0, 1000)}`,
  );
}

function reserveTab() {
  if (typeof window === "undefined" || typeof window.open !== "function") {
    throw new Error("Opening an audio tab requires a browser window.");
  }
  const tab = window.open("about:blank", "_blank");
  if (!tab)
    throw new Error(
      "Popup blocked. Allow pop-ups for this site and try again.",
    );
  return tab;
}

function showBlob(tab, blob, onDispose) {
  const url = URL.createObjectURL(
    blob.type === "audio/wav" ? blob : new Blob([blob], { type: "audio/wav" }),
  );
  let released = false;
  const watcher = setInterval(() => {
    if (tab.closed) dispose();
  }, 100);
  function dispose() {
    if (released) return;
    released = true;
    clearInterval(watcher);
    URL.revokeObjectURL(url);
    onDispose?.(dispose);
  }
  try {
    tab.location = url;
  } catch (error) {
    dispose();
    throw error;
  }
  return { url, window: tab, dispose };
}

export function createSpeechClient({
  baseUrl = "",
  fetchImpl = globalThis.fetch,
} = {}) {
  if (typeof fetchImpl !== "function")
    throw new TypeError("fetchImpl must be a function");
  const endpoint = `${baseUrl.replace(/\/$/, "")}/api/v1/speech`;

  async function synthesize(payload, { signal } = {}) {
    const response = await fetchImpl(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
    if (!response.ok) throw await httpError(response);
    const contentType = response.headers
      .get("Content-Type")
      ?.split(";", 1)[0]
      .trim()
      .toLowerCase();
    if (!WAV_TYPES.has(contentType)) {
      throw new Error(
        `Expected an audio/wav response, received ${contentType || "no Content-Type"}.`,
      );
    }
    const received = await response.blob();
    if (received.size === 0) throw new Error("The audio response was empty.");
    return {
      blob:
        received.type === "audio/wav"
          ? received
          : new Blob([received], { type: "audio/wav" }),
      audioId: response.headers.get("X-Audio-Id"),
      expiresAt: response.headers.get("X-Audio-Expires-At"),
    };
  }

  // Also lets a page reopen its latest downloaded result without another speech request.
  function openBlobInNewTab(blob, { onDispose } = {}) {
    const tab = reserveTab();
    try {
      return showBlob(tab, blob, onDispose);
    } catch (error) {
      tab.close();
      throw error;
    }
  }

  async function openInNewTab(payload, { signal, onDispose } = {}) {
    const tab = reserveTab(); // Happens in the synchronous click stack.
    try {
      const result = await synthesize(payload, { signal });
      return {
        ...showBlob(tab, result.blob, onDispose),
        blob: result.blob,
        audioId: result.audioId,
        expiresAt: result.expiresAt,
      };
    } catch (error) {
      tab.close();
      throw error;
    }
  }

  return { synthesize, openInNewTab, openBlobInNewTab };
}
