"""Standalone MCP server exposing BAS MCBP_AI (/ai/v1) to MCP clients.

A thin stdio adapter over `mcbp_core` — see `.claude/skills/mcbp-mcp-server/SKILL.md` for the
build blueprint. This package holds no knowledge of BAS routes, fields, or response shapes.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _distribution_version

# The distribution is named `mcbp`, the import package `mcbp_mcp_server` — read the version from
# installed metadata rather than restating it here, so `pyproject.toml` stays the single source.
# MCP clients show this string in their connected-servers UI; an empty one makes releases
# indistinguishable there.
try:
    __version__ = _distribution_version("mcbp")
except PackageNotFoundError:  # source tree that was never installed (e.g. a bare checkout)
    __version__ = "0+unknown"

__all__ = ["__version__"]
