// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 the Goliath-CNC contributors.
//
// The minimal shell of the goliath-run interface (unit U01). It follows the
// session through the event stream (GET events) and shows the snapshot as the
// server reports it, the banners, and who controls the session. The screens
// of the project design, section 8.3, replace it in later units.
//
// Rules kept here for those units:
// * text from the session goes into the page through textContent only, never
//   parsed as HTML (red-team C-09: a job file must never script this page);
// * a view that is not updating says so (reconnecting, disconnected, no
//   events for STALE_MS); it never keeps showing old state as if it were live;
// * every state change is a POST from the tab that holds control; the server
//   checks everything again (Origin, cookie, lease).
"use strict";

(function () {
  const STALE_MS = 45000; // three missed heartbeats (the server sends one every 15 s)
  const LEASE_KEY = "goliath-run.lease";

  let lastEventAt = 0;
  let streamState = "connecting";
  let leaseState = null; // {epoch, held} from the server
  let mine = null; // {lease, epoch} when this tab holds control

  function el(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    el(id).textContent = text;
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

  function renderConnection() {
    const age = lastEventAt === 0 ? null : Math.round((Date.now() - lastEventAt) / 1000);
    let text;
    let stale = true;
    if (streamState === "live" && age !== null && age * 1000 <= STALE_MS) {
      text = "Live: updated " + age + " s ago.";
      stale = false;
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
  }

  function renderSnapshot(snap) {
    setText("firmware", snap.firmware === null ? "FIRMWARE UNKNOWN" :
      "Robot: " + snap.firmware + " (from " + snap.firmware_source + ")");
    setText("machine-state", "Machine state: " + snap.phase + (snap.may_be_moving ? " - MAY BE MOVING" : ""));
    document.body.classList.toggle("moving", snap.may_be_moving === true);
    const list = el("banners");
    while (list.firstChild) {
      list.removeChild(list.firstChild);
    }
    for (const banner of snap.banners) {
      const item = document.createElement("li");
      item.className = "banner severity-" + String(banner.severity).replace(/[^a-z]/g, "");
      item.textContent = "[" + String(banner.severity).toUpperCase() + ", " + banner.icon + "] " + banner.text;
      list.appendChild(item);
    }
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

  document.addEventListener("DOMContentLoaded", function () {
    loadLease();
    el("take-control").addEventListener("click", function () {
      lease("take");
    });
    el("release-control").addEventListener("click", function () {
      lease("release");
    });
    connect();
    lease("acquire");
    window.setInterval(renderConnection, 1000);
  });
})();
