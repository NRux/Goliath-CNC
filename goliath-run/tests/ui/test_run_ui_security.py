# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The local interface's security rules as plain functions (unit U01; the project design, section 8.2).

The server tests exercise these over real sockets; here each rule is checked
on its own, with property tests where the space of inputs is large: a header
check must accept exactly the allowed values and nothing that merely looks
like them.
"""
from __future__ import annotations

import re
import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from goliath_run.ui import security as S

PORT = 48123
HOSTS = ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

_header_text = st.text(st.characters(codec="latin-1", exclude_characters="\r\n"), max_size=40)


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


def test_host_accepts_exactly_the_two_names_of_this_origin() -> None:
    for host in HOSTS + ("LocalHost:%d" % PORT,):
        assert S.check_host([host], PORT) is None, host
    for values in ([], [HOSTS[0], HOSTS[0]], [HOSTS[0], HOSTS[1]]):
        r = S.check_host(values, PORT)
        assert r is not None and r.status == 403, values
    r = S.check_host(["evil.example:%d" % PORT], PORT)
    assert r is not None and r.status == 403
    assert "evil.example" not in r.message  # the response never echoes the request
    assert "evil.example" in r.detail  # the log line names it


@settings(max_examples=500)
@given(value=st.one_of(_header_text, st.sampled_from(HOSTS).flatmap(
    lambda h: st.tuples(_header_text, _header_text).map(lambda ab: ab[0] + h + ab[1]))))
def test_host_property_nothing_else_passes(value: str) -> None:
    accepted = S.check_host([value], PORT) is None
    assert accepted == (value.lower() in HOSTS), value


@settings(max_examples=200)
@given(port=st.integers(min_value=1, max_value=65535), other=st.integers(min_value=0, max_value=99999))
def test_host_port_must_be_this_port(port: int, other: int) -> None:
    assert S.check_host(["127.0.0.1:%d" % port], port) is None
    assert (S.check_host(["127.0.0.1:%d" % other], port) is None) == (other == port)


# ---------------------------------------------------------------------------
# Origin, fetch metadata, content type
# ---------------------------------------------------------------------------


def test_origin_must_be_this_exact_origin() -> None:
    assert S.check_origin(["http://127.0.0.1:%d" % PORT], HOSTS[0]) is None
    assert S.check_origin(["http://localhost:%d" % PORT], HOSTS[1]) is None
    assert S.check_origin(["http://localhost:%d" % PORT], "LOCALHOST:%d" % PORT) is None
    for values in ([], ["null"], ["http://localhost:%d" % PORT], ["http://127.0.0.1:%d" % PORT] * 2,
                   ["https://127.0.0.1:%d" % PORT], ["http://127.0.0.1:%d/" % PORT]):
        r = S.check_origin(values, HOSTS[0])
        assert r is not None and r.status == 403, values


@settings(max_examples=500)
@given(value=_header_text)
def test_origin_property_nothing_else_passes(value: str) -> None:
    assert (S.check_origin([value], HOSTS[0]) is None) == (value == "http://" + HOSTS[0])


def test_fetch_site() -> None:
    assert S.check_fetch_site([]) is None  # older browsers send none; Origin and the cookie still apply
    assert S.check_fetch_site(["same-origin"]) is None
    for values in (["same-site"], ["cross-site"], ["none"], [""], ["same-origin", "same-origin"], ["SAME-ORIGIN"]):
        r = S.check_fetch_site(values)
        assert r is not None and r.status == 403, values


def test_json_content_type() -> None:
    for ok in ("application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"):
        assert S.check_json_content_type([ok]) is None, ok
    for values in ([], ["text/plain"], ["application/json", "application/json"], ["application/jsonx"],
                   ["application/json; charset=latin-1"], ["multipart/form-data"]):
        r = S.check_json_content_type(values)
        assert r is not None and r.status == 415, values


# ---------------------------------------------------------------------------
# Cookie and one-time link
# ---------------------------------------------------------------------------


def test_cookie_values() -> None:
    assert S.cookie_values(["a=1; goliath_ui_1=xyz; b=2"], "goliath_ui_1") == ["xyz"]
    assert S.cookie_values(["goliath_ui_1=x", "goliath_ui_1=y"], "goliath_ui_1") == ["x", "y"]
    assert S.cookie_values(["goliath_ui_10=x"], "goliath_ui_1") == []
    assert S.cookie_values(["goliath_ui_1"], "goliath_ui_1") == []
    assert S.cookie_values([], "goliath_ui_1") == []


_token_chars = st.text(st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"),
                       min_size=1, max_size=50)


@given(others=st.lists(st.tuples(_token_chars, _token_chars), max_size=5), token=_token_chars)
def test_cookie_values_property(others: list[tuple[str, str]], token: str) -> None:
    name = S.cookie_name(PORT)
    pairs = [(k, v) for k, v in others if k != name] + [(name, token)]
    header = "; ".join("%s=%s" % kv for kv in pairs)
    assert S.cookie_values([header], name) == [token]


def test_credentials_one_time_codes_and_cookie() -> None:
    cred = S.Credentials(PORT)
    assert cred.cookie_name == "goliath_ui_%d" % PORT
    code = cred.issue_launch_code()
    other = cred.issue_launch_code()
    assert code != other and len(code) >= 40
    assert cred.redeem(code) and not cred.redeem(code)
    assert not cred.redeem("x" * len(code)) and not cred.redeem("")
    assert cred.redeem(other)
    header = cred.set_cookie_header()
    m = re.fullmatch(r"goliath_ui_%d=([A-Za-z0-9_-]{40,}); Path=/; HttpOnly; SameSite=Strict" % PORT, header)
    assert m, header
    token = m.group(1)
    assert token not in (code, other)
    assert cred.check_cookie(["%s=%s" % (cred.cookie_name, token)]) is None
    for bad in ([], ["%s=%s" % (cred.cookie_name, token[:-1])], ["%s=%s; %s=%s" % (cred.cookie_name, token,
                                                                                   cred.cookie_name, token)]):
        r = cred.check_cookie(bad)
        assert r is not None and r.status == 401, bad
    assert S.Credentials(PORT).set_cookie_header() != header  # per launch


def test_launch_code_from_query() -> None:
    assert S.launch_code_from_query("code=abc_-9") == "abc_-9"
    for q in ("", "code=", "code=a&code=b", "code=a&x=1", "x=a", "CODE=a", "code=a%00", "code=a b", "code"):
        assert S.launch_code_from_query(q) is None, q


def test_request_line_redacted() -> None:
    assert S.redact_request_line("GET /launch?code=secret HTTP/1.1") == "GET /launch?<redacted> HTTP/1.1"
    assert S.redact_request_line("GET /api/snapshot HTTP/1.1") == "GET /api/snapshot HTTP/1.1"
    assert "secret" not in S.redact_request_line("GET /x?code=secret")
    assert "secret" not in S.redact_request_line("GET /launch?code=secret#x HTTP/1.1")


def test_csp_constant_is_strict() -> None:
    directives = dict(d.strip().split(" ", 1) for d in S.CSP.split(";"))
    assert directives["default-src"] == "'self'"
    assert directives["img-src"] == "'self' data:"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    for name, _value in S.SECURITY_HEADERS:
        assert not name.lower().startswith("access-control-")


# ---------------------------------------------------------------------------
# The control lease
# ---------------------------------------------------------------------------


def test_lease_basics() -> None:
    lease = S.ControlLease()
    seen: list[S.LeaseState] = []
    unsubscribe = lease.subscribe(seen.append)
    assert lease.state() == S.LeaseState(epoch=0, held=False)
    g = lease.acquire(None)
    assert g is not None and lease.holds(g.lease) and g.epoch == 1
    assert lease.acquire(None) is None
    assert lease.acquire(g.lease) == g  # the holder asking again
    assert not lease.holds("x" * 43) and not lease.holds(None)
    t = lease.take()
    assert t.lease != g.lease and t.epoch == 2 and not lease.holds(g.lease)
    assert not lease.release(g.lease)
    assert lease.release(t.lease) and lease.state() == S.LeaseState(epoch=3, held=False)
    assert seen == [S.LeaseState(1, True), S.LeaseState(2, True), S.LeaseState(3, False)]
    unsubscribe()
    lease.acquire(None)
    assert len(seen) == 3


def test_while_holding_runs_only_for_the_holder() -> None:
    lease = S.ControlLease()
    g = lease.acquire(None)
    assert g is not None
    ran: list[str] = []
    assert lease.while_holding(g.lease, lambda: ran.append("a"))
    assert not lease.while_holding("nope", lambda: ran.append("b"))
    assert not lease.while_holding(None, lambda: ran.append("c"))
    assert ran == ["a"]


def test_take_waits_for_a_running_submit() -> None:
    lease = S.ControlLease()
    g = lease.acquire(None)
    assert g is not None
    inside = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def slow() -> None:
        inside.set()
        release.wait(5)
        order.append("submit")

    t = threading.Thread(target=lambda: lease.while_holding(g.lease, slow))
    t.start()
    inside.wait(5)
    taker = threading.Thread(target=lambda: (lease.take(), order.append("take")))
    taker.start()
    taker.join(0.2)
    assert order == []  # the take waits for the submit that started under the old lease
    release.set()
    t.join(5)
    taker.join(5)
    assert order == ["submit", "take"]


_ops = st.lists(st.tuples(st.sampled_from(["acquire", "acquire-own", "take", "release", "release-other"]),
                          st.integers(min_value=0, max_value=5)), max_size=40)


@settings(max_examples=200)
@given(ops=_ops)
def test_lease_property_one_holder_and_monotonic_epoch(ops: list[tuple[str, int]]) -> None:
    lease = S.ControlLease()
    grants: list[str] = []
    epochs = [lease.state().epoch]
    for op, i in ops:
        if op == "acquire":
            g = lease.acquire(None)
            if g is not None:
                grants.append(g.lease)
        elif op == "acquire-own" and grants:
            # The holder asking again keeps its lease; a stale lease when nobody holds control gets a new one.
            g = lease.acquire(grants[i % len(grants)])
            if g is not None and g.lease not in grants:
                grants.append(g.lease)
        elif op == "take":
            grants.append(lease.take().lease)
        elif op == "release" and grants:
            lease.release(grants[-1])
        elif op == "release-other" and grants:
            lease.release(grants[i % len(grants)])
        holders = [x for x in grants if lease.holds(x)]
        assert len(holders) <= 1
        assert lease.state().held == bool(holders)
        if holders:
            assert holders == [grants[-1]]  # only the newest grant can still hold
        epochs.append(lease.state().epoch)
    assert epochs == sorted(epochs)
    assert len(set(grants)) == len(grants)


def test_lease_grant_and_state_json() -> None:
    lease = S.ControlLease()
    g = lease.acquire(None)
    assert g is not None
    assert g.to_json() == {"lease": g.lease, "epoch": 1}
    assert lease.state().to_json() == {"epoch": 1, "held": True}
    assert g.lease not in repr(lease.state())


@pytest.mark.parametrize("name", ["nt", "posix", "java"])
def test_socket_options_known_platforms(name: str) -> None:
    opts = S.socket_options(name)
    assert opts.reuse_address is False
    assert opts.exclusive_address is (name == "nt")
