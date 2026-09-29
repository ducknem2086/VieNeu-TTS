import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import { createSpeechClient } from "../client/speech-client.js";

const source = (await readFile(new URL("../ui/app.js", import.meta.url), "utf8"))
  .replace(/^import .*;\r?\n/, "");

function page(fetchImpl) {
  const elements = new Map();
  const getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: "hello", checked: true, dataset: {}, options: [], listeners: {},
      label: {}, addEventListener(event, callback) { this.listeners[event] = callback; },
      querySelector() { return this.label; }, setAttribute() {},
      replaceChildren(...options) { this.options = options; },
      add(option) { this.options.push(option); },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    createSpeechClient: () => createSpeechClient({ fetchImpl }),
    document: { getElementById }, window: { addEventListener() {} },
    fetch: fetchImpl, Option: function (name, value) { this.value = value; },
  });
  vm.runInContext(source, context);
  return { get: getElementById, context };
}

test("closing audio tabs prunes UI tracking while latest downloaded Blob can reopen", async () => {
  const originalWindow = globalThis.window;
  const originalInterval = globalThis.setInterval;
  const originalClear = globalThis.clearInterval;
  const watchers = new Set();
  const tabs = [];
  globalThis.window = { open() {
    const tab = { closed: false, close() { this.closed = true; } };
    tabs.push(tab);
    return tab;
  } };
  globalThis.setInterval = (callback) => { watchers.add(callback); return callback; };
  globalThis.clearInterval = (callback) => watchers.delete(callback);
  let requests = 0;
  const fetchImpl = async (url) => {
    if (url.endsWith("/speech")) {
      requests++;
      return new Response("RIFF", { headers: { "Content-Type": "audio/wav" } });
    }
    return Response.json(url === "/health" ? { model_loaded: true } : { voices: [] });
  };
  const { get, context } = page(fetchImpl);
  try {
    for (let index = 0; index < 3; index++) {
      await get("story-form").listeners.submit({ preventDefault() {} });
      assert.equal(vm.runInContext("activeTabs.size", context), 1);
      tabs.at(-1).close();
      for (const callback of watchers) callback();
      assert.equal(vm.runInContext("activeTabs.size", context), 0);
    }
    get("reopen").listeners.click();
    assert.equal(requests, 3);
    assert.match(tabs.at(-1).location, /^blob:/);
    assert.equal(vm.runInContext("activeTabs.size", context), 1);
    tabs.at(-1).close();
    for (const callback of watchers) callback();
    assert.equal(vm.runInContext("activeTabs.size", context), 0);
    get("reopen").listeners.click();
    vm.runInContext("for (const dispose of activeTabs) dispose()", context);
    assert.equal(vm.runInContext("activeTabs.size", context), 0);
    assert.equal(watchers.size, 0);
  } finally {
    vm.runInContext("for (const dispose of activeTabs) dispose()", context);
    globalThis.window = originalWindow;
    globalThis.setInterval = originalInterval;
    globalThis.clearInterval = originalClear;
  }
});

test("loading a model does not label the disabled action as audio synthesis", async () => {
  let finishLoad;
  const { get } = page(async (url) => {
    if (url.endsWith("/load")) return new Promise((resolve) => { finishLoad = resolve; });
    return Response.json(url === "/health" ? { model_loaded: false } : { voices: [] });
  });
  const pending = get("load-model").listeners.click();
  assert.equal(get("start").disabled, true);
  const loadingLabel = get("start").label.textContent;
  finishLoad(Response.json({ model_loaded: true }));
  await pending;
  assert.doesNotMatch(loadingLabel, /tạo âm thanh/i);
  assert.equal(get("start").disabled, false);
});
