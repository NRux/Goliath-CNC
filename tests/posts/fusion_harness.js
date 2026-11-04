// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright 2026 the Goliath-CNC contributors.
/*
  A stand-in for Fusion's post engine, for the tests of posts/fusion/goliath.cps.

      node fusion_harness.js POST.cps < request.json

  Fusion cannot run here, so test_fusion_post.py drives the post through this
  small model of the engine. It implements ONLY the subset of the documented
  post processor API that goliath.cps uses (Autodesk's post processor API
  reference, https://cam.autodesk.com/posts/reference/, and the Post Processor
  Training Guide). The post runs in a fresh JavaScript context that holds this
  subset and nothing else, so a post that reached for anything outside it
  fails here with a ReferenceError instead of passing unnoticed.

  The subset:

  * constants: MM, IN; PLANE_XY, PLANE_ZX, PLANE_YZ; CAPABILITY_MILLING;
    TYPE_MILLING, TYPE_TURNING, TYPE_JET; RADIUS_COMPENSATION_OFF, _LEFT,
    _RIGHT; FEED_PER_MINUTE, FEED_PER_REVOLUTION, FEED_INVERSE_TIME; the
    TOOL_* types and COMMAND_* commands listed in CONSTANTS below;
  * kernel settings the post assigns (description, extension, tolerance,
    minimumChordLength, maximumCircularSweep, allowHelicalMoves,
    allowedCircularPlanes, properties, ...) and the variables the engine
    sets: unit, currentSection, tool, cycleType, cycle, cycleExpanded,
    radiusCompensation, movement;
  * functions: spatial, toRad, setCodePage, writeln, error, warning,
    getProperty, hasGlobalParameter, getGlobalParameter,
    getNumberOfSections, getSection, getCircularPlane, getCircularSweep,
    isHelical, isSpiral, isFullCircle, expandCyclePoint;
  * Section: getId, getType, isMultiAxis, isOptional, workPlane,
    workOrigin, getGlobalWorkOrigin, getGlobalWorkPlane, getFeedMode,
    getTool, getParameter, getInitialPosition, strategy;
  * Tool: number, diameter, fluteLength, type, unit, spindleRPM,
    rampingSpindleRPM, isClockwise, getDescription.

  What the model does, as the training guide describes the engine: onOpen,
  the global parameters (onParameter), then per section the Manual NC
  commands that precede it ("manual_nc", before onSection: guide 6.1.2), its
  operation parameters, onSection, its records and onSectionEnd, then onClose. Arcs are
  linearized (onLinear calls within `tolerance`) when they break the post's
  circular settings (plane, helix, spiral, sweep, chord, radius). With
  "rapids_as_feed" set, rapids reach onLinear at that feed with movement
  MOVEMENT_RAPID, as the guide describes for high feed mapping and as Fusion
  for Personal Use is reported to do. expandCyclePoint is a plain stand-in
  (clearance, retract plane, feed to the bottom, out); it is not Fusion's
  expansion, and it only exists to show what the post does with the moves.

  The request (JSON on stdin) is one of:

    {"mode": "post", "job": {...}}    run the post on a job (see runJob)
    {"mode": "call", "function": NAME, "args": [...]}
                                      call one of the post's own functions
    {"mode": "map", "function": NAME, "args_list": [[...], ...]}
                                      call it once per argument list
    {"mode": "inspect", "names": [...]}
                                      read globals after the post loaded

  The reply (JSON on stdout): {"ok": true, "output": ..., "value": ...};
  {"ok": false, "error": MESSAGE, "output": ...} when the post called
  error(); {"ok": false, "crash": TEXT} for anything else.
*/
"use strict";

const fs = require("fs");
const vm = require("vm");

const CONSTANTS = {
  IN: 0,
  MM: 1,
  PLANE_XY: 0,
  PLANE_ZX: 1,
  PLANE_YZ: 2,
  CAPABILITY_MILLING: 1,
  TYPE_MILLING: 0,
  TYPE_TURNING: 1,
  TYPE_JET: 3,
  RADIUS_COMPENSATION_OFF: 0,
  RADIUS_COMPENSATION_LEFT: 1,
  RADIUS_COMPENSATION_RIGHT: 2,
  FEED_PER_MINUTE: 0,
  FEED_PER_REVOLUTION: 1,
  FEED_INVERSE_TIME: 2,
  MOVEMENT_RAPID: 5,
  MOVEMENT_CUTTING: 1,
  TOOL_UNSPECIFIED: 0,
  TOOL_DRILL: 1,
  TOOL_MILLING_END_FLAT: 5,
  TOOL_MILLING_END_BALL: 6,
  TOOL_MILLING_END_BULLNOSE: 7,
  TOOL_MILLING_CHAMFER: 8,
  COMMAND_STOP: 1,
  COMMAND_OPTIONAL_STOP: 2,
  COMMAND_END: 3,
  COMMAND_SPINDLE_CLOCKWISE: 4,
  COMMAND_SPINDLE_COUNTERCLOCKWISE: 5,
  COMMAND_STOP_SPINDLE: 6,
  COMMAND_LOAD_TOOL: 8,
  COMMAND_COOLANT_ON: 9,
  COMMAND_COOLANT_OFF: 10,
  COMMAND_START_SPINDLE: 11,
  COMMAND_TOOL_MEASURE: 12
};

class PostError extends Error {}

function vec(a) {
  return {x: a[0], y: a[1], z: a[2]};
}

function plane(p) {
  p = p || {};
  return {
    right: vec(p.right || [1, 0, 0]),
    up: vec(p.up || [0, 1, 0]),
    forward: vec(p.forward || [0, 0, 1])
  };
}

function constant(name) {
  if (!(name in CONSTANTS)) {
    throw new Error("harness: unknown constant " + name);
  }
  return CONSTANTS[name];
}

function makeTool(t) {
  return {
    number: t.number,
    diameter: t.diameter,
    fluteLength: t.fluteLength,
    type: constant(t.type || "TOOL_MILLING_END_FLAT"),
    unit: constant(t.unit || "MM"),
    spindleRPM: t.spindleRPM,
    rampingSpindleRPM: t.rampingSpindleRPM === undefined ? t.spindleRPM : t.rampingSpindleRPM,
    isClockwise: function () { return t.clockwise === undefined ? true : t.clockwise; },
    getDescription: function () { return t.description === undefined ? "" : t.description; }
  };
}

function makeSection(s, index) {
  const tool = makeTool(s.tool);
  const parameters = s.parameters || {};
  return {
    strategy: s.strategy,
    workPlane: plane(s.work_plane),
    workOrigin: vec(s.work_origin || [0, 0, 0]),
    getId: function () { return index; },
    getType: function () { return constant(s.type || "TYPE_MILLING"); },
    isMultiAxis: function () { return Boolean(s.multi_axis); },
    isOptional: function () { return Boolean(s.optional); },
    getGlobalWorkOrigin: function () { return vec(s.global_work_origin || [0, 0, 0]); },
    getGlobalWorkPlane: function () { return plane(s.global_work_plane); },
    getFeedMode: function () { return constant(s.feed_mode || "FEED_PER_MINUTE"); },
    getTool: function () { return tool; },
    getParameter: function (name, value) { return name in parameters ? parameters[name] : value; },
    getInitialPosition: function () { return vec(s.initial); },
    _job: s
  };
}

// --------------------------------------------------------------------------
// Circular motion as the engine treats it (training guide 5.26.1)
// --------------------------------------------------------------------------

const AXES = {0: ["x", "y", "z"], 1: ["z", "x", "y"], 2: ["y", "z", "x"]};

function circleOf(start, centre, end, clockwise, planeId) {
  const [a, b, n] = AXES[planeId];
  const a0 = Math.atan2(start[b] - centre[b], start[a] - centre[a]);
  const a1 = Math.atan2(end[b] - centre[b], end[a] - centre[a]);
  const startRadius = Math.hypot(start[a] - centre[a], start[b] - centre[b]);
  const endRadius = Math.hypot(end[a] - centre[a], end[b] - centre[b]);
  const full = Math.hypot(end[a] - start[a], end[b] - start[b]) < 1e-9;
  let ccw = a1 - a0;
  while (ccw < 0) ccw += 2 * Math.PI;
  while (ccw >= 2 * Math.PI) ccw -= 2 * Math.PI;
  let sweep = clockwise ? (2 * Math.PI - ccw) % (2 * Math.PI) : ccw;
  if (full) sweep = 2 * Math.PI;
  return {
    a, b, n, a0, startRadius, endRadius, sweep, full,
    chord: Math.hypot(end.x - start.x, end.y - start.y, end.z - start.z),
    helical: Math.abs(end[n] - start[n]) > 1e-9,
    spiral: Math.abs(startRadius - endRadius) > 1e-9
  };
}

function needsLinearizing(ctx, c, planeId) {
  const allowed = ctx.allowedCircularPlanes;
  if (allowed !== undefined && !(allowed & (1 << planeId))) return "plane";
  if (c.helical && !ctx.allowHelicalMoves) return "helix";
  if (c.spiral && !ctx.allowSpiralMoves) return "spiral";
  if (c.sweep > ctx.maximumCircularSweep + 1e-12) return "sweep";
  if (c.sweep < ctx.minimumCircularSweep) return "sweep";
  if (!c.full && c.chord < ctx.minimumChordLength) return "chord";
  if (c.endRadius < ctx.minimumCircularRadius || c.endRadius > ctx.maximumCircularRadius) return "radius";
  return null;
}

function linearize(start, centre, end, c, clockwise, tolerance) {
  // Points on the arc whose chords stay within `tolerance` of it.
  const r = Math.max(c.startRadius, c.endRadius);
  const step = r > tolerance ? 2 * Math.acos(1 - tolerance / r) : Math.PI / 2;
  const count = Math.max(1, Math.ceil(c.sweep / step));
  const points = [];
  for (let k = 1; k <= count; ++k) {
    if (k == count) {
      points.push(Object.assign({}, end));
      break;
    }
    const u = k / count;
    const angle = c.a0 + (clockwise ? -1 : 1) * c.sweep * u;
    const radius = c.startRadius + (c.endRadius - c.startRadius) * u;
    const p = {};
    p[c.a] = centre[c.a] + radius * Math.cos(angle);
    p[c.b] = centre[c.b] + radius * Math.sin(angle);
    p[c.n] = start[c.n] + (end[c.n] - start[c.n]) * u;
    points.push(p);
  }
  return points;
}

// --------------------------------------------------------------------------
// The engine model
// --------------------------------------------------------------------------

function makeContext(job, out, warnings) {
  const ctx = Object.assign({}, CONSTANTS);
  const globals = job.globals || [];
  const sections = (job.sections || []).map(makeSection);
  const state = {circular: null};

  ctx.unit = constant(job.unit || "MM");
  ctx.spatial = function (value, u) { return u == CONSTANTS.MM ? value : value * 25.4; };
  ctx.toRad = function (degrees) { return degrees * Math.PI / 180; };
  ctx.setCodePage = function (name) { ctx._codePage = name; };
  ctx.writeln = function (text) { out.push(String(text) + "\n"); };
  ctx.error = function (message) { throw new PostError(String(message)); };
  ctx.warning = function (message) { warnings.push(String(message)); };
  ctx.getProperty = function (name, value) {
    const chosen = job.properties || {};
    if (name in chosen) return chosen[name];
    const defined = ctx.properties && ctx.properties[name];
    return defined ? defined.value : value;
  };
  ctx.hasGlobalParameter = function (name) { return globals.some((g) => g[0] == name); };
  ctx.getGlobalParameter = function (name, value) {
    const found = globals.find((g) => g[0] == name);
    return found ? found[1] : value;
  };
  ctx.getNumberOfSections = function () { return sections.length; };
  ctx.getSection = function (index) {
    if (!(index >= 0 && index < sections.length)) throw new Error("harness: no section " + index);
    return sections[index];
  };
  ctx.getCircularPlane = function () { return state.circular.plane; };
  ctx.getCircularSweep = function () { return state.circular.sweep; };
  ctx.isHelical = function () { return state.circular.helical; };
  ctx.isSpiral = function () { return state.circular.spiral; };
  ctx.isFullCircle = function () { return state.circular.full; };
  ctx.expandCyclePoint = function (x, y, z) { state.expand(x, y, z); };
  return {ctx, sections, globals, state};
}

function runJob(source, postPath, job, out) {
  const warnings = [];
  const {ctx, sections, globals, state} = makeContext(job, out, warnings);
  vm.createContext(ctx);
  vm.runInContext(source, ctx, {filename: postPath});

  let position = null;
  const call = function (name, ...args) {
    if (typeof ctx[name] != "function") {
      throw new Error("harness: the post does not define " + name + ", which the engine calls here");
    }
    return ctx[name](...args);
  };
  const rapid = function (x, y, z) {
    if (job.rapids_as_feed !== undefined) {
      ctx.movement = CONSTANTS.MOVEMENT_RAPID;
      call("onLinear", x, y, z, job.rapids_as_feed);
    } else {
      ctx.movement = CONSTANTS.MOVEMENT_RAPID;
      call("onRapid", x, y, z);
    }
    position = {x, y, z};
  };
  const linear = function (x, y, z, feed) {
    ctx.movement = CONSTANTS.MOVEMENT_CUTTING;
    call("onLinear", x, y, z, feed);
    position = {x, y, z};
  };
  const circular = function (clockwise, cx, cy, cz, x, y, z, feed, options) {
    options = options || {};
    const planeId = constant("PLANE_" + (options.plane || "XY"));
    const centre = {x: cx, y: cy, z: cz};
    const end = {x, y, z};
    const c = circleOf(position, centre, end, clockwise, planeId);
    if (!options.bypass_engine && needsLinearizing(ctx, c, planeId)) {
      for (const p of linearize(position, centre, end, c, clockwise, ctx.tolerance)) {
        linear(p.x, p.y, p.z, feed);
      }
      return;
    }
    state.circular = {
      plane: planeId,
      sweep: options.sweep_deg !== undefined ? options.sweep_deg * Math.PI / 180 : c.sweep,
      helical: options.helical !== undefined ? options.helical : c.helical,
      spiral: options.spiral !== undefined ? options.spiral : c.spiral,
      full: options.full !== undefined ? options.full : c.full
    };
    ctx.movement = CONSTANTS.MOVEMENT_CUTTING;
    call("onCircular", clockwise, cx, cy, cz, x, y, z, feed);
    position = end;
    state.circular = null;
  };

  state.expand = function (x, y, z) {
    const cycle = ctx.cycle;
    const type = ctx.cycleType;
    rapid(x, y, cycle.clearance);
    rapid(x, y, cycle.retract);
    if (type == "drilling" || type == "counter-boring") {
      linear(x, y, z, cycle.feedrate);
      if (type == "counter-boring") call("onDwell", cycle.dwell);
      rapid(x, y, cycle.clearance);
    } else if (type == "deep-drilling" || type == "chip-breaking") {
      let depth = cycle.retract;
      while (depth > z) {
        const next = Math.max(depth - cycle.incrementalDepth, z);
        if (depth != cycle.retract) rapid(x, y, depth + 0.5);
        linear(x, y, next, cycle.feedrate);
        depth = next;
        if (depth > z) {
          rapid(x, y, type == "deep-drilling" ? cycle.retract : depth + cycle.chipBreakDistance);
        }
      }
      rapid(x, y, cycle.clearance);
    } else if (type == "reaming") {
      linear(x, y, z, cycle.feedrate);
      linear(x, y, cycle.retract, cycle.retractFeedrate);
      rapid(x, y, cycle.clearance);
    } else {
      throw new Error("harness: the stand-in expandCyclePoint does not expand '" + type + "'");
    }
    ctx.cycleExpanded = true;
  };

  const play = function (record) {
    const [kind, ...args] = record;
    if (kind == "rapid") {
      rapid(...args);
    } else if (kind == "linear") {
      linear(...args);
    } else if (kind == "circular") {
      circular(...args);
    } else if (kind == "cycle") {
      const [type, parameters, points] = args;
      ctx.cycleType = type;
      ctx.cycle = parameters;
      ctx.cycleExpanded = false;
      call("onCycle");
      for (const p of points) {
        call("onCyclePoint", p[0], p[1], p[2]);
      }
      call("onCycleEnd");
    } else if (kind == "radius_compensation") {
      ctx.radiusCompensation = constant(args[0]);
      call("onRadiusCompensation");
    } else if (kind == "dwell") {
      call("onDwell", args[0]);
    } else if (kind == "comment") {
      call("onComment", args[0]);
    } else if (kind == "spindle_speed") {
      call("onSpindleSpeed", args[0]);
    } else if (kind == "command") {
      call("onCommand", constant(args[0]));
    } else if (kind == "parameter") {
      call("onParameter", args[0], args[1]);
    } else if (kind == "pass_through") {
      call("onPassThrough", args[0]);
    } else if (kind == "rapid5d") {
      call("onRapid5D", ...args);
    } else if (kind == "linear5d") {
      call("onLinear5D", ...args);
    } else {
      throw new Error("harness: unknown record " + kind);
    }
  };

  call("onOpen");
  for (const [name, value] of globals) {
    call("onParameter", name, value);
  }
  for (const section of sections) {
    const s = section._job;
    // Manual NC commands are processed where they stand in the CAM tree, before
    // the operation they precede, so before its onSection (training guide 6.1.2).
    for (const record of s.manual_nc || []) {
      play(record);
    }
    for (const name of Object.keys(s.parameters || {})) {
      call("onParameter", name, s.parameters[name]);
    }
    ctx.currentSection = section;
    ctx.tool = section.getTool();
    ctx.radiusCompensation = CONSTANTS.RADIUS_COMPENSATION_OFF;
    call("onSection");
    position = vec(s.initial);
    for (const record of s.records || []) {
      play(record);
    }
    call("onSectionEnd");
  }
  call("onClose");
  return {output: out.join(""), warnings};
}

function main() {
  const postPath = process.argv[2];
  const source = fs.readFileSync(postPath, "utf8");
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  const out = [];
  let reply;
  try {
    if (request.mode == "post") {
      const result = runJob(source, postPath, request.job, out);
      reply = {ok: true, output: result.output, warnings: result.warnings};
    } else {
      const {ctx} = makeContext(request.job || {}, out, []);
      vm.createContext(ctx);
      vm.runInContext(source, ctx, {filename: postPath});
      if (request.mode == "call") {
        reply = {ok: true, value: ctx[request.function](...(request.args || []))};
      } else if (request.mode == "map") {
        reply = {ok: true, value: request.args_list.map((args) => ctx[request.function](...args))};
      } else if (request.mode == "inspect") {
        const value = {};
        for (const name of request.names) value[name] = ctx[name];
        reply = {ok: true, value};
      } else {
        throw new Error("harness: unknown mode " + request.mode);
      }
    }
  } catch (exc) {
    if (exc instanceof PostError) {
      reply = {ok: false, error: exc.message, output: out.join("")};
    } else {
      reply = {ok: false, crash: String(exc && exc.stack ? exc.stack : exc)};
    }
  }
  process.stdout.write(JSON.stringify(reply));
}

main();
