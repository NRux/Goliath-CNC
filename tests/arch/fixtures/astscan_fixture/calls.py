# SPDX-License-Identifier: GPL-3.0-or-later
"""Call shapes for find_calls and find_references. Parsed by the tests, never run."""
import asyncio
import subprocess as sp
from os import *  # noqa: F403 - a star import: names from it cannot be resolved

import goliath_link
from goliath_link import GoliathClient as ReExported
from goliath_link.client import GoliathClient
from goliath_link.client import GoliathClient as Aliased

from .helpers import run_thing as rt  # REF: helper import exact


def exact_forms():
    goliath_link.client.GoliathClient.upload_gcode(None, b"")  # MARK: upload exact
    GoliathClient.upload_gcode(None, b"")  # MARK: upload exact
    Aliased.upload_gcode(None, b"")  # MARK: upload exact
    sp.run(["true"], check=True)  # MARK: run exact
    rt()  # MARK: helper exact


def name_only_forms(client, make_client):
    client.upload_gcode(b"")  # MARK: upload name
    make_client().upload_gcode(b"")  # MARK: upload name
    ReExported.upload_gcode(client, b"")  # MARK: upload name
    asyncio.run(None)  # MARK: run name
    system("true")  # MARK: system name  # noqa: F405


def calls_spread_over_lines(client):
    return (
        client  # MARK: upload name
        .upload_gcode(b"")
    )


class Holder:
    hook = staticmethod(lambda c: c.upload_gcode(b""))  # MARK: upload name

    def method(self):
        self._client.upload_gcode(b"")  # MARK: upload name


def references_that_are_not_calls(client):
    send_later = client.upload_gcode  # REF: upload reference name
    client.upload_gcode = None  # REF: upload store name
    return send_later


def dynamic_access_is_not_a_call(client):
    # find_calls cannot see this; the literal is found by find_string_literals.
    return getattr(client, "upload_gcode")  # noqa: B009


def not_matches(client):
    client.upload_gcode_later(b"")
    client.upload(b"")
    uploads = {"upload_gcode": 1}
    return uploads
