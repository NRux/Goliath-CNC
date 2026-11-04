# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""Disclosures and their registry: a fallback cannot exist without being shown (unit F04, design 8.5)."""
from __future__ import annotations

import dataclasses
import json
import logging
import threading

import pytest

from goliath_prep.core.canon import canonical_json
from goliath_prep.core.disclosure import (
    Disclosure,
    DisclosureRegistry,
    StickyDisclosureError,
    UndisclosedFallbackError,
)
from goliath_prep.core.findings import Finding, Witness
from goliath_prep.core.params import Param


def d(id_: str, severity: str = "amber", text: str | None = None, source: str = "test", sticky: bool = True) -> Disclosure:
    return Disclosure(id=id_, severity=severity, text=text or "%s in effect" % id_, source=source,  # type: ignore[arg-type]
                      sticky=sticky)


def unmeasured_r_w() -> Param[float]:
    return Param(
        40.0,
        "mm",
        "DEFAULT_UNMEASURED",
        "docs/prep/params.json r_w",
        disclosures=(d("DEFAULT_UNMEASURED:r_w", "amber", "r_w = 40 mm, not measured", "docs/prep/params.json r_w"),),
    )


# ---------------------------------------------------------------------------
# Disclosure
# ---------------------------------------------------------------------------


def test_disclosure_fields_and_default_sticky() -> None:
    x = Disclosure(id="UPLOAD-END:INFO0-ONLY", severity="amber", text="upload end confirmed by INFO;0 only",
                   source="firmware < 01.02.03")
    assert x.sticky is True
    assert Disclosure.from_json(json.loads(canonical_json(x))) == x
    hash(x)
    with pytest.raises(dataclasses.FrozenInstanceError):
        x.severity = "info"  # type: ignore[misc]


@pytest.mark.parametrize(
    "fields, error, message",
    [
        ({"id": ""}, ValueError, "id"),
        ({"id": "has space"}, ValueError, "id"),
        ({"id": None}, TypeError, "id"),
        ({"severity": "warn"}, ValueError, "severity"),
        ({"severity": "AMBER"}, ValueError, "severity"),
        ({"text": " "}, ValueError, "text"),
        ({"source": ""}, ValueError, "source"),
        ({"sticky": 1}, TypeError, "sticky"),
    ],
)
def test_disclosure_validation(fields: dict, error: type, message: str) -> None:
    base = {"id": "X", "severity": "info", "text": "t", "source": "s"}
    base.update(fields)
    with pytest.raises(error, match=message):
        Disclosure(**base)


def test_disclosure_from_a_disclose_finding() -> None:
    finding = Finding.of("N-DUST", "about 1.4 dm3 removed; the vendor would empty the bin here",
                         witness=Witness(value=1.4, limit=1.15, hazard="dust bin"))
    x = Disclosure.from_finding(finding, severity="amber")
    assert (x.id, x.severity, x.text, x.sticky) == ("N-DUST", "amber", finding.message, True)
    assert "N-DUST" in x.source
    with pytest.raises(TypeError):
        Disclosure.from_finding(finding)  # type: ignore[call-arg]  # the severity is chosen, never defaulted
    refusal = Finding.of("W-SURF", "m", witness=Witness(hazard="h"))
    with pytest.raises(ValueError, match="disclose"):
        Disclosure.from_finding(refusal, severity="red")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_dedup_and_severity_order() -> None:
    reg = DisclosureRegistry()
    reg.raise_(d("A", "info"))
    reg.raise_(d("B", "red"))
    reg.raise_(d("C", "amber"))
    reg.raise_(d("A", "info"))  # exact repeat: one entry
    reg.raise_(d("D", "amber"))
    reg.raise_(d("E", "red"))
    assert [x.id for x in reg.active()] == ["B", "E", "C", "D", "A"]  # red, amber, info; first raised first
    assert len(reg) == 5

    # The same disclosure raised more severely is escalated in place ...
    reg.raise_(d("A", "red"))
    assert [(x.id, x.severity) for x in reg.active()] == [
        ("A", "red"), ("B", "red"), ("E", "red"), ("C", "amber"), ("D", "amber")]
    # ... and never lowered.
    reg.raise_(d("B", "info"))
    assert reg.active()[1] == d("B", "red")
    # Sticky wins over non-sticky.
    reg.raise_(d("F", "info", sticky=False))
    reg.raise_(d("F", "info", sticky=True))
    assert [x for x in reg.active() if x.id == "F"] == [d("F", "info", sticky=True)]
    # The same id with other text or another source is another disclosure: nothing is hidden.
    reg.raise_(d("C", "amber", text="C for cut 4"))
    reg.raise_(d("C", "amber", source="other"))
    assert [x.text for x in reg.active() if x.id == "C"] == ["C in effect", "C for cut 4", "C in effect"]
    assert len(reg) == 8


def test_registry_to_json_order_and_roundtrip() -> None:
    reg = DisclosureRegistry()
    for x in (d("I", "info"), d("R", "red"), d("M", "amber", sticky=False)):
        reg.raise_(x)
    data = reg.to_json()
    assert [entry["id"] for entry in data] == ["R", "M", "I"]
    assert data[1] == {"id": "M", "severity": "amber", "text": "M in effect", "source": "test", "sticky": False}
    back = DisclosureRegistry.from_json(json.loads(canonical_json(reg)))
    assert back.active() == reg.active()
    with pytest.raises(TypeError):
        reg.raise_("UNMEASURED")  # type: ignore[arg-type]


def test_clear_refuses_sticky_and_records_the_reason(caplog: pytest.LogCaptureFixture) -> None:
    reg = DisclosureRegistry()
    reg.raise_(d("LINK", "red", sticky=False))
    reg.raise_(d("LINK", "red", text="second", sticky=False))
    reg.raise_(d("ATTEST", "amber", sticky=True))
    with pytest.raises(StickyDisclosureError, match="ATTEST"):
        reg.clear("ATTEST", "power cycle observed")
    assert [x.id for x in reg.active()] == ["LINK", "LINK", "ATTEST"]
    with pytest.raises(ValueError, match="reason"):
        reg.clear("LINK", " ")
    with caplog.at_level(logging.INFO, logger="goliath_prep.disclosure"):
        assert reg.clear("LINK", "link restored, routing verified") == 2
    assert [x.id for x in reg.active()] == ["ATTEST"]
    assert [(x.id, reason) for x, reason in reg.cleared()] == [
        ("LINK", "link restored, routing verified"), ("LINK", "link restored, routing verified")]
    assert "LINK" in caplog.text and "link restored" in caplog.text
    assert reg.clear("NEVER-RAISED", "nothing to clear") == 0


def test_raising_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    reg = DisclosureRegistry()
    with caplog.at_level(logging.INFO, logger="goliath_prep.disclosure"):
        reg.raise_(d("AMBER-ONE", "amber"))
        reg.raise_(d("INFO-ONE", "info"))
        reg.raise_(d("AMBER-ONE", "amber"))  # a repeat is not logged again
    records = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert [level for level, _ in records] == ["WARNING", "INFO"]
    assert "AMBER-ONE" in records[0][1] and "AMBER-ONE in effect" in records[0][1]


def test_bind_raises_every_disclosure_a_param_carries() -> None:
    reg = DisclosureRegistry()
    r_w = unmeasured_r_w()
    m_w = Param(15.0, "mm", "POLICY", "docs/prep/params.json m_w")
    rho = Param.derived(55.0, "mm", "rho = r_w + m_w", inputs=(r_w, m_w))
    assert reg.bind(rho) is rho
    assert [x.id for x in reg.active()] == ["DEFAULT_UNMEASURED:r_w"]
    reg.require_disclosed([rho, r_w, m_w])


def test_require_disclosed_detects_an_undisclosed_fallback() -> None:
    reg = DisclosureRegistry()
    r_w = unmeasured_r_w()
    rho = Param.derived(55.0, "mm", "rho = r_w + m_w", inputs=(r_w,))
    # A fallback whose disclosure never reached the registry being shown.
    with pytest.raises(UndisclosedFallbackError, match="DEFAULT_UNMEASURED:r_w") as excinfo:
        reg.require_disclosed([Param(15.0, "mm", "POLICY", "m_w"), rho])
    assert "rho = r_w + m_w" in str(excinfo.value)
    # Shown less severely than it was raised is not shown.
    reg.raise_(d("DEFAULT_UNMEASURED:r_w", "info", "r_w = 40 mm, not measured", "docs/prep/params.json r_w"))
    with pytest.raises(UndisclosedFallbackError):
        reg.require_disclosed([r_w])
    reg.bind(r_w)
    reg.require_disclosed([r_w, rho])


def test_clearing_a_carried_disclosure_is_detected() -> None:
    reg = DisclosureRegistry()
    note = d("OWNER_PROFILE:m_tower", "info", sticky=False)
    m_tower = reg.bind(Param(30.0, "mm", "OWNER_PROFILE", "machine.toml margins.m_tower", disclosures=(note,)))
    reg.require_disclosed([m_tower])
    reg.clear("OWNER_PROFILE:m_tower", "test")
    with pytest.raises(UndisclosedFallbackError):
        reg.require_disclosed([m_tower])


def test_require_disclosed_accepts_only_params() -> None:
    with pytest.raises(TypeError, match="Param"):
        DisclosureRegistry().require_disclosed([40.0])  # type: ignore[list-item]


def test_registry_is_safe_across_threads() -> None:
    reg = DisclosureRegistry()

    def work(n: int) -> None:
        for i in range(200):
            reg.raise_(d("T%d" % (i % 50), "amber" if n % 2 else "info"))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reg) == 50
    assert {x.severity for x in reg.active()} == {"amber"}
