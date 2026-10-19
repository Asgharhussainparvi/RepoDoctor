import io
import zipfile
from pathlib import Path

import pytest

from repodoctor.api.repos import RepoError, RepoLimits, extract_zip, list_repo_files


def make_zip(entries: dict[str, bytes | str], *, symlinks=(), dirs=()) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
        for name in dirs:
            z.writestr(name if name.endswith("/") else name + "/", "")
    buf.seek(0)
    return buf


def extract(entries, tmp_path, limits=None, **kw):
    dest = tmp_path / "out"
    stats = extract_zip(make_zip(entries, **kw), dest, limits)
    return dest, stats


def error_code(entries, tmp_path, limits=None, **kw):
    with pytest.raises(RepoError) as err:
        extract(entries, tmp_path, limits, **kw)
    return err.value.code


def test_extracts_files_and_reports_stats(tmp_path):
    dest, stats = extract({"app/a.py": "x = 1\n", "tests/test_a.py": "def test_a(): pass\n", "README.md": "hi"}, tmp_path)
    assert stats.file_count == 3 and stats.total_bytes == len("x = 1\n") + len("def test_a(): pass\n") + 2
    assert (dest / "app" / "a.py").read_text() == "x = 1\n"
    assert list_repo_files(dest) == ["README.md", "app/a.py", "tests/test_a.py"]


def test_github_style_wrapper_directory_is_stripped(tmp_path):
    dest, _ = extract({"repo-main/app/a.py": "1", "repo-main/README.md": "2"}, tmp_path)
    assert sorted(list_repo_files(dest)) == ["README.md", "app/a.py"]


def test_a_single_top_level_file_is_not_mistaken_for_a_wrapper(tmp_path):
    dest, _ = extract({"main.py": "1"}, tmp_path)
    assert list_repo_files(dest) == ["main.py"]


def test_backslash_paths_from_windows_zip_tools_are_normalised(tmp_path):
    dest, _ = extract({"main.py": "0", "app\\a.py": "1", "app\\sub\\b.py": "2"}, tmp_path)
    assert sorted(list_repo_files(dest)) == ["app/a.py", "app/sub/b.py", "main.py"]


def test_vcs_and_cache_directories_are_skipped(tmp_path):
    dest, stats = extract({"a.py": "1", ".git/config": "x", "__pycache__/a.pyc": "x", "node_modules/m.js": "x",
                           "pkg/.venv/pyvenv.cfg": "x"}, tmp_path)
    assert list_repo_files(dest) == ["a.py"] and stats.file_count == 1


def test_symlinks_are_never_extracted(tmp_path):
    dest, stats = extract({"a.py": "1"}, tmp_path, symlinks=["link.py"])
    assert list_repo_files(dest) == ["a.py"] and not (dest / "link.py").exists()


@pytest.mark.parametrize("name", ["../evil.py", "a/../../evil.py", "/etc/passwd", "C:/Windows/evil.py",
                                  "..\\evil.py", "a/..\\..\\evil.py"])
def test_path_tricks_are_rejected(tmp_path, name):
    assert error_code({"ok.py": "1", name: "x"}, tmp_path) == "unsafe_archive"
    assert not (tmp_path.parent / "evil.py").exists()


def test_nothing_is_written_outside_the_target(tmp_path):
    try:
        extract({"../escape.txt": "x"}, tmp_path)
    except RepoError:
        pass
    assert not list(tmp_path.glob("escape*")) and not (tmp_path.parent / "escape.txt").exists()


def test_empty_or_only_excluded_archives_are_rejected(tmp_path):
    assert error_code({}, tmp_path) == "empty_repository"
    assert error_code({".git/config": "x"}, tmp_path) == "empty_repository"


def test_not_a_zip(tmp_path):
    with pytest.raises(RepoError) as err:
        extract_zip(io.BytesIO(b"this is not a zip"), tmp_path / "o")
    assert err.value.code == "invalid_zip" and err.value.status == 400


def test_file_count_and_unpacked_size_limits(tmp_path):
    many = {f"f{i}.py": "x" for i in range(6)}
    assert error_code(many, tmp_path, RepoLimits(max_files=5)) == "too_many_files"
    assert error_code({"big.txt": "x" * 5000}, tmp_path, RepoLimits(max_unpacked_bytes=1000)) == "too_large"


def test_compression_bomb_is_rejected_by_its_declared_size(tmp_path):
    bomb = {"zeros.bin": b"\0" * 5_000_000}  # a few KB compressed, 5 MB unpacked
    assert error_code(bomb, tmp_path, RepoLimits(max_unpacked_bytes=1_000_000)) == "too_large"


def test_corrupt_entries_become_a_clear_error_not_a_crash(tmp_path):
    raw = bytearray(make_zip({"a.py": "x = 1\n" * 50}).getvalue())
    raw[40] ^= 0xFF  # flip a byte inside the compressed data
    with pytest.raises(RepoError) as err:
        extract_zip(io.BytesIO(bytes(raw)), tmp_path / "o")
    assert err.value.code == "invalid_zip" and err.value.status == 400


def test_an_archive_that_lies_about_its_size_is_refused(tmp_path):
    import repodoctor.api.repos as repos
    data = make_zip({"a.bin": b"\0" * 400_000})
    real = repos.zipfile.ZipFile

    class LyingZip(real):
        def infolist(self):
            infos = super().infolist()
            for info in infos:
                info.file_size = 10  # claims 10 bytes, actually 400 KB
            return infos

    repos.zipfile.ZipFile = LyingZip
    try:
        with pytest.raises(RepoError) as err:
            extract_zip(data, tmp_path / "o", RepoLimits(max_unpacked_bytes=1000))
    finally:
        repos.zipfile.ZipFile = real
    assert err.value.code in ("invalid_zip", "too_large")


def test_encrypted_archives_are_refused(tmp_path):
    buf = make_zip({"a.py": "1"})
    raw = bytearray(buf.getvalue())
    # set the "encrypted" flag bit in the central directory entry
    idx = raw.index(b"PK\x01\x02")
    raw[idx + 8] |= 0x1
    with pytest.raises(RepoError) as err:
        extract_zip(io.BytesIO(bytes(raw)), tmp_path / "o")
    assert err.value.code == "encrypted_zip"


def test_file_listing_order_is_plain_string_order_on_every_os(tmp_path):
    dest, _ = extract({"app/a.py": "1", "README.md": "2", "Zed.txt": "3", "alpha.txt": "4"}, tmp_path)
    assert list_repo_files(dest) == ["README.md", "Zed.txt", "alpha.txt", "app/a.py"]
    assert list_repo_files(dest, limit=2) == ["README.md", "Zed.txt"]


@pytest.mark.parametrize(
    "name",
    ["aux.py", "CON", "nul.txt", "com1.py", "LPT9.log", "a:b.py", "weird?.py", 'q"uote.py', "pipe|.py", "star*.py",
     "trailing.", "trailing ", "dir/aux/file.py", "tab\there.py", "x" * 300 + ".py"],
)
def test_names_that_cannot_be_extracted_everywhere_get_a_clear_400(tmp_path, name):
    assert error_code({"ok.py": "1", name: "x"}, tmp_path) == "unsafe_archive"


def test_ordinary_unusual_names_are_still_fine(tmp_path):
    dest, _ = extract({"main.py": "1", "my-file_v2.0.py": "2", "dir with space/a.py": "3", "naux.py": "4", "console.py": "5"}, tmp_path)
    assert "dir with space/a.py" in list_repo_files(dest) and "console.py" in list_repo_files(dest)


def test_a_filesystem_error_during_extraction_is_a_clear_400_not_a_crash(tmp_path, monkeypatch):
    real_open = open

    def refusing_open(path, mode="r", *args, **kwargs):
        if "w" in mode and str(path).endswith("a.py"):
            raise OSError(36, "File name too long")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refusing_open)
    with pytest.raises(RepoError) as err:
        extract_zip(make_zip({"a.py": "1"}), tmp_path / "o")
    assert err.value.code == "unsafe_archive" and err.value.status == 400
