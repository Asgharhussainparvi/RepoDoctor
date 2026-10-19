"""Safe repository intake: zip extraction with zip-slip, zip-bomb and symlink defences."""
from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from repodoctor.sandbox.manager import EXCLUDED_NAMES

_DRIVE = re.compile(r"^[A-Za-z]:")
_SYMLINK = 0o120000
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
MAX_COMPONENT = 200
MAX_NAME = 800


class RepoError(Exception):
    """A repository cannot be accepted. Messages are safe to show in the UI."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class RepoLimits:
    max_files: int = 5000
    max_unpacked_bytes: int = 100_000_000


@dataclass(frozen=True)
class ExtractStats:
    file_count: int
    total_bytes: int


def _clean(name: str) -> str | None:
    """Normalised relative path, None to skip (VCS/cache dirs), RepoError if it is a path trick."""
    name = name.replace("\\", "/")  # Windows tools (Compress-Archive on PowerShell 5) write backslashes
    if "\x00" in name or name.startswith("/") or _DRIVE.match(name):
        raise RepoError("unsafe_archive", f"The archive contains an unsafe path: {name[:80]!r}")
    parts = [p for p in PurePosixPath(name).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise RepoError("unsafe_archive", f"The archive contains an unsafe path: {name[:80]!r}")
    if not parts or any(p in EXCLUDED_NAMES for p in parts[:-1]) or parts[-1] == ".git":
        return None
    _check_portable(parts)
    return "/".join(parts)


def _check_portable(parts: list[str]) -> None:
    """Reject names that cannot be extracted on Windows (or at all). The server may run on Windows even
    though the sandbox is Linux, so one odd name must be a clear 400, never a crash."""
    for part in parts:
        shown = part[:60]
        if len(part) > MAX_COMPONENT or sum(len(p) for p in parts) > MAX_NAME:
            raise RepoError("unsafe_archive", f"The archive contains a file name that is too long: {shown!r}")
        if _BAD_CHARS.search(part) or part.endswith((".", " ")) or _WINDOWS_DEVICE.match(part):
            raise RepoError("unsafe_archive", f"The archive contains a file name that is not valid on Windows: {shown!r}")


def extract_zip(source: BinaryIO, dest: Path, limits: RepoLimits | None = None) -> ExtractStats:
    """Extract a repository zip into dest (created). The caller deletes dest if this raises."""
    limits = limits or RepoLimits()
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise RepoError("invalid_zip", "That file is not a valid zip archive.") from exc

    kept: list[tuple[zipfile.ZipInfo, str]] = []
    with archive:
        for info in archive.infolist():
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == _SYMLINK:
                continue  # directories are implied; symlinks are never extracted
            if info.flag_bits & 0x1:
                raise RepoError("encrypted_zip", "Encrypted zip archives are not supported.")
            clean = _clean(info.filename)
            if clean is not None:
                kept.append((info, clean))
        if not kept:
            raise RepoError("empty_repository", "The archive contains no files.")
        if len(kept) > limits.max_files:
            raise RepoError("too_many_files", f"The archive has more than {limits.max_files} files.", 413)
        if sum(i.file_size for i, _ in kept) > limits.max_unpacked_bytes:
            raise RepoError("too_large", f"The archive unpacks to more than {limits.max_unpacked_bytes} bytes.", 413)

        tops = {c.split("/", 1)[0] for _, c in kept}
        strip = len(tops) == 1 and all("/" in c for _, c in kept)  # GitHub-style "repo-main/..." wrapper
        root = dest.resolve()
        root.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            for info, clean in kept:
                rel = clean.split("/", 1)[1] if strip else clean
                target = (root / rel).resolve()
                if root != target and root not in target.parents:
                    raise RepoError("unsafe_archive", "The archive tries to write outside the repository.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as out:
                    while chunk := src.read(65536):  # the declared size can lie: enforce while streaming
                        total += len(chunk)
                        if total > limits.max_unpacked_bytes:
                            raise RepoError("too_large",
                                            f"The archive unpacks to more than {limits.max_unpacked_bytes} bytes.", 413)
                        out.write(chunk)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
            raise RepoError("invalid_zip", "The archive is corrupt or uses an unsupported format.") from exc
        except OSError as exc:  # e.g. a name or path the file system refuses
            raise RepoError("unsafe_archive", "A file in the archive could not be extracted on this system.") from exc
    return ExtractStats(file_count=len(kept), total_bytes=total)


def list_repo_files(root: Path, limit: int = 1000) -> list[str]:
    """Repository files as sorted POSIX strings (plain string order, identical on every OS)."""
    files = [
        rel.as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        for rel in [path.relative_to(root)]
        if not any(part in EXCLUDED_NAMES for part in rel.parts)
    ]
    return sorted(files)[:limit]
