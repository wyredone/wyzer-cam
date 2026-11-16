(function () {
  "use strict";

  const API = "/x/json-aiwatch.cgi";
  const MODELS_URL = "https://openrouter.ai/api/v1/models";
  const RECOMMENDED_MODEL = "google/gemini-2.5-flash-lite";
  const TEXT_FIELDS = ["endpoint", "api_key", "model", "camera_name", "context", "language",
    "extra_instructions", "active_from", "active_to"];
  const SWITCHES = ["enabled", "filter_empty", "send_photo", "fallback_on_error"];
  const NUMBERS = { cooldown: 60, daily_limit: 200, snapshot_channel: 1, max_image_kb: 600, timeout: 30, free_daily_limit: 45, max_attempts: 3 };
  const MAX_RULES = 10;
  const MAX_POOL = 10;
  const MODEL_ID = /^[A-Za-z0-9._:/-]+$/;

  const el = (id) => document.getElementById(id);
  const form = el("aiwatchForm");
  let original = {};
  let catalog = null; // { paid: [...], free: [...] } once loaded
  let lastStatus = null;

  // The page also works when main.js helpers are missing or change.
  function alertUser(type, message) {
    if (typeof window.showAlert === "function") window.showAlert(type, message);
    else if (type === "danger") window.alert(message);
  }
  function busy(message) {
    if (typeof window.showBusy === "function") window.showBusy(message);
  }
  function idle() {
    if (typeof window.hideBusy === "function") window.hideBusy();
  }

  async function getJson(url, options) {
    const response = await fetch(url, Object.assign({ headers: { Accept: "application/json" } }, options));
    let data = null;
    try {
      data = await response.json();
    } catch (e) {
      throw new Error("The camera sent an unreadable reply (HTTP " + response.status + ").");
    }
    if (!response.ok) throw new Error((data && data.error) || "HTTP " + response.status);
    return data;
  }

  function isOpenRouter() {
    try {
      const host = new URL(el("endpoint").value.trim() || "https://openrouter.ai/").hostname;
      return host === "openrouter.ai" || host.endsWith(".openrouter.ai");
    } catch (e) {
      return false;
    }
  }

  // --- model list from OpenRouter (fetched by the browser, not the camera) ---

  // OpenRouter uses a negative price for "varies" (routers); that counts as unknown.
  function perMillion(price) {
    const value = Number.parseFloat(price) * 1e6;
    return Number.isFinite(value) && value >= 0 ? value : NaN;
  }

  function parseCatalog(data) {
    const paid = [];
    const free = [];
    (data.data || []).forEach((m) => {
      const arch = m.architecture || {};
      const inputs = arch.input_modalities || [];
      const outputs = arch.output_modalities || ["text"];
      if (!MODEL_ID.test(m.id || "") || !inputs.includes("image")) return;
      if (outputs.length !== 1 || outputs[0] !== "text") return;
      if (m.id === "openrouter/free" || /:batch$/.test(m.id)) return;
      const input = perMillion(m.pricing && m.pricing.prompt);
      const output = perMillion(m.pricing && m.pricing.completion);
      const isFree = /:free$/.test(m.id) || (input === 0 && output === 0);
      const entry = { id: m.id, name: m.name || m.id, input, reasoning: /reasoning|thinking/i.test(m.id + " " + (m.name || "")) };
      (isFree ? free : paid).push(entry);
    });
    const priceKey = (m) => (Number.isFinite(m.input) ? m.input : Infinity); // unknown prices last
    paid.sort((a, b) => (priceKey(a) === priceKey(b) ? a.id.localeCompare(b.id) : priceKey(a) < priceKey(b) ? -1 : 1));
    free.sort((a, b) => a.id.localeCompare(b.id));
    return { paid, free };
  }

  async function loadCatalog() {
    catalog = null;
    if (!isOpenRouter()) {
      renderPool([]);
      return;
    }
    el("pool-list").textContent = "Loading the list from OpenRouter…";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(MODELS_URL, { signal: controller.signal });
      if (!response.ok) throw new Error("HTTP " + response.status);
      catalog = parseCatalog(await response.json());
    } catch (err) {
      catalog = null;
    } finally {
      clearTimeout(timer);
    }
    renderDatalist();
    renderPool(currentPool());
  }

  function renderDatalist() {
    const list = el("model-list");
    list.textContent = "";
    if (!catalog) return;
    catalog.paid.slice(0, 80).forEach((m) => {
      const option = document.createElement("option");
      option.value = m.id;
      option.textContent = Number.isFinite(m.input) ? "$" + m.input.toFixed(2) + " per million input tokens" : "";
      list.appendChild(option);
    });
  }

  function currentPool() {
    return Array.from(document.querySelectorAll(".aiwatch-pool:checked")).map((box) => box.value);
  }

  function renderPool(selected) {
    const box = el("pool-list");
    box.textContent = "";
    const known = catalog ? catalog.free : [];
    const entries = known.map((m) => ({ id: m.id, note: m.reasoning ? "reasoning model, may run out of answer space" : "" }));
    selected.forEach((id) => {
      if (!entries.some((e) => e.id === id)) entries.push({ id, note: catalog ? "not in the current list" : "" });
    });
    if (!entries.length) {
      const note = document.createElement("span");
      note.className = "text-secondary";
      note.textContent = isOpenRouter()
        ? "The list could not be loaded from OpenRouter. Press Refresh list to try again."
        : "The list of free models is only available for OpenRouter endpoints.";
      box.appendChild(note);
      return;
    }
    entries.forEach((entry, index) => {
      const row = document.createElement("div");
      row.className = "form-check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.className = "form-check-input aiwatch-pool";
      input.id = "pool_" + index;
      input.value = entry.id;
      input.checked = selected.includes(entry.id);
      const label = document.createElement("label");
      label.className = "form-check-label";
      label.htmlFor = input.id;
      label.textContent = entry.id;
      if (entry.note) {
        const small = document.createElement("small");
        small.className = "text-secondary ms-1";
        small.textContent = "(" + entry.note + ")";
        label.appendChild(small);
      }
      row.appendChild(input);
      row.appendChild(label);
      box.appendChild(row);
    });
  }

  function showMode() {
    const pool = el("mode_pool").checked;
    el("pool-box").classList.toggle("d-none", !pool);
    el("single-box").classList.toggle("d-none", pool);
  }

  // --- form <-> settings ---

  function fill(config) {
    original = config;
    TEXT_FIELDS.forEach((key) => { el(key).value = config[key] == null ? "" : String(config[key]); });
    SWITCHES.forEach((key) => { el(key).checked = config[key] !== false && config[key] !== "false"; });
    el("enabled").checked = config.enabled === true || config.enabled === "true";
    Object.keys(NUMBERS).forEach((key) => {
      const value = Number.parseInt(config[key], 10);
      el(key).value = Number.isInteger(value) ? value : NUMBERS[key];
    });
    el("rules").value = (Array.isArray(config.rules) ? config.rules : []).join("\n");
    const services = Array.isArray(config.services) ? config.services : [];
    document.querySelectorAll(".aiwatch-service").forEach((box) => {
      box.checked = services.includes(box.value);
    });
    const pool = config.free_pool === true || config.free_pool === "true";
    el("mode_pool").checked = pool;
    el("mode_single").checked = !pool;
    renderPool(Array.isArray(config.free_models) ? config.free_models : []);
    showMode();
  }

  function collect() {
    const config = JSON.parse(JSON.stringify(original || {}));
    TEXT_FIELDS.forEach((key) => { config[key] = el(key).value.trim(); });
    config.extra_instructions = config.extra_instructions.replace(/\s+/g, " ");
    SWITCHES.forEach((key) => { config[key] = el(key).checked; });
    Object.keys(NUMBERS).forEach((key) => {
      const value = Number.parseInt(el(key).value, 10);
      config[key] = Number.isInteger(value) && value >= 0 ? value : NUMBERS[key];
    });
    config.rules = el("rules").value.split("\n").map((line) => line.trim()).filter(Boolean);
    config.services = Array.from(document.querySelectorAll(".aiwatch-service"))
      .filter((box) => box.checked).map((box) => box.value);
    config.free_pool = el("mode_pool").checked;
    config.free_models = currentPool();
    return config;
  }

  function problems(config) {
    const list = [];
    if (!/^https?:\/\//.test(config.endpoint)) list.push("The endpoint must start with http:// or https://.");
    if (config.free_pool) {
      if (config.enabled && !config.free_models.length) list.push("Pick at least one free model, or choose One model.");
      if (config.free_models.length > MAX_POOL) list.push("Pick at most " + MAX_POOL + " free models.");
    } else {
      if (config.enabled && !config.model) list.push("Enter a model, or switch off Analyze motion events.");
      if (config.model && !MODEL_ID.test(config.model)) list.push("The model id contains characters a model id does not use.");
    }
    if (config.rules.length > MAX_RULES) list.push("At most " + MAX_RULES + " rules are supported.");
    if (Boolean(config.active_from) !== Boolean(config.active_to)) {
      list.push("Set both active times, or leave both empty.");
    }
    return list;
  }

  // --- status, usage and events ---

  function statusLine(ok, text, extra) {
    const li = document.createElement("li");
    const icon = document.createElement("i");
    icon.className = ok ? "bi bi-check-circle text-success" : "bi bi-exclamation-triangle text-warning";
    li.appendChild(icon);
    li.appendChild(document.createTextNode(" " + text));
    if (extra) {
      li.appendChild(document.createTextNode(" "));
      li.appendChild(extra);
    }
    return li;
  }

  function addRow(body, values, className) {
    const row = body.insertRow();
    values.forEach((value) => {
      const cell = row.insertCell();
      cell.textContent = value;
      if (className) cell.className = className;
    });
    return row;
  }

  function renderUsage(status) {
    const body = el("aiwatch-usage");
    body.textContent = "";
    const usage = status.usage || [];
    const pool = Array.isArray(original.free_models) ? original.free_models : [];
    const limit = Number.parseInt(original.free_daily_limit, 10) || NUMBERS.free_daily_limit;
    const freeCalls = usage.filter((u) => pool.includes(u.model)).reduce((sum, u) => sum + u.calls, 0);
    el("aiwatch-usage-summary").textContent = original.free_pool
      ? "Free models: " + freeCalls + " of " + limit + " calls used today. Counted since midnight, kept in memory; cleared when the camera restarts."
      : "Counted since midnight, kept in memory; cleared when the camera restarts.";
    if (!usage.length) {
      const row = addRow(body, ["No model calls yet today."], "text-secondary");
      row.cells[0].colSpan = 4;
      return;
    }
    usage.forEach((u) => {
      addRow(body, [u.model, String(u.calls), String(u.failures),
        u.wait > 0 ? "waiting " + u.wait + " s after an error" : "ready"]);
    });
  }

  function renderStatus(status, config) {
    lastStatus = status;
    const list = el("aiwatch-status");
    list.textContent = "";
    list.appendChild(statusLine(status.installed, status.installed
      ? "aiwatch is installed" : "The aiwatch program is missing from this camera"));
    list.appendChild(statusLine(status.hooked, status.hooked
      ? "Motion events are passed to aiwatch" : "The motion script does not call aiwatch; reinstall it from the SD card"));
    let fix = null;
    if (!status.motion_enabled) {
      fix = document.createElement("button");
      fix.type = "button";
      fix.className = "btn btn-sm btn-outline-warning";
      fix.textContent = "Turn on";
      fix.addEventListener("click", async () => {
        fix.disabled = true;
        try {
          await getJson(API + "?action=motion_on");
          await loadStatus();
        } catch (err) {
          alertUser("danger", "Could not turn motion detection on: " + err.message);
          fix.disabled = false;
        }
      });
    }
    list.appendChild(statusLine(status.motion_enabled, status.motion_enabled
      ? "Motion detection is on" : "Motion detection is off, so no events arrive", fix));
    const limit = Number.parseInt(config.daily_limit, 10) || NUMBERS.daily_limit;
    list.appendChild(statusLine(status.calls_today < limit,
      status.calls_today + " of " + limit + " events analyzed today"));

    renderUsage(status);

    const body = el("aiwatch-events");
    body.textContent = "";
    const events = (status.events || []).slice().reverse();
    if (!events.length) {
      const row = addRow(body, ["No events since the camera started."], "text-secondary");
      row.cells[0].colSpan = 5;
      return;
    }
    events.forEach((event) => {
      addRow(body, [
        String(event.time || "").replace("T", " ").slice(0, 19),
        event.status !== "ok" ? "failed" : event.notify ? "sent" : "quiet",
        event.description || (event.status !== "ok" ? "The model could not be reached" : ""),
        event.rule || "",
        event.model || "",
      ]);
    });
  }

  async function loadStatus() {
    try {
      renderStatus(await getJson(API + "?action=status"), original);
    } catch (err) {
      el("aiwatch-status").textContent = "Status is unavailable: " + err.message;
    }
  }

  async function load() {
    busy("Loading AI alert settings...");
    try {
      fill(await getJson(API));
      await loadStatus();
    } catch (err) {
      alertUser("danger", "Could not load the settings: " + err.message);
    } finally {
      idle();
    }
    loadCatalog();
  }

  async function save() {
    const config = collect();
    const list = problems(config);
    if (list.length) {
      alertUser("danger", list.join(" "));
      return false;
    }
    busy("Saving AI alert settings...");
    try {
      fill(await getJson(API, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(config, null, 2),
      }));
      if (catalog) renderPool(original.free_models || []);
      if (typeof window.showOverlayMessage === "function") {
        window.showOverlayMessage("AI alert settings saved.", "success");
      } else {
        alertUser("success", "AI alert settings saved.");
      }
      await loadStatus();
      return true;
    } catch (err) {
      alertUser("danger", "Could not save the settings: " + err.message);
      return false;
    } finally {
      idle();
    }
  }

  async function runTest(action, button) {
    const output = el("aiwatch-test-output");
    button.disabled = true;
    output.classList.remove("d-none");
    output.textContent = "Taking a snapshot and asking the model; this can take up to half a minute...";
    try {
      // Test what is on screen, not what was saved last time.
      if (JSON.stringify(collect()) !== JSON.stringify(original) && !(await save())) {
        output.textContent = "Fix the settings above first.";
        return;
      }
      const result = await getJson(API + "?action=" + action);
      output.textContent = (result.ok ? "" : "The test failed.\n") + (result.output || "");
      await loadStatus();
    } catch (err) {
      output.textContent = "The test could not run: " + err.message;
    } finally {
      button.disabled = false;
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  el("aiwatch-reload").addEventListener("click", load);
  el("aiwatch-test").addEventListener("click", (event) => runTest("test", event.currentTarget));
  el("aiwatch-test-notify").addEventListener("click", (event) => runTest("test_notify", event.currentTarget));
  el("api_key_toggle").addEventListener("click", (event) => {
    const input = el("api_key");
    const hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    event.currentTarget.textContent = hidden ? "Hide" : "Show";
  });
  el("mode_single").addEventListener("change", showMode);
  el("mode_pool").addEventListener("change", showMode);
  el("model_recommended").addEventListener("click", () => { el("model").value = RECOMMENDED_MODEL; });
  el("pool_refresh").addEventListener("click", loadCatalog);
  el("endpoint").addEventListener("change", loadCatalog);

  load();
})();
