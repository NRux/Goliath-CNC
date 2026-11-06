# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Regenerate the committed probe reports: goliath-link's probe run against its simulator.

    python goliath-run/tests/fixtures/probe_reports/make_probe_reports.py

Needs goliath-link from this checkout (``pip install -e goliath-link``). Each
scenario starts a :class:`goliath_link.sim.SimulatedGoliath`, runs
:func:`goliath_link.probe.run_probe` against it with short timings and keeps the
files the probe wrote, unedited, in a folder named after the scenario. Nothing in
them is typed by hand: every field is what the probe wrote. The serial number in
them (``000000000001``) is the simulator's placeholder, not a robot's.

Which probe wrote them: the probe as frozen for rung R1 (goliath-link at commit
a647bbd) wrote every folder except ``gfw-01.01.09``, which the hotfixed probe
(firmware gate, shareable copies) rewrote; that probe also writes the
``.shareable`` copies. The tests also run scenarios live with the probe of this
checkout and compare the facts with the committed reports, which shows that the
hotfix changed nothing the import of a run on 01.02.00 or newer depends on.

``frozen-probe-gfw-01.01.09`` is no scenario: the frozen probe swept firmware
older than 01.02.00 (01.01.09 here) like any other, which the probe of this
checkout no longer does, so that report cannot be made again. It stays to show
how a report the frozen probe wrote on old firmware (as the owner's rung R1
report, on GFW 01.00.13) is read: branch B4.

The tests of unit R01 import these reports (``import_probe_report``) and also run
one scenario live (:func:`run_scenario`), so a change of the probe's report format
shows up as a difference to the committed files.

Scenarios (the bring-up branches of the project design, section 11.2):

* ``gfw-01.02.00`` - the 01.02.00 simulator: branch B1, the fingerprint matches.
* ``gfw-01.02.00-unknown-command-error`` - ``unknown_command="error"`` (``ERR;1``
  for an unknown command, a GUESS of the simulator): branch B2.
* ``vendor-app-newer-extended`` - the newer-firmware model (01.02.03, the 19-byte
  status frame) with ``--extended``: branch B3; ``SEND TCP POS`` is answered with a
  position frame (a solicited frame, not one streamed at idle).
* ``gfw-01.01.09`` - firmware older than 01.02.00 (branch B4): the probe's firmware
  gate stops the run at the identity the simulator pushes on connect, before any
  send (exit code 4); the importer refuses the report.
* ``gfw-01.02.00-idle-frame`` - while the sweep session listens passively, the
  simulator pushes one position frame on its own (its position-frame routine, as a
  robot that streams frames outside work would: U-52).
* ``gfw-01.02.00-extended-warning`` - ``--extended`` against 01.02.00: the probe
  does not send the extended commands and finishes with a warning (exit code 3).
* ``gfw-01.02.00-slot0-occupied`` - another client holds slot 0, so every probe
  connection lands one slot later and the replies arrive on the wrong connections:
  slot-routing errors (branch B5, exit code 1).
* ``vendor-app-newer-line-framing`` - the newer-firmware model (01.02.03) reading
  commands line by line (``framing="line"``): only ``INFO\\n`` is answered, so the
  probe chooses the terminator ``lf`` and exits 0. Rung R1's go criteria include
  the terminator ``none`` (design 11.2), so this run is a no-go on every branch.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

HERE = Path(__file__).resolve().parent
STAMP = "20261104T090000Z"

# Short timings keep a run near ten seconds; the probe's own minimum pace is 0.3 s.
TIMINGS: Dict[str, Any] = dict(listen=0.3, reply_timeout=1.0, pace=0.3, slot_release_wait=0.2, connect_timeout=2.0)


def _push_idle_frame(sim: Any, stop: threading.Event) -> None:
    # Four sessions of three connections: the twelfth accept is the sweep's S3.
    if not sim.wait_for(lambda: sim.accept_count >= 12, timeout=30.0) or stop.is_set():
        return
    time.sleep(0.1)  # inside the sweep's passive listen (0.3 s), before its first command
    sim._position_block(0)  # the simulator's own frame routine: one frame to slot 2 (S3)


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "gfw-01.02.00": {"sim": {"profile": "gfw-01.02.00"}, "probe": {}},
    "gfw-01.02.00-unknown-command-error": {
        "sim": {"profile": "gfw-01.02.00", "unknown_command": "error"},
        "probe": {},
    },
    "vendor-app-newer-extended": {"sim": {"profile": "vendor-app-newer"}, "probe": {"extended": True}},
    "gfw-01.01.09": {"sim": {"profile": "gfw-01.02.00", "firmware": (1, 1, 9)}, "probe": {}},
    "gfw-01.02.00-idle-frame": {"sim": {"profile": "gfw-01.02.00"}, "probe": {}, "during": _push_idle_frame},
    "gfw-01.02.00-extended-warning": {"sim": {"profile": "gfw-01.02.00"}, "probe": {"extended": True}},
    "gfw-01.02.00-slot0-occupied": {"sim": {"profile": "gfw-01.02.00", "occupied_slots": (0,)}, "probe": {}},
    "vendor-app-newer-line-framing": {"sim": {"profile": "vendor-app-newer", "framing": "line"}, "probe": {}},
}


def run_scenario(name: str, out_dir: Path) -> Path:
    """Run one scenario; return the path of the JSON report written into ``out_dir``.

    The probe is given a relative output folder (``.``, with ``out_dir`` as the
    working directory), so the report names its files without this computer's paths.
    """
    from goliath_link.probe import run_probe
    from goliath_link.sim import SimulatedGoliath

    scenario = SCENARIOS[name]
    out_dir.mkdir(parents=True, exist_ok=True)
    during: Optional[Callable[[Any, threading.Event], None]] = scenario.get("during")
    cwd = os.getcwd()
    os.chdir(out_dir)
    try:
        with SimulatedGoliath(**scenario["sim"]) as sim:
            stop = threading.Event()
            helper = None
            if during is not None:
                helper = threading.Thread(target=during, args=(sim, stop), daemon=True)
                helper.start()
            try:
                report = run_probe(sim.host, sim.port, ".", stamp=STAMP, **TIMINGS, **scenario["probe"])
            finally:
                stop.set()
                if helper is not None:
                    helper.join(timeout=5.0)
            if sim.errors:
                raise RuntimeError("the simulator failed in scenario %s: %r" % (name, sim.errors))
    finally:
        os.chdir(cwd)
    return out_dir / Path(report["report_file"]).name


def main() -> int:
    names = sys.argv[1:] or sorted(SCENARIOS)
    for name in names:
        path = run_scenario(name, HERE / name)
        print("%s: %s" % (name, path.relative_to(HERE)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
