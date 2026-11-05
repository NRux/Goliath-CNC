// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 the Goliath-CNC contributors.
//
// The screens below the navigation (unit U02): Machine, Ladder, Records and
// Settings. Each is built by the presenters (goliath_run.ui.presenters) and
// served at api/screen/<name>; this file only draws it. A screen is a list of
// sections: "notice", "fields" (a label and a value with its provenance tag),
// "table", "list" and "group" (of sections). The active screen is fetched
// again whenever the session's view changes, at most one request at a time.
//
// Text goes into the page through textContent only (red-team C-09). A screen
// that cannot be loaded says so in the screen area: nothing old is left on
// show as if it were current.
"use strict";

(function () {
  const SCREENS = ["machine", "ladder", "records", "settings"];
  const LABELS = {info: "information", amber: "warning", red: "danger", purple: "practice", blue: "air job"};

  let current = null;
  let inFlight = false;
  let again = false;

  function el(id) {
    return document.getElementById(id);
  }

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) {
      n.className = className;
    }
    if (text !== undefined && text !== null) {
      n.textContent = String(text);
    }
    return n;
  }

  function severityClass(severity) {
    return "severity-" + String(severity || "info").replace(/[^a-z]/g, "");
  }

  // The severity in words, so colour is never the only signal (design 8.7).
  function level(severity) {
    return node("span", "level " + severityClass(severity), LABELS[severity] || String(severity));
  }

  function heading(depth, text) {
    return node("h" + Math.min(6, depth), null, text);
  }

  function renderNotice(section, depth) {
    const box = node("div", "notice " + severityClass(section.severity));
    if (section.title) {
      box.appendChild(heading(depth, section.title));
    }
    const p = node("p");
    if (section.severity && section.severity !== "info") {
      p.appendChild(level(section.severity));
      p.appendChild(document.createTextNode(" "));
    }
    p.appendChild(document.createTextNode(String(section.text)));
    box.appendChild(p);
    return box;
  }

  function renderFields(section, depth) {
    const box = node("div", "fields");
    box.appendChild(heading(depth, section.title));
    const list = node("dl");
    for (const row of section.rows) {
      list.appendChild(node("dt", null, row.label));
      const dd = node("dd", row.value.known ? "known" : "unknown");
      dd.appendChild(node("span", "value", row.value.text));
      dd.appendChild(document.createTextNode(" "));
      dd.appendChild(node("span", "tag", row.value.tag));
      dd.appendChild(node("span", "ref", row.value.ref));
      list.appendChild(dd);
    }
    box.appendChild(list);
    return box;
  }

  function renderTable(section, depth) {
    const box = node("div", "table");
    box.appendChild(heading(depth, section.title));
    if (section.note) {
      box.appendChild(node("p", "note", section.note));
    }
    const wrap = node("div", "table-wrap");
    const table = node("table");
    const head = node("thead");
    const headRow = node("tr");
    headRow.appendChild(node("th", null, "Level"));
    for (const column of section.columns) {
      headRow.appendChild(node("th", null, column));
    }
    head.appendChild(headRow);
    table.appendChild(head);
    const body = node("tbody");
    for (const row of section.rows) {
      const tr = node("tr", severityClass(row.severity));
      const first = node("td");
      first.appendChild(level(row.severity));
      tr.appendChild(first);
      for (const cell of row.cells) {
        tr.appendChild(node("td", null, cell));
      }
      body.appendChild(tr);
    }
    table.appendChild(body);
    wrap.appendChild(table);
    box.appendChild(wrap);
    return box;
  }

  function renderList(section, depth) {
    const box = node("div", "list " + severityClass(section.severity));
    box.appendChild(heading(depth, section.title));
    if (section.severity && section.severity !== "info") {
      box.appendChild(level(section.severity));
    }
    const list = node("ul");
    for (const item of section.items) {
      list.appendChild(node("li", null, item));
    }
    box.appendChild(list);
    return box;
  }

  function renderSection(section, depth) {
    switch (section.kind) {
      case "notice":
        return renderNotice(section, depth);
      case "fields":
        return renderFields(section, depth);
      case "table":
        return renderTable(section, depth);
      case "list":
        return renderList(section, depth);
      case "group": {
        const box = node("section", "group");
        box.appendChild(heading(depth, section.title));
        for (const part of section.sections) {
          box.appendChild(renderSection(part, depth + 1));
        }
        return box;
      }
      default:
        // A kind this page does not know is shown as a problem, never skipped.
        return renderNotice({severity: "red", text: "This page cannot draw a section of kind " +
          JSON.stringify(section.kind) + " (" + JSON.stringify(section.id) + "). Update the interface."}, depth);
    }
  }

  function clear(area) {
    while (area.firstChild) {
      area.removeChild(area.firstChild);
    }
  }

  function renderScreen(view) {
    const area = el("screen");
    clear(area);
    area.appendChild(node("h2", null, view.title));
    const when = new Date().toLocaleTimeString();
    area.appendChild(node("p", "built", (typeof view.seq === "number" ?
      "Built from session update " + view.seq + "; " : "") + "loaded at " + when +
      ", and again whenever the session changes."));
    for (const section of view.sections) {
      area.appendChild(renderSection(section, 3));
    }
  }

  function renderFailure(name, text) {
    const area = el("screen");
    clear(area);
    area.appendChild(node("h2", null, name.charAt(0).toUpperCase() + name.slice(1)));
    area.appendChild(renderNotice({severity: "red", text: "The " + name + " screen could not be loaded: " + text +
      " What it would show is not on this page; this is not the robot's state."}, 3));
  }

  async function load() {
    if (inFlight) {
      again = true;
      return;
    }
    inFlight = true;
    const name = current;
    try {
      const response = await fetch("api/screen/" + name, {credentials: "same-origin", cache: "no-store"});
      let data = null;
      try {
        data = await response.json();
      } catch (err) {
        data = {error: "goliath-run answered " + response.status + " without JSON"};
      }
      if (name === current) {
        if (response.status === 200 && data && Array.isArray(data.sections)) {
          renderScreen(data);
        } else {
          renderFailure(name, "goliath-run answered " + response.status + ": " + (data && data.error));
        }
      }
    } catch (err) {
      if (name === current) {
        renderFailure(name, "the request failed (" + err + ").");
      }
    } finally {
      inFlight = false;
      if (again) {
        again = false;
        load();
      }
    }
  }

  function show() {
    const wanted = window.location.hash.replace(/^#/, "");
    const name = SCREENS.indexOf(wanted) >= 0 ? wanted : "machine";
    current = name;
    for (const screen of SCREENS) {
      const link = el("nav-" + screen);
      if (screen === name) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    }
    load();
  }

  document.addEventListener("DOMContentLoaded", function () {
    window.addEventListener("hashchange", show);
    document.addEventListener("goliath:view", function () {
      if (current !== null) {
        load();
      }
    });
    show();
  });
})();
