"""Turn a model's small edit (old text -> new text) into an exact unified diff. Pure functions.

Language models are unreliable at writing diff syntax (line numbers, context). They are good
at saying "replace this snippet with that one". So the model proposes an edit and WE generate
the diff from the real file contents; the patch policy and approval gate still apply to the result.
"""

from __future__ import annotations

import difflib


class EditError(Exception):
    """The edit cannot be applied to the file. `code` is machine-readable; the message is for the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def split_keepends(text: str) -> list[str]:
    """Split on '\\n' only (keeping it), so '\\r' and other separators stay inside their line."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def detect_eol(content: str) -> str:
    return "\r\n" if "\r\n" in content else "\n"


def _fuzzy_replace(lf: str, old: str, new: str) -> str | None:
    """Match ignoring trailing whitespace per line (NOT indentation). Unique match or None."""
    file_lines = lf.split("\n")
    old_lines = old.rstrip("\n").split("\n")
    wanted = [line.rstrip() for line in old_lines]
    hits = [
        i
        for i in range(len(file_lines) - len(old_lines) + 1)
        if [line.rstrip() for line in file_lines[i : i + len(old_lines)]] == wanted
    ]
    if len(hits) != 1:
        return None
    i = hits[0]
    return "\n".join(
        file_lines[:i] + new.rstrip("\n").split("\n") + file_lines[i + len(old_lines) :]
    )


def apply_edit(content: str, old_text: str, new_text: str) -> str:
    """Replace exactly one occurrence of old_text. Preserves the file's line-ending style."""
    eol = detect_eol(content)
    lf = content.replace("\r\n", "\n")
    old = old_text.replace("\r\n", "\n")
    new = new_text.replace("\r\n", "\n")
    if not old.strip():
        raise EditError(
            "empty_old_text",
            "old_text is empty. Copy the exact lines you want to replace.",
        )
    if old == new:
        raise EditError(
            "no_change", "old_text and new_text are identical, so nothing would change."
        )
    count = lf.count(old)
    if count > 1:
        raise EditError(
            "ambiguous_match",
            f"old_text appears {count} times. Include more surrounding lines so it matches exactly once.",
        )
    if count == 1:
        updated = lf.replace(old, new, 1)
    else:
        updated = _fuzzy_replace(lf, old, new)
        if updated is None:
            raise EditError(
                "no_match",
                "old_text was not found in the file. Copy it exactly, including indentation.",
            )
    if updated == lf:
        raise EditError("no_change", "The edit would not change the file.")
    return updated.replace("\n", "\r\n") if eol == "\r\n" else updated


def build_diff(path: str, old_content: str, new_content: str) -> str:
    """Unified diff with a/ and b/ prefixes, valid for `git apply` (handles missing final newline)."""
    diff = difflib.unified_diff(
        split_keepends(old_content),
        split_keepends(new_content),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    out: list[str] = []
    for line in diff:
        out.append(line)
        if (
            line[:1] in (" ", "+", "-")
            and not line.startswith(("--- ", "+++ "))
            and not line.endswith("\n")
        ):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


def make_edit_diff(path: str, content: str, old_text: str, new_text: str) -> str:
    return build_diff(path, content, apply_edit(content, old_text, new_text))
