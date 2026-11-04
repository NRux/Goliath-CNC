// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 the Goliath-CNC contributors.
/**
  Goliath GI post processor for Autodesk Fusion (goliath.cps).

  It writes GI, the Goliath-intermediate G-code of docs/prep/gi-format.md in
  the Goliath-CNC repository: millimetre, absolute, XY G-code plus ;GOLIATH
  annotations for the stock, the tower-2 axis, the one tool and the operation
  boundaries. goliath-prep reads GI, checks the job and writes what the robot
  runs; this post holds no safety logic.

  Written from scratch against Autodesk's public post processor
  documentation (the entry functions and kernel settings of the post
  processor API reference and the Post Processor Training Guide). It is not
  derived from any Autodesk library post.

  Prefer a visible failure over a guess: anything GI cannot carry, or that
  would be unsafe to guess, stops the post with error() and a message that
  says what to change. The program is collected in memory and written only in
  onClose, so a refused job writes no program.
*/

description = "Goliath CNC: GI for goliath-prep";
vendor = "Goliath CNC (open software)";
legal = "Copyright 2026 the Goliath-CNC contributors. Licensed under Apache-2.0.";
certificationLevel = 2;
minimumRevision = 45600;

longDescription = "Writes GI (Goliath-intermediate G-code) for goliath-prep, the job checker of the open " +
  "Goliath CNC software: millimetres, origin at the stock corner where tower 1 stands, Z = 0 at the stock " +
  "top, one tool, XY arcs. Set the post property 'Tower 2 axis' before posting.";

extension = "gi";
setCodePage("ascii");

capabilities = CAPABILITY_MILLING;

// Circular motion (Post Processor Training Guide, "Circular Interpolation Settings").
// Arcs that do not meet these settings are linearized by the post engine
// within `tolerance`; the ones that reach onCircular are XY arcs of at most
// 180 degrees, without helix or spiral. onCircular still checks each of these
// and refuses anything else.
tolerance = spatial(0.01, MM);
// A chord of at least 0.02 mm stays at least 0.019 mm after rounding to three
// decimals, far from GI's ambiguous zone below 0.01 mm (gi-format.md 8.7).
minimumChordLength = spatial(0.02, MM);
minimumCircularRadius = spatial(0.01, MM);
// Keeps I and J well inside GI's +/-10000 mm range.
maximumCircularRadius = spatial(5000, MM);
minimumCircularSweep = toRad(0.01);
maximumCircularSweep = toRad(180);
allowHelicalMoves = false;
allowSpiralMoves = false;
allowedCircularPlanes = 1 << PLANE_XY;

properties = {
  tower2Axis: {
    title: "Tower 2 axis",
    description: "Where tower 2 stands, seen from tower 1 (GI TOWER2). Y: tower 2 at X = 0, Y = panel " +
      "height. X: tower 2 at X = panel width, Y = 0. It must match the job's job.toml. There is no " +
      "default: the post refuses until you choose.",
    type: "enum",
    values: [
      {title: "Not set (the post refuses)", id: "unset"},
      {title: "Y: tower 2 at X = 0, Y = panel height", id: "Y"},
      {title: "X: tower 2 at X = panel width, Y = 0", id: "X"}
    ],
    value: "unset",
    scope: "post"
  }
};

// ===========================================================================
// Constants
// ===========================================================================

var GI_POST_NAME = "goliath.cps";
var GI_POST_VERSION = "0.1.0";
var GI_VERSION_LINE = ";GOLIATH-GI 1";

// A stock corner or top counts as 0 when it is closer than half the file's
// resolution (three decimals): the file could not show a smaller offset.
var GI_ORIGIN_TOLERANCE = 0.0005; // mm
// The work plane of every operation must be the setup's own (top view, no tilt).
var GI_DIRECTION_TOLERANCE = 1e-6;
// GI refuses coordinates beyond +/-10000 mm (gi-format.md 8.2), in thousandths.
var GI_MAX_COORDINATE = 10000000;
// GI arcs: end and start at least 0.01 mm apart, radii within 0.005 mm (8.7), in thousandths.
var GI_MIN_ARC_CHORD = 10;
var GI_MAX_RADIUS_MISMATCH = 5;
// A sweep this close above 180 degrees is still 180 degrees (floating point).
var GI_SWEEP_SLACK = 1e-9;

// Fusion operation strategies (section.strategy) that have a GI operation kind.
// Every other strategy is written as kind=other, with a note in the file.
var GI_OP_KINDS = {
  "contour2d": "profile",
  "pocket2d": "pocket",
  "adaptive2d": "adaptive",
  "adaptive": "adaptive",
  "drill": "drill",
  "engrave": "engrave",
  "trace": "engrave"
};

// Drilling cycles that Fusion's post engine expands (expandCyclePoint) into
// moves and dwells only. Every other cycle is refused: tapping and boring
// cycles reverse, stop or orient the spindle, the milling cycles carry their
// own compensation, and probing is not possible on the Goliath.
var GI_EXPANDED_CYCLES = {
  "drilling": "Drilling - rapid out",
  "counter-boring": "Counterboring - dwell and rapid out",
  "chip-breaking": "Chip breaking - partial retract",
  "deep-drilling": "Deep drilling - full retract",
  "reaming": "Reaming - feed out"
};

// Fusion's stock box, as global parameters in the setup's WCS (the names are
// those of the training guide's parameter dump; checked against Fusion's own
// output on the first run, see posts/fusion/README.md).
var GI_STOCK_PARAMETERS = [
  ["stock-lower-x", "lowerX"],
  ["stock-lower-y", "lowerY"],
  ["stock-lower-z", "lowerZ"],
  ["stock-upper-x", "upperX"],
  ["stock-upper-y", "upperY"],
  ["stock-upper-z", "upperZ"]
];

// ===========================================================================
// Pure formatting: numbers, text, annotations. No post engine state is used
// here, so the test harness can call these functions directly.
// ===========================================================================

/** Stops the post with a message for the owner (error() aborts the script). */
function giRefuse(message) {
  var text = "Goliath post: " + message;
  error(text);
  throw new Error(text); // only reached if error() ever returned
}

function giZeros(count) {
  var text = "";
  for (var i = 0; i < count; ++i) {
    text += "0";
  }
  return text;
}

/**
  The exact decimal digits of a number's shortest round-trip form:
  {negative, digits, exponent} with |value| = digits x 10^exponent.
*/
function giDecimalParts(value) {
  var text = String(Math.abs(value));
  var exponent = 0;
  var e = text.indexOf("e");
  if (e >= 0) {
    exponent = parseInt(text.substring(e + 1), 10);
    text = text.substring(0, e);
  }
  var point = text.indexOf(".");
  if (point >= 0) {
    exponent -= text.length - point - 1;
    text = text.substring(0, point) + text.substring(point + 1);
  }
  text = text.replace(/^0+/, "");
  if (text == "") {
    text = "0";
  }
  return {negative: value < 0, digits: text, exponent: exponent};
}

/**
  `value` rounded to `decimals` decimals, as a whole number of 10^-decimals
  units. Rounds the shortest decimal form of the number, half to even, so
  1.0005 gives 1000 thousandths and 1.0015 gives 1002 (the rule of the
  FreeCAD post, posts/freecad/gi_format.py).
*/
function giScaled(value, decimals, what) {
  if (typeof value != "number" || !isFinite(value)) {
    giRefuse(what + " is " + value + ", not a number. Regenerate the toolpath and post again");
  }
  if (Math.abs(value) >= 1e9) {
    giRefuse(what + " is " + value + ", far outside anything GI can carry");
  }
  var parts = giDecimalParts(value);
  var shift = parts.exponent + decimals;
  var result;
  if (shift >= 0) {
    result = parseInt(parts.digits + giZeros(shift), 10);
  } else {
    var cut = -shift;
    var kept;
    var dropped;
    if (parts.digits.length <= cut) {
      kept = "0";
      dropped = giZeros(cut - parts.digits.length) + parts.digits;
    } else {
      kept = parts.digits.substring(0, parts.digits.length - cut);
      dropped = parts.digits.substring(parts.digits.length - cut);
    }
    result = parseInt(kept, 10);
    var half = "5" + giZeros(cut - 1);
    if (dropped > half || (dropped == half && result % 2 == 1)) {
      result += 1;
    }
  }
  return (parts.negative && result != 0) ? -result : result;
}

/** A length in whole thousandths of a millimetre. */
function giThousandths(value, what) {
  return giScaled(value, 3, what || "a length");
}

/** Thousandths of a millimetre as GI writes them: three decimals, never "-0.000". */
function giFormatThousandths(n) {
  var magnitude = Math.abs(n);
  var whole = Math.floor(magnitude / 1000);
  var fraction = String(magnitude - whole * 1000);
  while (fraction.length < 3) {
    fraction = "0" + fraction;
  }
  return (n < 0 ? "-" : "") + whole + "." + fraction;
}

/** A length in millimetres, three decimals. */
function giMm(value, what) {
  return giFormatThousandths(giThousandths(value, what));
}

/** A whole number (feeds in mm/min, spindle speeds in rpm), half to even. */
function giWhole(value, what) {
  return giScaled(value, 0, what || "a number");
}

/**
  Text for a GI value or comment: ASCII only (the post writes the ascii code
  page), control characters as spaces, runs of white space as one space.
*/
function giText(text) {
  var out = "";
  var source = (text === undefined || text === null) ? "" : String(text);
  for (var i = 0; i < source.length; ++i) {
    var code = source.charCodeAt(i);
    if (code < 0x20 || code == 0x7f) {
      out += " ";
    } else if (code > 0x7e) {
      out += "?";
    } else {
      out += source.charAt(i);
    }
  }
  return out.replace(/ +/g, " ").replace(/^ /, "").replace(/ $/, "");
}

/** A GI quoted value (gi-format.md 4.1): backslash and double quote escaped. */
function giQuoted(text) {
  return "\"" + giText(text).replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"";
}

/**
  A plain comment, "; <prefix>: <text>". The prefix ("Note" or "Fusion")
  keeps it from reading as an annotation or as a ;U or ;S marker.
*/
function giComment(prefix, text) {
  return "; " + prefix + ": " + giText(text);
}

/** The GI operation kind for a Fusion strategy, or null (then kind=other). */
function giOpKind(strategy) {
  var key = String(strategy || "").toLowerCase();
  return GI_OP_KINDS.hasOwnProperty(key) ? GI_OP_KINDS[key] : null;
}

/** The GI tool kind for a Fusion tool type, or null (then TOOL carries no kind). */
function giToolKind(type) {
  if (type == TOOL_MILLING_END_FLAT) {
    return "flat";
  }
  if (type == TOOL_MILLING_END_BALL) {
    return "ball";
  }
  if (type == TOOL_MILLING_END_BULLNOSE) {
    return "bull";
  }
  return null;
}

/**
  The header lines (gi-format.md 6): `job` holds cam, stock {x, y, t in
  thousandths}, tower2, tool {number, d, flute in thousandths, kind, rpm,
  name} and notes.
*/
function giHeaderLines(job) {
  var tool = job.tool;
  var toolWords = ["n=" + tool.number, "d=" + giFormatThousandths(tool.d), "flute=" + giFormatThousandths(tool.flute)];
  if (tool.kind) {
    toolWords.push("kind=" + tool.kind);
  }
  if (tool.rpm) {
    toolWords.push("rpm=" + tool.rpm);
  }
  if (tool.name) {
    toolWords.push("name=" + giQuoted(tool.name));
  }
  var lines = [
    GI_VERSION_LINE,
    ";GOLIATH SOURCE cam=" + giQuoted(job.cam) + " post=" + giQuoted(GI_POST_NAME + " " + GI_POST_VERSION),
    ";GOLIATH UNITS mm",
    ";GOLIATH STOCK x=" + giFormatThousandths(job.stock.x) + " y=" + giFormatThousandths(job.stock.y) +
      " t=" + giFormatThousandths(job.stock.t) + " origin=t1-corner zzero=top",
    ";GOLIATH TOWER2 axis=" + job.tower2,
    ";GOLIATH TOOL " + toolWords.join(" "),
    // The post applies no shift: Fusion's WCS already has Z = 0 at the stock top.
    ";GOLIATH ZSHIFT 0.000"
  ];
  for (var i = 0; i < job.notes.length; ++i) {
    lines.push(giComment("Note", job.notes[i]));
  }
  lines.push("G17 G21 G90");
  return lines;
}

/** The OP line of one operation (gi-format.md 7). */
function giOpLine(id, kind, toolNumber, label) {
  var line = ";GOLIATH OP id=" + id + " kind=" + kind + " through=auto tool=" + toolNumber;
  if (label) {
    line += " label=" + giQuoted(label);
  }
  return line;
}

// ===========================================================================
// Reading the job from Fusion (onOpen): checks first, then the header
// ===========================================================================

function giSectionName(section) {
  var comment = giText(section.getParameter("operation-comment", ""));
  var number = section.getId() + 1;
  return comment ? "operation \"" + comment + "\" (operation " + number + " in the NC program)" :
    "operation " + number + " in the NC program";
}

function giNearlyZero(value, tolerance) {
  return typeof value == "number" && isFinite(value) && Math.abs(value) < tolerance;
}

function giSameDirection(v, x, y, z) {
  return giNearlyZero(v.x - x, GI_DIRECTION_TOLERANCE) && giNearlyZero(v.y - y, GI_DIRECTION_TOLERANCE) &&
    giNearlyZero(v.z - z, GI_DIRECTION_TOLERANCE);
}

function giSamePlane(a, b) {
  return giSameDirection(a.right, b.right.x, b.right.y, b.right.z) &&
    giSameDirection(a.up, b.up.x, b.up.y, b.up.z) &&
    giSameDirection(a.forward, b.forward.x, b.forward.y, b.forward.z);
}

function giVectorText(v) {
  return "(" + giMm(v.x) + ", " + giMm(v.y) + ", " + giMm(v.z) + ")";
}

/** Everything about one operation that GI cannot carry, refused before anything is written. */
function giCheckSection(section, first) {
  var name = giSectionName(section);
  if (section.getType() != TYPE_MILLING) {
    giRefuse(name + " is not a milling operation. The Goliath is a router: post milling and drilling " +
      "operations only");
  }
  if (section.isMultiAxis()) {
    giRefuse(name + " is a multi-axis toolpath. GI has X, Y and Z only: use 2D or 3D operations without " +
      "tool axis tilting");
  }
  if (section.isOptional()) {
    giRefuse(name + " is marked optional (block skip). GI has no optional blocks: make it a normal " +
      "operation or leave it out of the NC program");
  }
  var plane = section.workPlane;
  if (!giSameDirection(plane.right, 1, 0, 0) || !giSameDirection(plane.up, 0, 1, 0) ||
      !giSameDirection(plane.forward, 0, 0, 1)) {
    giRefuse(name + " uses a tool orientation other than the setup's (a tilted or rotated work plane). " +
      "GI is written in the setup's WCS only: turn off Tool Orientation in the operation's Geometry tab");
  }
  var origin = section.workOrigin;
  if (!giNearlyZero(origin.x, GI_ORIGIN_TOLERANCE) || !giNearlyZero(origin.y, GI_ORIGIN_TOLERANCE) ||
      !giNearlyZero(origin.z, GI_ORIGIN_TOLERANCE)) {
    giRefuse(name + " uses its own origin " + giVectorText(origin) + " instead of the setup's WCS origin. " +
      "Turn off Tool Orientation (and its origin) in the operation's Geometry tab");
  }
  var globalOrigin = section.getGlobalWorkOrigin();
  var firstOrigin = first.getGlobalWorkOrigin();
  if (!giNearlyZero(globalOrigin.x - firstOrigin.x, GI_ORIGIN_TOLERANCE) ||
      !giNearlyZero(globalOrigin.y - firstOrigin.y, GI_ORIGIN_TOLERANCE) ||
      !giNearlyZero(globalOrigin.z - firstOrigin.z, GI_ORIGIN_TOLERANCE) ||
      !giSamePlane(section.getGlobalWorkPlane(), first.getGlobalWorkPlane())) {
    giRefuse(name + " has another WCS than " + giSectionName(first) + ". One GI file is one setup on " +
      "one panel: post each setup as its own NC program");
  }
  if (section.getFeedMode() != FEED_PER_MINUTE) {
    giRefuse(name + " does not use feed per minute. GI feeds are millimetres per minute: turn off feed " +
      "per revolution and inverse time feeds for this operation");
  }
}

/** The one tool of the file, from every operation's tool; refuses a second tool. */
function giJobTool(sections) {
  var first = sections[0].getTool();
  var firstName = giSectionName(sections[0]);
  if (typeof first.number != "number" || first.number < 0 || Math.floor(first.number) != first.number) {
    giRefuse(firstName + " uses a tool numbered " + first.number + "; GI needs a whole tool number");
  }
  var tool = {
    number: first.number,
    d: giThousandths(first.diameter, "the tool diameter"),
    flute: giThousandths(first.fluteLength, "the tool flute length"),
    kind: giToolKind(first.type),
    rpm: null,
    name: null
  };
  var notes = [];
  var speeds = [];
  var names = [];
  var unnamed = false;
  for (var i = 0; i < sections.length; ++i) {
    var name = giSectionName(sections[i]);
    var t = sections[i].getTool();
    if (t.unit != MM) {
      giRefuse(name + " uses tool T" + t.number + ", which is defined in inches. The post cannot be " +
        "sure which unit Fusion hands its sizes in, so it does not convert them: use a tool defined in " +
        "millimetres (copy the tool into a millimetre tool library, or change the tool's unit)");
    }
    if (t.number !== first.number) {
      giRefuse("the NC program uses more than one tool: " + firstName + " uses T" + first.number + ", " +
        name + " uses T" + t.number + ". GI carries one tool and the Goliath has no tool changer: post " +
        "the operations of each tool as their own NC program, one file per tool");
    }
    var d = giThousandths(t.diameter, "the tool diameter");
    var flute = giThousandths(t.fluteLength, "the tool flute length");
    if (d != tool.d || flute != tool.flute || giToolKind(t.type) != tool.kind) {
      giRefuse(firstName + " and " + name + " both use T" + t.number + ", but not the same tool (diameter " +
        giFormatThousandths(tool.d) + " and " + giFormatThousandths(d) + " mm, flute length " +
        giFormatThousandths(tool.flute) + " and " + giFormatThousandths(flute) + " mm). Use one tool " +
        "definition for every operation");
    }
    if (!t.isClockwise()) {
      giRefuse(name + " turns the spindle counter-clockwise. The Goliath spindle turns clockwise only: " +
        "set the tool's spindle direction to clockwise");
    }
    var rpms = [t.spindleRPM];
    if (typeof t.rampingSpindleRPM == "number" && t.rampingSpindleRPM != 0) {
      rpms.push(t.rampingSpindleRPM);
    }
    for (var r = 0; r < rpms.length; ++r) {
      var rpm = giWhole(rpms[r], "the spindle speed");
      if (rpm < 1) {
        giRefuse(name + " runs the spindle at " + rpms[r] + " rpm. Set the operation's spindle speed " +
          "(and ramp spindle speed) above 0");
      }
      if (speeds.indexOf(rpm) < 0) {
        speeds.push(rpm);
      }
    }
    var description = giText(t.getDescription());
    if (description) {
      if (names.indexOf(description) < 0) {
        names.push(description);
      }
    } else {
      unnamed = true;
    }
  }
  if (tool.d <= 0 || tool.flute <= 0) {
    giRefuse("tool T" + tool.number + " has diameter " + giFormatThousandths(tool.d) + " mm and flute " +
      "length " + giFormatThousandths(tool.flute) + " mm. GI needs both above 0: set them in the tool " +
      "library");
  }
  if (tool.kind == null) {
    notes.push("Fusion's tool type of T" + tool.number + " is not one GI names (flat, ball or bull nose " +
      "end mill), so TOOL has no kind");
  }
  if (speeds.length == 1) {
    tool.rpm = speeds[0];
  } else {
    speeds.sort(function (a, b) { return a - b; });
    notes.push("the operations run T" + tool.number + " at different spindle speeds (" + speeds.join(", ") +
      " rpm), so TOOL has no rpm; each speed is written where it starts");
  }
  if (names.length == 1 && !unnamed) {
    tool.name = names[0];
  } else if (names.length > 1) {
    notes.push("the operations describe T" + tool.number + " differently, so TOOL has no name");
  }
  return {tool: tool, notes: notes};
}

/** Fusion's stock box, which must start at the tower-1 corner and have its top at Z = 0. */
function giJobStock() {
  var raw = {};
  for (var i = 0; i < GI_STOCK_PARAMETERS.length; ++i) {
    var parameter = GI_STOCK_PARAMETERS[i][0];
    if (!hasGlobalParameter(parameter)) {
      giRefuse("Fusion did not pass the stock parameter '" + parameter + "', so the post cannot write " +
        "the panel size or check the WCS origin. Make sure the setup has a stock, and report this with " +
        "your Fusion version");
    }
    var value = Number(getGlobalParameter(parameter));
    if (!isFinite(value)) {
      giRefuse("Fusion passed the stock parameter '" + parameter + "' as '" +
        getGlobalParameter(parameter) + "', not a number");
    }
    raw[GI_STOCK_PARAMETERS[i][1]] = value;
  }
  var box = "X " + giMm(raw.lowerX) + " to " + giMm(raw.upperX) + ", Y " + giMm(raw.lowerY) + " to " +
    giMm(raw.upperY) + ", Z " + giMm(raw.lowerZ) + " to " + giMm(raw.upperZ);
  if (!giNearlyZero(raw.lowerX, GI_ORIGIN_TOLERANCE) || !giNearlyZero(raw.lowerY, GI_ORIGIN_TOLERANCE)) {
    giRefuse("the WCS origin is not at the stock corner where tower 1 stands: in the setup's WCS the " +
      "stock spans " + box + ". GI needs X = 0, Y = 0 at that corner, with the stock along +X and +Y. " +
      "In the setup, set the WCS origin to the stock box point at that corner (the lowest X and Y), on " +
      "the top face, with Z pointing up");
  }
  if (!giNearlyZero(raw.upperZ, GI_ORIGIN_TOLERANCE)) {
    giRefuse("Z = 0 is not at the stock top: in the setup's WCS the stock spans " + box + ". GI needs " +
      "Z = 0 at the top of the stock, with cuts below it. In the setup, put the WCS origin on the top " +
      "face of the stock (stock box point, top), with Z pointing up");
  }
  var stock = {
    x: giThousandths(raw.upperX - raw.lowerX, "the stock size along X"),
    y: giThousandths(raw.upperY - raw.lowerY, "the stock size along Y"),
    t: giThousandths(raw.upperZ - raw.lowerZ, "the stock thickness"),
    raw: raw,
    box: box
  };
  if (stock.x <= 0 || stock.y <= 0 || stock.t <= 0) {
    giRefuse("the stock has no size (" + box + "). Give the setup a box stock the size of the panel");
  }
  return stock;
}

// ===========================================================================
// State of the program being written
// ===========================================================================

var giLines = []; // the whole program; written in onClose, so a refused job writes none
var giStock = null; // the stock box read in onOpen
var giToolNumber = null;
var giWritten = {x: null, y: null, z: null}; // last written position, thousandths; null: not yet written
var giFeedWritten = null; // last F written in the current operation, mm/min
var giPending = null; // the section's initial position, written before its first move
var giOp = null; // the open operation: {id, name, moves}

function giRequireOperation(what) {
  if (!giOp) {
    giRefuse("Fusion sent " + what + " outside an operation. GI motion belongs to an operation; " +
      "please report this with the NC program");
  }
}

/** The F word for a feed move: written when it changes and on the first feed move of each operation. */
function giFeedWord(feed) {
  var f = giWhole(feed, "the feed rate");
  if (f < 1) {
    giRefuse(giOp.name + " has a feed rate of " + feed + " mm/min, which is 0 in whole mm/min. Set the " +
      "operation's feed rates");
  }
  if (f === giFeedWritten) {
    return null;
  }
  giFeedWritten = f;
  return "F" + f;
}

function giCoordinate(value, axis) {
  var n = giThousandths(value, "the " + axis + " coordinate");
  if (Math.abs(n) > GI_MAX_COORDINATE) {
    giRefuse((giOp ? giOp.name + ": " : "") + axis + " " + giFormatThousandths(n) + " mm is beyond " +
      "GI's range of +/-10000 mm");
  }
  return n;
}

/**
  One linear move ("G0" or "G1") to the axes `target` names. Only axes whose
  written value changes are written; a move that changes none writes nothing
  (it does not move at the file's resolution).
*/
function giMove(code, target, feed) {
  var words = [code];
  var axes = ["x", "y", "z"];
  for (var i = 0; i < axes.length; ++i) {
    var axis = axes[i];
    if (target.hasOwnProperty(axis)) {
      var n = giCoordinate(target[axis], axis.toUpperCase());
      if (n !== giWritten[axis]) {
        words.push(axis.toUpperCase() + giFormatThousandths(n));
        giWritten[axis] = n;
      }
    }
  }
  if (words.length == 1) {
    return;
  }
  if (code != "G0") {
    var f = giFeedWord(feed);
    if (f) {
      words.push(f);
    }
  }
  giLines.push(words.join(" "));
  giOp.moves += 1;
}

/**
  The section's initial position, written before its first move, the same
  way Fusion sends that move: G0 when it is a rapid, G1 at its feed when it is
  a feed move (Fusion for Personal Use sends rapids as feed moves; the post
  never turns them into G0). The tool goes up first and then across, or across
  first and then down, never diagonally down; at the start of the program Z
  goes first.

  This move is the post's own: Fusion gives no path for it. So it must not
  cut: a move across in X or Y (or from an X and Y the program has not yet
  written) below the stock top is refused rather than guessed, and so is a
  first move of the program that goes below the stock top (gi-format.md 14,
  point 3).
*/
function giFlushPositioning(code, feed) {
  if (!giPending) {
    return;
  }
  var p = giPending;
  giPending = null;
  var z = giCoordinate(p.z, "Z");
  var upFirst = giWritten.z === null || z > giWritten.z;
  var acrossZ = upFirst ? z : giWritten.z;
  var across = giWritten.x === null || giWritten.y === null || giCoordinate(p.x, "X") !== giWritten.x ||
    giCoordinate(p.y, "Y") !== giWritten.y;
  if (across && acrossZ < 0) {
    giRefuse(giOp.name + " starts below the stock top at " + giVectorText(p) + ", and the tool is not above " +
      "that point: the post's move there would cross the panel at Z " + giFormatThousandths(acrossZ) + " mm. " +
      "Fusion gives no path for that move, so the post does not guess one. Set the operation's clearance " +
      "and retract heights above the stock top (Heights tab)");
  }
  if (upFirst) {
    giMove(code, {z: p.z}, feed);
    giMove(code, {x: p.x, y: p.y}, feed);
  } else {
    giMove(code, {x: p.x, y: p.y}, feed);
    giMove(code, {z: p.z}, feed);
  }
}

// ===========================================================================
// Entry functions
// ===========================================================================

function onOpen() {
  if (unit != MM) {
    giRefuse("the output unit is inches. GI is millimetres only, and the post does not convert a job " +
      "made in inches, because Fusion hands some values (the stock and the tools) in the unit they were " +
      "made in. Set the document's units to millimetres and Unit to Millimeters (or Document unit) in " +
      "the NC program, then post again");
  }
  var tower2 = getProperty("tower2Axis");
  if (tower2 != "X" && tower2 != "Y") {
    giRefuse("the post property 'Tower 2 axis' is not set. Set it to Y if tower 2 stands at X = 0, " +
      "Y = panel height, or to X if it stands at X = panel width, Y = 0, as the job's job.toml says");
  }
  var count = getNumberOfSections();
  if (!(count > 0)) {
    giRefuse("there is no operation to post");
  }
  var sections = [];
  for (var i = 0; i < count; ++i) {
    sections.push(getSection(i));
  }
  for (var j = 0; j < sections.length; ++j) {
    giCheckSection(sections[j], sections[0]);
  }
  var jobTool = giJobTool(sections);
  giToolNumber = jobTool.tool.number;
  giStock = giJobStock();
  var cam = giText(getGlobalParameter("generated-by", ""));
  var notes = ["stock box from Fusion, in the setup's WCS: " + giStock.box].concat(jobTool.notes);
  giLines = giHeaderLines({
    cam: cam ? cam : "Autodesk Fusion (version not reported)",
    stock: giStock,
    tower2: tower2,
    tool: jobTool.tool,
    notes: notes
  });
}

function onParameter(name, value) {
  if (name == "call-subprogram") {
    giRefuse("a Manual NC 'Call program' command (" + giText(value) + "). GI carries no subprogram " +
      "calls: remove the Manual NC command");
  }
  // The other Manual NC commands that reach onParameter (training guide 6.3)
  // have nothing to act on in GI; a note keeps them from vanishing unseen.
  if (name == "display") {
    giLines.push(giComment("Note", "Fusion Manual NC display message, not shown on the Goliath: " +
      giText(value)));
    return;
  }
  if (name == "Print" || name == "print") {
    giLines.push(giComment("Note", "Fusion Manual NC print message, not printed on the Goliath: " +
      giText(value)));
    return;
  }
  if (name == "action") {
    giLines.push(giComment("Note", "Fusion Manual NC action " + giQuoted(value) + " not used: this " +
      "post has no actions"));
    return;
  }
  // Global parameters come again for each setup of the NC program; a second
  // setup with another stock would make the header wrong.
  if (giStock) {
    for (var i = 0; i < GI_STOCK_PARAMETERS.length; ++i) {
      if (name == GI_STOCK_PARAMETERS[i][0]) {
        var known = giStock.raw[GI_STOCK_PARAMETERS[i][1]];
        var v = Number(value);
        if (!isFinite(v) || Math.abs(v - known) >= GI_ORIGIN_TOLERANCE) {
          giRefuse("the NC program holds operations of setups with different stock (" + name + " is " +
            giMm(known) + " in the first setup and " + giText(value) + " in a later one). One GI file " +
            "is one panel: post each setup as its own NC program");
        }
      }
    }
  }
}

function onSection() {
  var section = currentSection;
  var name = giSectionName(section);
  var id = "op" + (section.getId() + 1);
  var strategy = String(section.strategy || "");
  var kind = giOpKind(strategy);
  if (!kind) {
    giLines.push(giComment("Note", "Fusion strategy \"" + strategy + "\" of " + name + " has no GI " +
      "operation kind; written as kind=other"));
    kind = "other";
  }
  giLines.push(giOpLine(id, kind, giToolNumber, giText(section.getParameter("operation-comment", ""))));
  var rpm = giWhole(section.getTool().spindleRPM, "the spindle speed");
  giLines.push("M3 S" + rpm);
  giOp = {id: id, name: name, moves: 0};
  giFeedWritten = null; // each operation states its feed on its first feed move
  giPending = section.getInitialPosition();
}

function onRadiusCompensation() {
  if (radiusCompensation != RADIUS_COMPENSATION_OFF) {
    giRefuse((giOp ? giOp.name : "an operation") + " turns on radius compensation in the control " +
      "(G41/G42). GI carries the tool centre path only: set Compensation type to 'In computer' in the " +
      "operation's Passes tab");
  }
}

function onRapid(x, y, z) {
  giRequireOperation("a rapid move");
  giFlushPositioning("G0", null);
  giMove("G0", {x: x, y: y, z: z}, null);
}

function onLinear(x, y, z, feed) {
  giRequireOperation("a feed move");
  giFlushPositioning("G1", feed);
  giMove("G1", {x: x, y: y, z: z}, feed);
}

function onCircular(clockwise, cx, cy, cz, x, y, z, feed) {
  giRequireOperation("an arc");
  var name = giOp.name;
  if (getCircularPlane() != PLANE_XY) {
    giRefuse(name + ": an arc outside the XY plane reached the post. GI arcs lie in the XY plane; " +
      "please report this with the NC program");
  }
  if (isHelical()) {
    giRefuse(name + ": a helical arc reached the post, which asks Fusion to linearize helices; " +
      "please report this with the NC program");
  }
  if (isSpiral()) {
    giRefuse(name + ": a spiral arc reached the post, which asks Fusion to linearize spirals; " +
      "please report this with the NC program");
  }
  if (isFullCircle() || getCircularSweep() > Math.PI + GI_SWEEP_SLACK) {
    giRefuse(name + ": an arc of more than 180 degrees reached the post, which asks Fusion to " +
      "linearize those; please report this with the NC program");
  }
  giFlushPositioning("G1", feed);
  var sx = giWritten.x;
  var sy = giWritten.y;
  var ex = giCoordinate(x, "X");
  var ey = giCoordinate(y, "Y");
  var ez = giCoordinate(z, "Z");
  // The centre relative to the start as written, so the written centre is
  // Fusion's centre rounded once.
  var i = giCoordinate(cx, "arc centre X") - sx;
  var j = giCoordinate(cy, "arc centre Y") - sy;
  var dx = ex - sx;
  var dy = ey - sy;
  if (dx * dx + dy * dy < GI_MIN_ARC_CHORD * GI_MIN_ARC_CHORD) {
    giRefuse(name + ": an arc whose end, written with three decimals, is less than 0.01 mm from its " +
      "start (GI reads that as a full circle or refuses it as ambiguous). Please report this with the " +
      "NC program");
  }
  var startRadius = Math.sqrt(i * i + j * j);
  var endRadius = Math.sqrt((ex - sx - i) * (ex - sx - i) + (ey - sy - j) * (ey - sy - j));
  if (startRadius == 0 || Math.abs(startRadius - endRadius) > GI_MAX_RADIUS_MISMATCH) {
    giRefuse(name + ": an arc whose start and end are not on one circle at the file's resolution " +
      "(radii " + (startRadius / 1000) + " and " + (endRadius / 1000) + " mm). Please report this with " +
      "the NC program");
  }
  var words = [clockwise ? "G2" : "G3", "X" + giFormatThousandths(ex), "Y" + giFormatThousandths(ey)];
  if (ez !== giWritten.z) {
    words.push("Z" + giFormatThousandths(ez));
  }
  words.push("I" + giFormatThousandths(i));
  words.push("J" + giFormatThousandths(j));
  var f = giFeedWord(feed);
  if (f) {
    words.push(f);
  }
  giWritten.x = ex;
  giWritten.y = ey;
  giWritten.z = ez;
  giLines.push(words.join(" "));
  giOp.moves += 1;
}

function onCycle() {
  giRequireOperation("a canned cycle");
  if (!GI_EXPANDED_CYCLES.hasOwnProperty(cycleType)) {
    var choices = [];
    for (var key in GI_EXPANDED_CYCLES) {
      if (GI_EXPANDED_CYCLES.hasOwnProperty(key)) {
        choices.push("'" + GI_EXPANDED_CYCLES[key] + "'");
      }
    }
    giRefuse(giOp.name + " uses the cycle '" + cycleType + "', which this post does not expand for the " +
      "Goliath. Use one of " + choices.join(", ") + ", or a 2D Bore or Circular operation for large holes");
  }
  giLines.push(giComment("Note", "Fusion " + cycleType + " cycle, expanded into moves by Fusion's post " +
    "engine"));
}

function onCyclePoint(x, y, z) {
  giRequireOperation("a cycle point");
  if (!GI_EXPANDED_CYCLES.hasOwnProperty(cycleType)) {
    giRefuse(giOp.name + " uses the cycle '" + cycleType + "', which this post does not expand");
  }
  expandCyclePoint(x, y, z);
}

function onCycleEnd() {
}

function onDwell(seconds) {
  if (typeof seconds != "number" || !(seconds >= 0)) {
    giRefuse("a dwell of " + seconds + " seconds");
  }
  // goliath-prep refuses G4 until its dwell gate opens (gi-format.md 8.5); the post writes it as given.
  giLines.push("G4 P" + giMm(seconds, "the dwell time"));
}

function onSpindleSpeed(speed) {
  var rpm = giWhole(speed, "the spindle speed");
  if (rpm < 1) {
    giRefuse((giOp ? giOp.name + ": " : "") + "Fusion changes the spindle speed to " + speed + " rpm. " +
      "The Goliath spindle turns clockwise only, above 0 rpm");
  }
  giLines.push("M3 S" + rpm);
}

function onCommand(command) {
  if (command == COMMAND_COOLANT_ON || command == COMMAND_COOLANT_OFF) {
    giLines.push(giComment("Note", "Fusion coolant command not written: the Goliath has no coolant"));
    return;
  }
  if (command == COMMAND_STOP_SPINDLE) {
    giLines.push("M5");
    return;
  }
  if (command == COMMAND_SPINDLE_CLOCKWISE || command == COMMAND_START_SPINDLE) {
    var rpm = giWhole(tool.spindleRPM, "the spindle speed");
    if (rpm < 1 || !tool.isClockwise()) {
      giRefuse("Fusion starts the spindle at " + tool.spindleRPM + " rpm. The Goliath spindle turns " +
        "clockwise only, above 0 rpm");
    }
    giLines.push("M3 S" + rpm);
    return;
  }
  var what = "machine command number " + command;
  if (command == COMMAND_STOP || command == COMMAND_OPTIONAL_STOP) {
    what = "a program stop (M0/M1); nothing can resume a paused program on the Goliath";
  } else if (command == COMMAND_END) {
    what = "a program end inside the program";
  } else if (command == COMMAND_LOAD_TOOL) {
    what = "a tool change; GI carries one tool";
  } else if (command == COMMAND_SPINDLE_COUNTERCLOCKWISE) {
    what = "a counter-clockwise spindle; the Goliath spindle turns one way";
  }
  giRefuse("Fusion asked for " + what + ". GI cannot carry it: remove the Manual NC command, or the " +
    "operation setting that asks for it");
}

function onComment(text) {
  giLines.push(giComment("Fusion", text));
}

function onPassThrough(text) {
  giRefuse("a Manual NC 'Pass through' command (" + giText(text) + "). GI carries no raw controller " +
    "text: goliath-prep writes every machine word. Remove the Manual NC command");
}

function onRapid5D(x, y, z, dx, dy, dz) {
  giRefuse((giOp ? giOp.name + ": " : "") + "a 5-axis move. GI has X, Y and Z only");
}

function onLinear5D(x, y, z, dx, dy, dz, feed) {
  giRefuse((giOp ? giOp.name + ": " : "") + "a 5-axis move. GI has X, Y and Z only");
}

function onSectionEnd() {
  if (!giOp) {
    return;
  }
  if (giPending || giOp.moves == 0) {
    giRefuse(giOp.name + " has no moves. Regenerate its toolpath, or leave it out of the NC program");
  }
  giLines.push(";GOLIATH OPEND id=" + giOp.id);
  giOp = null;
}

function onClose() {
  giLines.push("M5");
  giLines.push("M30");
  for (var i = 0; i < giLines.length; ++i) {
    writeln(giLines[i]);
  }
}
