# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the Goliath-CNC contributors.
"""The local interface's security rules (unit U01; the project design, section 8.2).

A local server that can start a 900 W job is a target for cross-site requests
and DNS rebinding from any page the laptop visits, and the laptop sits on the
robot's Wi-Fi, which a phone may join. Each rule here is a plain function or a
small class, so it can be tested on its own; :mod:`goliath_run.ui.server`
applies them to every request:

* **Loopback only** - the server binds ``127.0.0.1`` (:data:`LOOPBACK`) and
  nothing else; on Windows it holds the port exclusively
  (:func:`socket_options`), so another local program cannot bind it too.
* **Host** - :func:`check_host`: exactly ``127.0.0.1:<port>`` or
  ``localhost:<port>``. A DNS-rebinding page has its own host name in
  ``Host``, so it is refused before anything else happens.
* **Per-launch token** - :class:`Credentials`: a random session token per
  server, held by the browser as an ``HttpOnly; SameSite=Strict`` cookie. The
  browser gets it from a one-time link (``/launch?code=...``): each code
  works once, and the code is not the token, so the link left in the
  browser's history is worth nothing.
* **Origin on every POST** - :func:`check_origin`: the request's ``Origin``
  is this exact origin. Fetch metadata (:func:`check_fetch_site`) is checked
  too where a browser sends it, and request bodies are JSON only
  (:func:`check_json_content_type`), which no cross-site form can send.
* **Headers on every response** - :data:`SECURITY_HEADERS`, including the
  Content-Security-Policy :data:`CSP` (``default-src 'self'``, never
  ``'unsafe-inline'``: red-team C-09). No CORS header, ever.
* **One controlling tab** - :class:`ControlLease`: one tab holds the lease and
  may submit intents; the others are read-only and can explicitly take
  control.

A refused request gets a :class:`Refusal`: a status, a fixed message for the
response (it never echoes the request) and a detail for the log line.
"""
from __future__ import annotations

import hmac
import re
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CSP",
    "LOOPBACK",
    "SECURITY_HEADERS",
    "ControlLease",
    "Credentials",
    "LeaseGrant",
    "LeaseState",
    "Refusal",
    "SocketOptions",
    "check_fetch_site",
    "check_host",
    "check_json_content_type",
    "check_origin",
    "cookie_name",
    "cookie_values",
    "launch_code_from_query",
    "redact_request_line",
    "socket_options",
]

LOOPBACK = "127.0.0.1"
"""The only address the interface listens on (design 8.2: never ``0.0.0.0``)."""

HOST_NAMES = ("127.0.0.1", "localhost")
"""The names this origin may be reached by; ``Host`` must be one of them with the server's port."""

CSP = ("default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
"""The Content-Security-Policy of every response.

Design 8.2 sets ``default-src 'self'; img-src 'self' data:`` and forbids
``'unsafe-inline'``. The other directives only tighten it: no plugins, no
``<base>`` that could redirect relative URLs, forms post only here, and no
framing by another page (clickjacking an arming button)."""

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
)
"""Sent with every response, refusals and errors included. No ``Access-Control-*`` header is ever sent."""

_TOKEN_BYTES = 32  # 256 random bits; base64url gives 43 characters
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_PORT = re.compile(r"[1-9][0-9]{0,4}\Z")


@dataclass(frozen=True)
class Refusal:
    """Why a request is refused.

    ``message`` is the response body: fixed text that never repeats what the
    request sent. ``detail`` is for the log line and may name header values.
    """

    status: int
    message: str
    detail: str


# ---------------------------------------------------------------------------
# Host, Origin, fetch metadata, content type
# ---------------------------------------------------------------------------


def check_host(values: Sequence[str], port: int) -> Refusal | None:
    """None when ``values`` (every ``Host`` header of the request) is exactly one allowed host.

    Allowed: ``127.0.0.1:<port>`` and ``localhost:<port>``, the name compared
    without regard to ASCII case (host names are case-insensitive), the port
    exactly as the server's (no leading zero, no sign). No header, two
    headers, another name, another port, a missing port: refused (403). This
    is the defence against DNS rebinding: a page on ``evil.example`` that
    re-points its name at ``127.0.0.1`` still sends ``Host: evil.example``.
    """
    if len(values) != 1:
        return Refusal(403, "This request was refused: it must name this interface (127.0.0.1) as its host.",
                       "%d Host headers %r (exactly one is accepted)" % (len(values), list(values)))
    value = values[0]
    name, sep, port_text = value.rpartition(":")
    ok = (bool(sep) and value.isascii() and name.lower() in HOST_NAMES and bool(_PORT.match(port_text))
          and int(port_text) == port)
    if ok:
        return None
    return Refusal(403, "This request was refused: it must name this interface (127.0.0.1) as its host.",
                   "Host %r is not 127.0.0.1:%d or localhost:%d (DNS rebinding or a misdirected request)"
                   % (value, port, port))


def check_origin(values: Sequence[str], host: str) -> Refusal | None:
    """None when ``values`` (every ``Origin`` header) is exactly ``http://<host>``.

    ``host`` is the request's accepted ``Host`` value (see
    :func:`check_host`); its name is compared lower-case, as browsers write
    origins. Every browser sends ``Origin`` with a POST, so a missing one is
    refused, and so are ``null``, another scheme, host or port, and two
    headers.
    """
    expected = "http://" + host.lower()
    if len(values) == 1 and values[0] == expected:
        return None
    return Refusal(403, "This request was refused: it did not come from this interface's own page.",
                   "Origin %r is not %r" % (list(values), expected))


def check_fetch_site(values: Sequence[str]) -> Refusal | None:
    """None when ``Sec-Fetch-Site`` is absent or exactly ``same-origin``.

    Browsers that send fetch metadata say ``same-origin`` for this
    interface's own requests; ``same-site`` (another port of 127.0.0.1),
    ``cross-site`` and ``none`` are refused. Browsers that do not send it are
    still bound by the cookie and the Origin check.
    """
    if not values or (len(values) == 1 and values[0] == "same-origin"):
        return None
    return Refusal(403, "This request was refused: it did not come from this interface's own page.",
                   "Sec-Fetch-Site %r is not same-origin" % (list(values),))


def check_json_content_type(values: Sequence[str]) -> Refusal | None:
    """None when the body is declared as JSON (``application/json``, optionally ``; charset=utf-8``).

    A cross-site HTML form can only send form encodings or plain text, so
    requiring JSON keeps one more door shut.
    """
    if len(values) == 1:
        media, _, params = values[0].partition(";")
        params = params.strip().lower()
        if media.strip().lower() == "application/json" and params in ("", "charset=utf-8"):
            return None
    return Refusal(415, "This request was refused: the body must be JSON (Content-Type: application/json).",
                   "Content-Type %r is not application/json" % (list(values),))


# ---------------------------------------------------------------------------
# Cookie and one-time link
# ---------------------------------------------------------------------------


def cookie_name(port: int) -> str:
    """The session cookie's name for the server on ``port``.

    Browsers do not separate cookies by port, so two interfaces on one
    machine (two robots) would overwrite each other's cookie under one name.
    """
    return "goliath_ui_%d" % port


def cookie_values(headers: Sequence[str], name: str) -> list[str]:
    """Every value the ``Cookie`` headers give for ``name``, in order (a browser sends one)."""
    out: list[str] = []
    for header in headers:
        for item in header.split(";"):
            key, sep, value = item.strip().partition("=")
            if sep and key == name:
                out.append(value)
    return out


def launch_code_from_query(query: str) -> str | None:
    """The code of a one-time link's query (``code=<code>``, nothing else), or None."""
    key, sep, value = query.partition("=")
    if key != "code" or not sep or not _TOKEN.match(value):
        return None
    return value


def redact_request_line(line: str) -> str:
    """``line`` with any query replaced by ``<redacted>``, so a one-time code never reaches a log."""
    method, sep, rest = line.partition(" ")
    target, sep2, version = rest.partition(" ")
    if "?" in target:
        target = target.split("?", 1)[0] + "?<redacted>"
    elif "#" in target:
        target = target.split("#", 1)[0] + "#<redacted>"
    return method + sep + target + sep2 + version


class Credentials:
    """The per-launch session token and the one-time codes that hand it to a browser.

    The token is random per server (256 bits). It is never shown: a browser
    gets it as a cookie by opening a one-time link whose code is a separate
    random value. Each code works once (:meth:`redeem`); the command line can
    issue another (:meth:`issue_launch_code`) when the operator needs to sign
    in again, for example after closing the browser. Codes are compared in
    constant time.
    """

    def __init__(self, port: int) -> None:
        self.cookie_name = cookie_name(port)
        self._token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._codes: set[str] = set()
        self._lock = threading.Lock()

    def issue_launch_code(self) -> str:
        """A new one-time code (it stays valid until it is used)."""
        code = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._lock:
            self._codes.add(code)
        return code

    def redeem(self, code: str) -> bool:
        """True once per issued code: the code is used up."""
        with self._lock:
            for known in self._codes:
                if hmac.compare_digest(known.encode("ascii"), code.encode("utf-8", "surrogatepass")):
                    self._codes.discard(known)
                    return True
        return False

    def set_cookie_header(self) -> str:
        """The ``Set-Cookie`` value: host-only, whole site, script cannot read it, never sent cross-site.

        No ``Expires`` or ``Max-Age``: the cookie ends with the browser
        session, and the token ends with this server anyway.
        """
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (self.cookie_name, self._token)

    def check_cookie(self, headers: Sequence[str]) -> Refusal | None:
        """None when the ``Cookie`` headers carry this launch's token exactly once."""
        values = cookie_values(headers, self.cookie_name)
        if len(values) == 1 and hmac.compare_digest(values[0].encode("utf-8", "surrogatepass"),
                                                    self._token.encode("ascii")):
            return None
        if not values:
            detail = "no %s cookie" % self.cookie_name
        elif len(values) > 1:
            detail = "%d %s cookies (exactly one is accepted)" % (len(values), self.cookie_name)
        else:
            detail = "the %s cookie does not hold this launch's token" % self.cookie_name
        return Refusal(401, "Not signed in. Open the link goliath-run printed when it started the interface "
                            "(each link works once; goliath-run can print a new one).", detail)


# ---------------------------------------------------------------------------
# Socket options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SocketOptions:
    """How the listening socket is set up on a platform.

    ``reuse_address`` - ``SO_REUSEADDR``. Off everywhere: the port is random,
    so there is nothing to rebind quickly, and on Windows it would let another
    program bind the same port. ``exclusive_address`` - Windows'
    ``SO_EXCLUSIVEADDRUSE``: no other socket may bind the port while this
    server holds it.
    """

    reuse_address: bool
    exclusive_address: bool


def socket_options(os_name: str) -> SocketOptions:
    """The options for ``os.name`` ``os_name`` (``nt`` is Windows)."""
    return SocketOptions(reuse_address=False, exclusive_address=os_name == "nt")


# ---------------------------------------------------------------------------
# The control lease
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseGrant:
    """What the tab that acquires or takes control gets: the lease (a secret it sends back) and the epoch."""

    lease: str
    epoch: int

    def __repr__(self) -> str:
        return "LeaseGrant(lease=<hidden>, epoch=%d)" % self.epoch

    def to_json(self) -> dict[str, Any]:
        return {"lease": self.lease, "epoch": self.epoch}


@dataclass(frozen=True)
class LeaseState:
    """What every tab may know: the epoch (it grows with every change) and whether some tab holds control."""

    epoch: int
    held: bool

    def to_json(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "held": self.held}


class ControlLease:
    """One controlling tab; every other tab is read-only (design 8.2).

    A tab :meth:`acquire` s control when nobody holds it, or :meth:`take` s it
    explicitly from the tab that holds it (the old lease stops counting at
    once). The holder may :meth:`release` it. There is no timeout: a closed
    tab keeps the lease until another tab takes control, so control never
    moves without someone asking for it. Intents run through
    :meth:`while_holding`, under the same lock as :meth:`take`, so an intent
    is accepted only from the tab that holds control at that moment.

    Lease values are random secrets compared in constant time; listeners
    (:meth:`subscribe`) hear every change as a :class:`LeaseState`, which
    carries no secret.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holder: str | None = None
        self._epoch = 0
        self._listeners: list[Callable[[LeaseState], None]] = []
        self._listeners_lock = threading.Lock()

    def state(self) -> LeaseState:
        with self._lock:
            return LeaseState(self._epoch, self._holder is not None)

    def holds(self, lease: str | None) -> bool:
        """True when ``lease`` is the current holder's lease."""
        with self._lock:
            return self._matches(lease)

    def _matches(self, lease: str | None) -> bool:
        if self._holder is None or type(lease) is not str:
            return False
        return hmac.compare_digest(self._holder.encode("ascii"), lease.encode("utf-8", "surrogatepass"))

    def _grant(self) -> LeaseGrant:
        self._holder = secrets.token_urlsafe(_TOKEN_BYTES)
        self._epoch += 1
        return LeaseGrant(self._holder, self._epoch)

    def acquire(self, lease: str | None) -> LeaseGrant | None:
        """Control for a tab: a new grant when nobody holds it, the same grant for its holder, else None."""
        with self._lock:
            if self._holder is not None:
                if self._matches(lease):
                    return LeaseGrant(self._holder, self._epoch)
                return None
            grant = self._grant()
            state = LeaseState(self._epoch, True)
        self._notify(state)
        return grant

    def take(self) -> LeaseGrant:
        """Control for a tab, taken from whoever holds it (an explicit operator action)."""
        with self._lock:
            grant = self._grant()
            state = LeaseState(self._epoch, True)
        self._notify(state)
        return grant

    def release(self, lease: str | None) -> bool:
        """The holder gives control up; False (and no change) for any other lease."""
        with self._lock:
            if not self._matches(lease):
                return False
            self._holder = None
            self._epoch += 1
            state = LeaseState(self._epoch, False)
        self._notify(state)
        return True

    def while_holding(self, lease: str | None, action: Callable[[], None]) -> bool:
        """Run ``action`` if ``lease`` holds control, with no change of control meanwhile; False if it does not."""
        with self._lock:
            if not self._matches(lease):
                return False
            action()
            return True

    def subscribe(self, listener: Callable[[LeaseState], None]) -> Callable[[], None]:
        """Call ``listener`` after every change; returns the function that unsubscribes it."""
        with self._listeners_lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._listeners_lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, state: LeaseState) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener(state)
