// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 the Goliath-CNC contributors.
//
// The shell of the goliath-run interface (units U01 and U02). It follows the
// session through the event stream (GET events): after the snapshots, a
// "view" event carries what the presenters (goliath_run.ui.presenters) say
// every screen shows: the top bar, the banners and the disclosure chip. This
// file draws that view and nothing of its own reading of the snapshot. The
// screens below the navigation are drawn by screens.js.
//
// Rules kept here for the units that add screens:
// * text from the session goes into the page through textContent only, never
//   parsed as HTML (red-team C-09: a job file must never script this page);
// * a view that is not updating says so (reconnecting, disconnected, no
//   events for STALE_MS), and the top bar then shows what the presenters say
//   for that case (if_stale): no software stop, and the robot may be moving.
//   The banners and the screen keep the last view under a NOT UPDATING note.
//   It never keeps showing old state as if it were live;
// * colour is never the only signal: every banner has its icon and its
//   severity written out;
// * every state change is a POST from the tab that holds control; the server
//   checks everything again (Origin, cookie, lease).
"use strict";

(function () {
  const STALE_MS = 45000; // three missed heartbeats (the server sends one every 15 s)
  const LEASE_KEY = "goliath-run.lease";
  const FALLBACK_STOP = "NO SOFTWARE STOP: PHYSICAL STOP ONLY";
  const FALLBACK_STATE = "UNKNOWN: this view is not updating, so the robot MAY BE MOVING";

  let lastEventAt = 0;
  let streamState = "connecting";
  let leaseState = null; // {epoch, held} from the server
  let mine = null; // {lease, epoch} when this tab holds control
  let view = null; // the presenters' view of the newest snapshot
  let live = false;

  function el(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    el(id).textContent = text;
  }

  function severityClass(severity) {
    return "severity-" + String(severity).replace(/[^a-z]/g, "");
  }

  function setSeverity(node, severity) {
    for (const name of Array.from(node.classList)) {
      if (name.indexOf("severity-") === 0) {
        node.classList.remove(name);
      }
    }
    if (severity) {
      node.classList.add(severityClass(severity));
    }
  }

  // An icon from the page's sprite: a copy of the template with its reference set (no markup is parsed).
  function icon(name) {
    const svg = el("icon-template").content.firstElementChild.cloneNode(true);
    svg.querySelector("use").setAttribute("href", "#icon-" + String(name).replace(/[^a-z]/g, ""));
    return svg;
  }

  function loadLease() {
    try {
      const raw = window.sessionStorage.getItem(LEASE_KEY);
      mine = raw === null ? null : JSON.parse(raw);
    } catch (err) {
      mine = null;
      console.error("goliath-run: could not read this tab's saved control lease", err);
    }
  }

  function saveLease() {
    try {
      if (mine === null) {
        window.sessionStorage.removeItem(LEASE_KEY);
      } else {
        window.sessionStorage.setItem(LEASE_KEY, JSON.stringify(mine));
      }
    } catch (err) {
      console.error("goliath-run: could not save this tab's control lease (a reload will make it read-only)", err);
    }
  }

  // Whether the stream is live and has delivered an event within STALE_MS.
  function fresh() {
    return streamState === "live" && lastEventAt !== 0 && Date.now() - lastEventAt <= STALE_MS;
  }

  function renderConnection() {
    const age = lastEventAt === 0 ? null : Math.round((Date.now() - lastEventAt) / 1000);
    let text;
    const stale = !fresh();
    if (!stale) {
      text = "Live: updated " + age + " s ago.";
    } else if (streamState === "live" || streamState === "connecting") {
      text = age === null ? "Connecting to goliath-run ..." :
        "NO UPDATES for " + age + " s: this view may be out of date.";
    } else if (streamState === "reconnecting") {
      text = "RECONNECTING: this view may be out of date.";
    } else {
      text = streamState; // a closed stream's own explanation
    }
    setText("connection", text);
    document.body.classList.toggle("stale", stale);
    // The banners and the screen keep the last view, so they say so while it is not current.
    el("stale-banners").hidden = !stale;
    el("stale-screen").hidden = !stale;
    const nowLive = !stale && view !== null;
    if (nowLive !== live) {
      live = nowLive;
      renderTopBar();
    }
  }

  // The top bar: the presenters' texts while the view is live, their if_stale texts when it is not.
  function renderTopBar() {
    const firmware = el("firmware");
    const stop = el("stop-chip");
    const state = el("machine-state");
    if (view === null) {
      firmware.textContent = "Robot: waiting for goliath-run";
      stop.textContent = FALLBACK_STOP;
      setSeverity(stop, "red");
      state.textContent = lastEventAt === 0 && streamState === "connecting" ?
        "Machine state: waiting for goliath-run" : "Machine state: " + FALLBACK_STATE;
      setSeverity(state, lastEventAt === 0 && streamState === "connecting" ? null : "red");
      return;
    }
    const bar = view.top_bar;
    firmware.textContent = bar.firmware.text + (live ? "" : " (last known)");
    setSeverity(firmware, bar.firmware.severity);
    if (live) {
      stop.textContent = bar.stop.text;
      setSeverity(stop, bar.stop.severity);
      state.textContent = "Machine state: " + bar.state.text;
      setSeverity(state, bar.state.severity);
    } else {
      stop.textContent = bar.stop.if_stale;
      setSeverity(stop, "red");
      state.textContent = "Machine state: " + bar.state.if_stale;
      setSeverity(state, "red");
    }
    document.body.classList.toggle("moving", live ? bar.state.may_be_moving === true : true);
    document.body.classList.toggle("practice", bar.practice === true);
  }

  function renderBanners() {
    const list = el("banners");
    while (list.firstChild) {
      list.removeChild(list.firstChild);
    }
    for (const banner of view.banners) {
      const item = document.createElement("li");
      item.className = "banner " + severityClass(banner.severity);
      item.appendChild(icon(banner.icon));
      const label = document.createElement("span");
      label.className = "banner-level";
      label.textContent = String(banner.severity_label).toUpperCase() + ":";
      label.title = banner.icon_label;
      item.appendChild(label);
      const text = document.createElement("span");
      text.className = "banner-text";
      text.textContent = banner.text;
      item.appendChild(text);
      list.appendChild(item);
    }
  }

  function renderChip() {
    const chip = el("chip");
    chip.textContent = view.chip.label;
    setSeverity(chip, view.chip.severity);
    const list = el("chip-list");
    while (list.firstChild) {
      list.removeChild(list.firstChild);
    }
    if (view.chip.items.length === 0) {
      const item = document.createElement("li");
      item.textContent = "Nothing is in effect.";
      list.appendChild(item);
    }
    for (const entry of view.chip.items) {
      const item = document.createElement("li");
      item.className = "chip-item " + severityClass(entry.severity);
      const head = document.createElement("strong");
      head.textContent = "[" + String(entry.severity).toUpperCase() + ", " + entry.kind +
        (entry.sticky ? ", stays for this session" : "") + "] " + entry.id;
      item.appendChild(head);
      const text = document.createElement("p");
      text.textContent = entry.text;
      item.appendChild(text);
      const source = document.createElement("p");
      source.className = "ref";
      source.textContent = "Source: " + entry.source;
      item.appendChild(source);
      list.appendChild(item);
    }
  }

  function onView(next) {
    if (view !== null && next.seq < view.seq) {
      return; // never go back to an older state
    }
    view = next;
    document.title = next.title;
    live = fresh();
    renderTopBar();
    renderBanners();
    renderChip();
    document.dispatchEvent(new CustomEvent("goliath:view", {detail: {seq: next.seq}}));
  }

  function renderSnapshot(snap) {
    setText("snapshot", JSON.stringify(snap, null, 2));
  }

  function renderControl() {
    let text;
    if (leaseState === null) {
      text = "Control: asking goliath-run ...";
    } else if (mine !== null && leaseState.held && leaseState.epoch === mine.epoch) {
      text = "Control: this tab controls the session.";
    } else if (leaseState.held) {
      text = "Control: another tab controls the session; this tab is read-only.";
    } else {
      text = "Control: no tab controls the session; this tab is read-only.";
    }
    setText("control-state", text);
  }

  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = {error: "goliath-run answered " + response.status + " without JSON"};
    }
    return {status: response.status, data: data};
  }

  async function lease(action) {
    try {
      const result = await post("api/lease", {action: action, lease: mine === null ? null : mine.lease});
      if (result.status === 200 && typeof result.data.lease === "string") {
        mine = {lease: result.data.lease, epoch: result.data.epoch};
        const granted = {epoch: result.data.epoch, held: true};
        if (newer(granted)) {
          leaseState = granted;
        }
      } else if (action === "release" || result.status === 409) {
        mine = null;
      }
      if (result.status !== 200 && result.status !== 409) {
        setText("control-state", "Control: goliath-run refused (" + result.status + "): " + result.data.error);
        saveLease();
        return;
      }
      if (result.data.state && newer(result.data.state)) {
        leaseState = result.data.state;
      }
    } catch (err) {
      setText("control-state", "Control: the request failed (" + err + "); this tab is read-only.");
      mine = null;
      saveLease();
      return;
    }
    saveLease();
    renderControl();
  }

  // The lease epoch only grows (per server). The event stream and the lease
  // replies travel on different connections, so news can arrive out of order:
  // older news is ignored, never allowed to overwrite newer.
  function newer(state) {
    return leaseState === null || state.epoch >= leaseState.epoch;
  }

  function onLease(state) {
    if (!newer(state)) {
      return;
    }
    leaseState = state;
    if (mine !== null && state.epoch > mine.epoch) {
      mine = null; // control changed hands (or was released): this tab is read-only now
      saveLease();
    }
    renderControl();
  }

  function seen() {
    lastEventAt = Date.now();
    renderConnection();
  }

  function connect() {
    const stream = new EventSource("events");
    stream.addEventListener("open", function () {
      streamState = "live";
      renderConnection();
    });
    stream.addEventListener("snapshot", function (event) {
      seen();
      renderSnapshot(JSON.parse(event.data));
    });
    stream.addEventListener("view", function (event) {
      seen();
      onView(JSON.parse(event.data));
    });
    stream.addEventListener("lease", function (event) {
      seen();
      onLease(JSON.parse(event.data));
    });
    stream.addEventListener("heartbeat", seen);
    stream.addEventListener("end", function (event) {
      stream.close();
      streamState = "The session ended (" + JSON.parse(event.data).reason + "). Reload to follow a new session.";
      renderConnection();
    });
    stream.addEventListener("server-error", function (event) {
      stream.close();
      streamState = "STOPPED UPDATING: " + JSON.parse(event.data).error + " Reload to start again.";
      renderConnection();
    });
    stream.addEventListener("error", function () {
      if (stream.readyState === EventSource.CLOSED) {
        if (streamState === "live" || streamState === "connecting" || streamState === "reconnecting") {
          streamState = "DISCONNECTED: this view is not updating. Is goliath-run still running? " +
            "Reload, or sign in again with a new link from goliath-run.";
        }
      } else {
        streamState = "reconnecting";
      }
      renderConnection();
    });
  }

  function toggleChip() {
    const panel = el("chip-panel");
    const open = panel.hidden;
    panel.hidden = !open;
    el("chip").setAttribute("aria-expanded", open ? "true" : "false");
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadLease();
    el("take-control").addEventListener("click", function () {
      lease("take");
    });
    el("release-control").addEventListener("click", function () {
      lease("release");
    });
    el("chip").addEventListener("click", toggleChip);
    renderTopBar();
    connect();
    lease("acquire");
    window.setInterval(renderConnection, 1000);
  });
})();
