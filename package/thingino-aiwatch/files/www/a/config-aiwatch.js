(function () {
  "use strict";

  const API = "/x/json-aiwatch.cgi";
  const TEXT_FIELDS = ["endpoint", "api_key", "model", "camera_name", "context", "language",
    "extra_instructions", "active_from", "active_to"];
  const SWITCHES = ["enabled", "filter_empty", "send_photo", "fallback_on_error"];
  const NUMBERS = { cooldown: 60, daily_limit: 200, snapshot_channel: 1, max_image_kb: 600, timeout: 30 };
  const MAX_RULES = 10;

  const el = (id) => document.getElementById(id);
  const form = el("aiwatchForm");
  let original = {};

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
    return config;
  }

  function problems(config) {
    const list = [];
    if (!/^https?:\/\//.test(config.endpoint)) list.push("The endpoint must start with http:// or https://.");
    if (config.enabled && !config.model) list.push("Enter a model, or switch off Analyze motion events.");
    if (config.rules.length > MAX_RULES) list.push("At most " + MAX_RULES + " rules are supported.");
    if (Boolean(config.active_from) !== Boolean(config.active_to)) {
      list.push("Set both active times, or leave both empty.");
    }
    return list;
  }

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

  function renderStatus(status, config) {
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
      status.calls_today + " of " + limit + " model calls used today"));

    const body = el("aiwatch-events");
    body.textContent = "";
    const events = (status.events || []).slice().reverse();
    if (!events.length) {
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 4;
      cell.className = "text-secondary";
      cell.textContent = "No events since the camera started.";
      return;
    }
    events.forEach((event) => {
      const row = body.insertRow();
      row.insertCell().textContent = String(event.time || "").replace("T", " ").slice(0, 19);
      row.insertCell().textContent = event.status !== "ok" ? "failed" : event.notify ? "sent" : "quiet";
      row.insertCell().textContent = event.description || (event.status !== "ok" ? "The model could not be reached" : "");
      row.insertCell().textContent = event.rule || "";
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

  load();
})();
