# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixture package for tests/arch/test_astscan.py.

The tests parse these files; nothing imports or runs them. Each line where a
test expects a match carries a marker comment that the test reads:

* ``# MARK: <query> exact|name``: find_calls(<query>) reports this line, with
  ``exact`` True or False; find_references(<query>) reports it as a call;
* ``# REF: <query> <kind> exact|name``: find_references(<query>) reports this
  line with that kind (it is not a call);
* ``# LIT: <kind>[,<kind>...]``: find_string_literals with the command pattern
  of test_astscan.py reports this line, once per kind listed.
"""
