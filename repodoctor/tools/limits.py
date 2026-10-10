"""Limits applied to every tool. One place to tune them."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolLimits:
    max_tool_calls: int = 25  # agent-facing calls per session
    tool_timeout_seconds: int = 30  # list/read/search/git helpers
    max_list_files: int = 500
    max_read_lines: int = 400
    max_line_chars: int = 2000
    max_file_bytes: int = 1_000_000
    max_search_results: int = 50
    max_search_line_chars: int = 300
    max_patch_bytes: int = 100_000
    max_patch_files: int = 5
    max_patch_changed_lines: int = 200
    allow_test_edits: bool = False  # patches may not touch tests or test config
    output_excerpt_chars: int = 6000
