"""Standalone MCP server exposing BAS MCBP_AI (/ai/v1) to MCP clients.

A thin stdio adapter over `mcbp_core` — see `.claude/skills/mcbp-mcp-server/SKILL.md` for the
build blueprint. This package holds no knowledge of BAS routes, fields, or response shapes.
"""
from __future__ import annotations
