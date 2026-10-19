"""SandboxManager logic against a fake Docker. No Docker needed."""

import io
import tarfile

import pytest

from repodoctor.sandbox import manager as m
from repodoctor.sandbox.config import SandboxConfig
from repodoctor.sandbox.errors import (
    DockerUnavailableError,
    SandboxError,
    SandboxImageMissingError,
    SandboxNotRunningError,
    UploadRejectedError,
)
from repodoctor.sandbox.manager import (
    SandboxManager,
    build_repo_tar,
    is_timeout_exit,
    validate_argv,
)


def raw(rc=0, data=b"", timed_out=False, duration=0.1, truncated=False):
    return m._RawResult(rc, data, truncated, timed_out, duration)


class FakeDocker:
    def __init__(
        self,
        *,
        os_type="linux",
        image_present=True,
        run_rc=0,
        exec_rc=0,
        exec_data=b"",
        exec_timed_out=False,
        exec_duration=0.5,
        exec_raises=None,
    ):
        self.__dict__.update(locals())
        self.calls, self.inputs = [], []

    def __call__(self, cmd, *, input_bytes, timeout, max_bytes):
        if self.exec_raises and cmd[1] == "info":
            raise self.exec_raises
        args = list(cmd[1:])
        self.calls.append(args)
        self.inputs.append(input_bytes)
        verb = args[0]
        if verb == "info":
            return raw(0, f"{self.os_type}\n".encode())
        if verb == "image":
            return raw(0 if self.image_present else 1)
        if verb == "run":
            return raw(self.run_rc, b"cid\n" if self.run_rc == 0 else b"boom")
        if verb == "exec":
            return raw(
                None if self.exec_timed_out else self.exec_rc,
                self.exec_data,
                self.exec_timed_out,
                self.exec_duration,
            )
        if verb in ("kill", "rm", "container"):
            return raw(0)
        raise AssertionError(f"unexpected docker call: {args}")

    @property
    def verbs(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        f = FakeDocker(**kwargs)
        monkeypatch.setattr(m, "_run_process", f)
        return f

    return install


@pytest.fixture
def started(fake):
    f = fake()
    sb = SandboxManager(session_id="t1")
    sb.start()
    return sb, f


# ---- pure helpers ----
@pytest.mark.parametrize(
    ("rc", "duration", "expected"),
    [
        (124, 0.1, True),
        (137, 10.0, True),
        (137, 0.5, False),
        (1, 99, False),
        (0, 99, False),
        (None, 5, False),
    ],
)
def test_is_timeout_exit(rc, duration, expected):
    assert is_timeout_exit(rc, duration, 10) is expected


@pytest.mark.parametrize(
    "bad", ["ls", b"ls", [], [""], ["ok", 3], ["a\x00b"], ["x"] * 65, ["y" * 5000]]
)
def test_validate_argv_rejects(bad):
    with pytest.raises(ValueError):
        validate_argv(bad)


def test_validate_argv_accepts_list_and_tuple():
    assert validate_argv(["python", "-m", "pytest"]) == ("python", "-m", "pytest")
    assert validate_argv(("ls",)) == ("ls",)


@pytest.mark.parametrize("bad", ["", "has space", "a/b", "x" * 41, "semi;colon"])
def test_session_id_validated(bad):
    with pytest.raises(ValueError):
        SandboxManager(session_id=bad)


# ---- container security flags ----
def test_run_args_enforce_isolation():
    args = SandboxManager(session_id="t1").build_run_args()
    joined = " ".join(args)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in args
    assert "--read-only" in args
    assert "--memory 512m" in joined and "--memory-swap 512m" in joined
    assert "--pids-limit" in args and "--cpus" in args
    assert "size=" in joined  # tmpfs are size-capped


def test_run_args_never_widen_access():
    args = SandboxManager(session_id="t1").build_run_args()
    joined = " ".join(args).lower()
    for forbidden in (
        "--privileged",
        "--volume",
        "--mount",
        "docker.sock",
        "--network host",
        "--cap-add",
        "--pid host",
        "--device",
    ):
        assert forbidden not in joined
    assert "-v" not in args


# ---- lifecycle ----
def test_start_happy_path(fake):
    f = fake()
    sb = SandboxManager(session_id="t1")
    sb.start()
    assert f.verbs == ["info", "image", "run"]
    assert sb.container_name == "repodoctor-t1"


def test_start_reports_docker_unavailable(fake):
    fake(exec_raises=FileNotFoundError("docker"))
    with pytest.raises(DockerUnavailableError, match="Docker must be running"):
        SandboxManager(session_id="t1").start()


def test_start_rejects_windows_containers(fake):
    fake(os_type="windows")
    with pytest.raises(SandboxError, match="Linux containers"):
        SandboxManager(session_id="t1").start()


def test_start_requires_image_and_never_runs_container(fake):
    f = fake(image_present=False)
    with pytest.raises(SandboxImageMissingError, match="repodoctor.sandbox build"):
        SandboxManager(session_id="t1").start()
    assert "run" not in f.verbs


def test_start_failure_cleans_up(fake):
    f = fake(run_rc=125)
    with pytest.raises(SandboxError):
        SandboxManager(session_id="t1").start()
    assert f.verbs[-1] == "rm"


def test_exec_before_start_raises():
    with pytest.raises(SandboxNotRunningError):
        SandboxManager(session_id="t1").exec(["ls"])


def test_exec_wraps_command_with_in_container_timeout(started):
    sb, f = started
    result = sb.exec(["python", "-c", "print(1)"], timeout=10)
    assert f.calls[-1] == [
        "exec",
        "-w",
        "/workspace",
        "repodoctor-t1",
        "timeout",
        "-s",
        "TERM",
        "-k",
        "2",
        "10",
        "python",
        "-c",
        "print(1)",
    ]
    assert result.ok and result.argv == ("python", "-c", "print(1)")


def test_exec_nonzero_exit_is_reported_not_raised(started, fake):
    sb, f = started
    f.exec_rc, f.exec_data = 1, b"1 failed, 31 passed"
    result = sb.exec(["python", "-m", "pytest"])
    assert result.exit_code == 1 and not result.ok and "1 failed" in result.output


def test_exec_detects_timeout_exit_code(started):
    sb, f = started
    f.exec_rc = 124
    assert sb.exec(["sleep", "99"], timeout=5).timed_out


def test_exec_backstop_kills_sandbox(started):
    sb, f = started
    f.exec_timed_out = True
    result = sb.exec(["sleep", "99"], timeout=5)
    assert result.timed_out and result.exit_code is None
    assert "kill" in f.verbs
    with pytest.raises(SandboxNotRunningError):
        sb.exec(["ls"])


def test_exec_daemon_error_marks_sandbox_dead(started):
    sb, f = started
    f.exec_rc, f.exec_data = 1, b"Error response from daemon: container is not running"
    with pytest.raises(SandboxNotRunningError):
        sb.exec(["ls"])


@pytest.mark.parametrize("timeout", [0, -1, 301, 1.5, True, "5"])
def test_exec_rejects_bad_timeout(started, timeout):
    sb, _ = started
    with pytest.raises(ValueError):
        sb.exec(["ls"], timeout=timeout)


def test_exec_rejects_bad_argv(started):
    sb, _ = started
    with pytest.raises(ValueError):
        sb.exec("ls -la")


def test_destroy_is_idempotent(started):
    sb, f = started
    sb.destroy()
    sb.destroy()
    assert f.verbs.count("rm") == 1
    with pytest.raises(SandboxNotRunningError):
        sb.exec(["ls"])


def test_destroy_without_start_makes_no_docker_calls(fake):
    f = fake()
    SandboxManager(session_id="t1").destroy()
    assert f.calls == []


def test_context_manager_destroys_on_error(fake):
    f = fake()
    with pytest.raises(RuntimeError):
        with SandboxManager(session_id="t1"):
            raise RuntimeError("boom")
    assert f.verbs[-1] == "rm"


# ---- upload ----
def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "__pycache__").mkdir()
    (repo / "node_modules").mkdir()
    (repo / ".venv").mkdir()
    (repo / "a.py").write_text("print('a')\n")
    (repo / "pkg" / "b.py").write_text("x = 1\n")
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (repo / "node_modules" / "m.js").write_text("1")
    (repo / ".venv" / "pyvenv.cfg").write_text("home=x")
    return repo


def tar_names(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return sorted(tar.getnames())


def test_tar_excludes_vcs_caches_and_venvs(tmp_path):
    data, summary = build_repo_tar(make_repo(tmp_path), SandboxConfig())
    assert tar_names(data) == ["a.py", "pkg/b.py"]
    assert summary.file_count == 2


def test_tar_normalises_ownership_and_mode(tmp_path):
    data, _ = build_repo_tar(make_repo(tmp_path), SandboxConfig())
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            assert (member.uid, member.gid, member.uname) == (0, 0, "")
            assert member.mode in (0o644, 0o755)


def test_tar_skips_symlinks(tmp_path):
    repo = make_repo(tmp_path)
    try:
        (repo / "link.py").symlink_to(repo / "a.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    data, summary = build_repo_tar(repo, SandboxConfig())
    assert "link.py" not in tar_names(data) and summary.skipped_links == 1


def test_tar_rejects_empty_missing_and_oversized(tmp_path):
    empty = tmp_path / "empty"
    (empty / ".git").mkdir(parents=True)
    with pytest.raises(UploadRejectedError, match="no files"):
        build_repo_tar(empty, SandboxConfig())
    with pytest.raises(UploadRejectedError, match="Not a directory"):
        build_repo_tar(tmp_path / "nope", SandboxConfig())
    repo = make_repo(tmp_path)
    with pytest.raises(UploadRejectedError, match="more than"):
        build_repo_tar(repo, SandboxConfig(max_upload_files=1))
    with pytest.raises(UploadRejectedError, match="larger than"):
        build_repo_tar(repo, SandboxConfig(max_upload_bytes=3))


def test_upload_streams_tar_into_container(started, tmp_path):
    sb, f = started
    summary = sb.upload_repo(make_repo(tmp_path))
    call = f.calls[-1]
    assert call[:2] == ["exec", "-i"] and "tar" in call and "-C" in call
    assert tar_names(f.inputs[-1]) == ["a.py", "pkg/b.py"]
    assert summary.file_count == 2


def test_upload_failure_is_reported(started, tmp_path):
    sb, f = started
    f.exec_rc, f.exec_data = 2, b"tar: disk full"
    with pytest.raises(SandboxError, match="disk full"):
        sb.upload_repo(make_repo(tmp_path))


# ---- stdin (added in Phase 3) ----
def test_exec_with_stdin_adds_interactive_flag_and_pipes_bytes(started):
    sb, f = started
    sb.exec(["git", "apply", "-"], stdin=b"patch-bytes")
    assert f.calls[-1][:2] == ["exec", "-i"] and f.inputs[-1] == b"patch-bytes"
    assert f.calls[-1][-3:] == ["git", "apply", "-"]


def test_exec_without_stdin_has_no_interactive_flag(started):
    sb, f = started
    sb.exec(["ls"])
    assert "-i" not in f.calls[-1] and f.inputs[-1] is None


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("text", id="str"),
        pytest.param(bytearray(b"x"), id="bytearray"),
        # explicit id: pytest would otherwise put all 1,000,001 bytes into the test id,
        # which overflows Windows' 32,767-char limit on the PYTEST_CURRENT_TEST variable
        pytest.param(b"x" * 1_000_001, id="too-large"),
    ],
)
def test_exec_rejects_bad_stdin(started, bad):
    sb, _ = started
    with pytest.raises(ValueError):
        sb.exec(["cat"], stdin=bad)


def test_workspace_property():
    assert SandboxManager(session_id="t1").workspace == "/workspace"


def test_docker_unavailable_message_is_clean_and_detail_is_separate(fake):
    fake(exec_raises=FileNotFoundError("[Errno 2] No such file or directory: 'docker'"))
    with pytest.raises(DockerUnavailableError) as err:
        SandboxManager(session_id="t1").start()
    assert str(err.value) == "Sandbox unavailable. Docker must be running."
    assert "docker" in err.value.detail and "Errno" in err.value.detail
