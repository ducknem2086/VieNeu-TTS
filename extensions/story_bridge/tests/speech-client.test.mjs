import assert from "node:assert/strict";
import test from "node:test";

const { createSpeechClient } = await import("../client/speech-client.js");
const wav = new Uint8Array([82, 73, 70, 70, 4, 0, 0, 0, 87, 65, 86, 69]);

function audioResponse(options = {}) {
  return new Response(wav, {
    status: options.status ?? 200,
    headers: {
      "Content-Type": options.contentType ?? "audio/wav",
      "X-Audio-Id": "audio-123",
      "X-Audio-Expires-At": "2026-09-29T12:00:00Z",
    },
  });
}

function withBrowser(run, { blocked = false } = {}) {
  const oldWindow = globalThis.window;
  const oldCreate = URL.createObjectURL;
  const oldRevoke = URL.revokeObjectURL;
  const events = [];
  const tab = {
    closed: false,
    location: "about:blank",
    close() {
      this.closed = true;
      events.push("close");
    },
  };
  globalThis.window = {
    open(url) {
      events.push(`open:${url}`);
      return blocked ? null : tab;
    },
  };
  URL.createObjectURL = (blob) => {
    events.push(`blob:${blob.type}`);
    return "blob:story-test";
  };
  URL.revokeObjectURL = (url) => events.push(`revoke:${url}`);
  return Promise.resolve()
    .then(() => run({ events, tab }))
    .finally(() => {
      globalThis.window = oldWindow;
      URL.createObjectURL = oldCreate;
      URL.revokeObjectURL = oldRevoke;
    });
}

test("synthesize sends JSON payload and returns WAV bytes with metadata", async () => {
  const controller = new AbortController();
  let seen;
  const client = createSpeechClient({
    baseUrl: "http://127.0.0.1:8002/",
    fetchImpl: async (url, init) => {
      seen = { url, init };
      return audioResponse();
    },
  });
  const result = await client.synthesize(
    { text: "Xin chào", temperature: 0.7 },
    { signal: controller.signal },
  );
  assert.equal(seen.url, "http://127.0.0.1:8002/api/v1/speech");
  assert.equal(seen.init.method, "POST");
  assert.equal(seen.init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(seen.init.body), {
    text: "Xin chào",
    temperature: 0.7,
  });
  assert.equal(seen.init.signal, controller.signal);
  assert.equal(result.blob.type, "audio/wav");
  assert.deepEqual(new Uint8Array(await result.blob.arrayBuffer()), wav);
  assert.equal(result.audioId, "audio-123");
  assert.equal(result.expiresAt, "2026-09-29T12:00:00Z");
});

test("synthesize reports JSON API details and plain-text HTTP failures", async () => {
  const jsonClient = createSpeechClient({
    fetchImpl: async () =>
      new Response(
        JSON.stringify({ detail: [{ msg: "text must be nonblank" }] }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      ),
  });
  await assert.rejects(
    jsonClient.synthesize({ text: "" }),
    /422.*text must be nonblank/,
  );
  const textClient = createSpeechClient({
    fetchImpl: async () =>
      new Response("gateway unavailable", {
        status: 503,
        headers: { "Content-Type": "text/plain" },
      }),
  });
  await assert.rejects(
    textClient.synthesize({ text: "Hi" }),
    /503.*gateway unavailable/,
  );
});

test("synthesize preserves a long JSON error detail", async () => {
  const client = createSpeechClient({
    fetchImpl: async () =>
      new Response(JSON.stringify({ detail: `${"x".repeat(600)} endpoint failed` }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }),
  });
  await assert.rejects(client.synthesize({ text: "Hi" }), /endpoint failed/);
});

test("synthesize rejects a successful non-audio response", async () => {
  const client = createSpeechClient({
    fetchImpl: async () =>
      new Response('{"detail":"oops"}', {
        headers: { "Content-Type": "application/json" },
      }),
  });
  await assert.rejects(
    client.synthesize({ text: "Hi" }),
    /audio\/wav|audio response/i,
  );
});

test("openInNewTab reserves a tab before fetch and navigates to MIME-correct Blob URL", async () => {
  await withBrowser(async ({ events, tab }) => {
    const client = createSpeechClient({
      fetchImpl: async () => {
        events.push("fetch");
        return audioResponse();
      },
    });
    const promise = client.openInNewTab({ text: "Xin chào" });
    assert.deepEqual(events, ["open:about:blank", "fetch"]);
    const result = await promise;
    assert.equal(result.window, tab);
    assert.equal(result.url, "blob:story-test");
    assert.equal(tab.location, "blob:story-test");
    assert.equal(result.expiresAt, "2026-09-29T12:00:00Z");
    assert.ok(result.blob instanceof Blob);
    assert.ok(!events.some((event) => event.startsWith("revoke:")));
    result.dispose();
    assert.ok(events.includes("revoke:blob:story-test"));
  });
});

test("blocked popup avoids network and failed requests close their placeholder", async () => {
  await withBrowser(
    async ({ events }) => {
      const client = createSpeechClient({
        fetchImpl: async () => {
          events.push("fetch");
          return audioResponse();
        },
      });
      await assert.rejects(
        client.openInNewTab({ text: "Hi" }),
        /popup|pop-up|cửa sổ/i,
      );
      assert.deepEqual(events, ["open:about:blank"]);
    },
    { blocked: true },
  );
  await withBrowser(async ({ tab }) => {
    const client = createSpeechClient({
      fetchImpl: async () => new Response("busy", { status: 429 }),
    });
    await assert.rejects(client.openInNewTab({ text: "Hi" }), /429.*busy/);
    assert.equal(tab.closed, true);
  });
});

test("closed child tab releases its Blob URL and dispose is idempotent", async () => {
  await withBrowser(async ({ events, tab }) => {
    const client = createSpeechClient({
      fetchImpl: async () => audioResponse(),
    });
    const result = await client.openInNewTab({ text: "Hi" });
    tab.closed = true;
    await new Promise((resolve) => setTimeout(resolve, 180));
    result.dispose();
    assert.equal(
      events.filter((event) => event === "revoke:blob:story-test").length,
      1,
    );
  });
});
