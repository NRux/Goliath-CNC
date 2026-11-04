# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures for the repository-level architecture checks.

Python 3.9 compatible: the repository-level suite runs on 3.9.
"""
from __future__ import annotations

import pytest

from repo_git import Git, open_git


@pytest.fixture(scope="session")
def git() -> Git:
    """git in this repository's checkout; skips (or fails, see repo_git) without one."""
    return open_git()
