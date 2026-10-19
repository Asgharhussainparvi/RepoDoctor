"""Phase 9: hostile code running inside the REAL Docker sandbox.

Every script here is something a malicious repository (or an approved malicious patch) could run. The assertions
are about CONTAINMENT: the host is untouched, limits hold, and the session ends honestly.

Run: python -m pytest -m docker tests/security/test_hostile_repos_docker.py -v
The parsing helpers are also unit-tested without Docker (class TestHelpers).
"""
from __future__ import annotations

import re
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from repodoctor.agent import AgentConfig, FakeModel, Orchestrator, SessionStore, State
from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager
from repodoctor.tools import Toolbox
from repodoctor.tools.edits import make_edit_diff

SENTINEL = "SENTINEL-do-not-leak-4711"
ALL_MANAGERS: list[SandboxManager] = []


# ===================================================================== pure helpers (tested below without Docker)
def parse_proc_status(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def parse_mounts(text: str) -> list[dict[str, str]]:
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            mounts.append({"source": parts[0], "target": parts[1], "fstype": parts[2], "options": parts[3]})
    return mounts


HOST_SHARE_TYPES = ("9p", "virtiofs", "cifs", "nfs", "vboxsf", "grpcfuse", "osxfs")


def host_shares(mounts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [m for m in mounts if m["fstype"].startswith("fuse") or m["fstype"].startswith(HOST_SHARE_TYPES)]


def marker(output: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=(.*)$", output, flags=re.MULTILINE)
    assert match, f"marker {name!r} missing from output:\n{output[-800:]}"
    return match.group(1).strip()


class TestHelpers:
    def test_proc_status_parsing(self):
        status = parse_proc_status("Name:\tpython\nCapEff:\t0000000000000000\nNoNewPrivs:\t1\nSeccomp:\t2\n")
        assert (status["CapEff"], status["NoNewPrivs"], status["Seccomp"]) == ("0000000000000000", "1", "2")

    def test_mount_parsing_and_host_share_detection(self):
        text = ("overlay / overlay ro,relatime 0 0\ntmpfs /workspace tmpfs rw,nosuid,size=262144k 0 0\n"
                "grpcfuse /host fuse.grpcfuse rw 0 0\nshare /mnt/c 9p rw 0 0\nproc /proc proc rw 0 0\n")
        mounts = parse_mounts(text)
        assert [m["target"] for m in host_shares(mounts)] == ["/host", "/mnt/c"]
        assert next(m for m in mounts if m["target"] == "/")["options"].startswith("ro")

    def test_marker_extraction(self):
        assert marker("noise\nWRITTEN=128\nmore", "WRITTEN") == "128"
        with pytest.raises(AssertionError):
            marker("nothing here", "WRITTEN")


# ===================================================================== fixtures
@pytest.fixture(scope="module")
def config():
    cfg = SandboxConfig.from_env()
    try:
        SandboxManager.check_docker(cfg)
    except SandboxError as exc:
        pytest.skip(f"Docker not available: {exc}")
    if not SandboxManager.image_exists(cfg):
        pytest.fail(f"Sandbox image {cfg.image!r} is missing. Run: python -m repodoctor.sandbox build")
    return cfg


@pytest.fixture
def sandbox(config):
    manager = SandboxManager(config)
    ALL_MANAGERS.append(manager)
    with manager:
        yield manager


BASE_FILES = {
    "app/__init__.py": "",
    "app/core.py": "def add(a, b):\n    return a + b\n",
    "tests/test_core.py": "from app.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "pytest.ini": "[pytest]\npythonpath = .\n",
}


def make_repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    for rel, text in {**BASE_FILES, **(extra or {})}.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return repo


@pytest.fixture
def hostile_toolbox(config, tmp_path):
    def build(extra: dict[str, str]) -> tuple[Toolbox, SandboxManager]:
        manager = SandboxManager(config)
        ALL_MANAGERS.append(manager)
        manager.start()
        request_cleanup.append(manager)
        toolbox = Toolbox(manager)
        loaded = toolbox.load_repository({"path": str(make_repo(tmp_path, extra))})
        assert loaded.ok, loaded.error
        return toolbox, manager

    request_cleanup: list[SandboxManager] = []
    yield build
    for manager in request_cleanup:
        manager.destroy()


def eventually_alive(sandbox: SandboxManager, attempts: int = 8) -> bool:
    """After a fork storm, killed children are zombies until init reaps them; give the sandbox a moment."""
    for _ in range(attempts):
        if sandbox.exec(["python", "-c", "print('alive')"]).output.strip() == "alive":
            return True
        time.sleep(0.5)
    return False


def script(sandbox: SandboxManager, code: str, timeout: int = 40):
    return sandbox.exec(["python", "-"], stdin=textwrap.dedent(code).encode(), timeout=timeout)


# ===================================================================== container hardening (verified from inside)
@pytest.mark.docker
class TestContainerHardening:
    def test_no_capabilities_no_new_privileges_default_seccomp_and_not_root(self, sandbox):
        out = script(sandbox, """
            import os
            print("EUID=%d" % os.geteuid())
            for name, fn in {"setuid0": lambda: os.setuid(0), "chown": lambda: os.chown("/workspace", 0, 0),
                             "shadow": lambda: open("/etc/shadow").read()}.items():
                try:
                    fn(); print("%s=ALLOWED" % name)
                except Exception as e:
                    print("%s=DENIED" % name)
            print(open("/proc/self/status").read())
        """).output
        status = parse_proc_status(out)
        assert marker(out, "EUID") == "10001"
        assert [marker(out, n) for n in ("setuid0", "chown", "shadow")] == ["DENIED"] * 3
        assert int(status["CapEff"], 16) == 0 and int(status["CapBnd"], 16) == 0, status["CapEff"]
        assert status["NoNewPrivs"] == "1"
        assert status["Seccomp"] == "2", "Docker's default seccomp filter should be active"

    def test_the_host_filesystem_and_docker_socket_are_not_reachable(self, sandbox):
        out = script(sandbox, """
            import os
            for p in ["/var/run/docker.sock", "/run/docker.sock", "/host", "/Users", "/mnt/c", "/mnt/host", "/run/desktop", "/c"]:
                print("EXISTS %s=%s" % (p, os.path.exists(p)))
            print("MNT=%s" % sorted(os.listdir("/mnt")))
            print(open("/proc/self/mounts").read())
        """).output
        assert "=True" not in "\n".join(l for l in out.splitlines() if l.startswith("EXISTS"))
        assert marker(out, "MNT") == "[]"
        mounts = parse_mounts(out)
        assert host_shares(mounts) == [], f"host file shares are mounted: {host_shares(mounts)}"
        assert not any("docker.sock" in m["source"] or "docker.sock" in m["target"] for m in mounts)

    def test_only_workspace_and_tmp_are_writable(self, sandbox):
        out = script(sandbox, """
            import os
            ok = lambda p: (open(p, "w").close(), os.remove(p))
            for name in ["/etc/x", "/usr/local/x", "/var/x", "/opt/x", "/root/x", "/proc/sys/kernel/hostname", "/sys/x", "/bin/x"]:
                try:
                    open(name, "w").close(); print("WRITE %s=ALLOWED" % name)
                except Exception:
                    print("WRITE %s=DENIED" % name)
            for name in ["/workspace/ok", "/tmp/ok"]:
                try:
                    ok(name); print("WRITE %s=ALLOWED" % name)
                except Exception as e:
                    print("WRITE %s=DENIED %s" % (name, e))
            print(open("/proc/self/mounts").read())
        """).output
        writes = dict(re.findall(r"^WRITE (\S+)=(\w+)", out, flags=re.MULTILINE))
        assert writes["/workspace/ok"] == writes["/tmp/ok"] == "ALLOWED"
        assert {p for p, v in writes.items() if v == "ALLOWED"} == {"/workspace/ok", "/tmp/ok"}
        root = next(m for m in parse_mounts(out) if m["target"] == "/")
        assert root["options"].split(",")[0] == "ro", f"root filesystem is not read-only: {root}"

    def test_network_is_unreachable_including_dns_and_cloud_metadata(self, sandbox):
        out = script(sandbox, """
            import os, socket, urllib.request
            def attempt(name, fn):
                try:
                    fn(); print("NET %s=REACHED" % name)
                except Exception as e:
                    print("NET %s=BLOCKED" % name)
            attempt("dns", lambda: socket.getaddrinfo("example.com", 80))
            attempt("tcp", lambda: socket.create_connection(("1.1.1.1", 53), timeout=3))
            attempt("metadata", lambda: urllib.request.urlopen("http://169.254.169.254/latest/meta-data/", timeout=3))
            attempt("localhost", lambda: socket.create_connection(("127.0.0.1", 9), timeout=2))
            print("IFACES=%s" % sorted(os.listdir("/sys/class/net")))
        """, timeout=60).output
        assert "REACHED" not in out, out
        assert set(re.findall(r"'(\w+)'", marker(out, "IFACES"))) <= {"lo"}

    def test_host_environment_variables_never_enter_the_container(self, sandbox, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", SENTINEL)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SENTINEL)
        out = script(sandbox, """
            import os
            print(dict(os.environ))
            for p in ["/proc/self/environ", "/proc/1/environ"]:
                try:
                    print(open(p, "rb").read())
                except Exception as e:
                    print("ERR", p)
        """).output
        assert SENTINEL not in out and "AWS_" not in out

    def test_a_fork_storm_is_stopped_by_the_process_limit(self, sandbox):
        out = script(sandbox, """
            import subprocess
            kids = []
            stopped = "never"
            try:
                for _ in range(1500):
                    kids.append(subprocess.Popen(["sleep", "25"]))
            except Exception as e:
                stopped = type(e).__name__
            print("SPAWNED=%d" % len(kids))
            print("STOPPED=%s" % stopped)
            for k in kids:
                k.kill()
        """, timeout=60).output
        spawned = int(marker(out, "SPAWNED"))
        assert spawned < 400, f"{spawned} processes were created; the pids limit (256) did not hold"
        assert marker(out, "STOPPED") != "never"
        assert eventually_alive(sandbox)

    def test_a_memory_bomb_is_killed_and_the_sandbox_survives(self, sandbox):
        result = script(sandbox, """
            chunks = []
            while True:
                chunks.append(b"x" * (50 * 1024 * 1024))
                print("MB=%d" % (len(chunks) * 50), flush=True)
        """, timeout=60)
        assert result.exit_code != 0 and not result.timed_out
        reached = [int(x) for x in re.findall(r"^MB=(\d+)$", result.output, flags=re.MULTILINE)]
        assert reached and max(reached) <= 600, f"allocated {max(reached)} MB despite the 512 MB limit"
        assert sandbox.exec(["python", "-c", "print('ok')"]).ok

    def test_filling_the_disk_is_capped_by_the_tmpfs_size(self, sandbox):
        result = script(sandbox, """
            written = 0
            try:
                with open("/workspace/fill.bin", "wb") as f:
                    while written < 600 * 1024 * 1024:
                        f.write(b"\\0" * (1024 * 1024)); f.flush(); written += 1024 * 1024
                print("ERR=none")
            except OSError as e:
                print("ERR=%d" % e.errno)
            print("WRITTEN=%d" % (written // (1024 * 1024)), flush=True)
        """, timeout=90)
        capped = "ERR=28" in result.output and int(marker(result.output, "WRITTEN")) <= 270
        assert capped or result.exit_code != 0, result.output[-300:]  # ENOSPC at the size cap, or killed by the memory limit
        assert "ERR=none" not in result.output

    def test_output_floods_are_capped_and_keep_the_tail(self, sandbox):
        result = script(sandbox, """
            import sys
            for _ in range(80):
                sys.stdout.write("x" * (1024 * 1024))
            print("\\nTHE-END")
        """, timeout=90)
        assert result.truncated and "THE-END" in result.output
        assert len(result.output.encode()) <= sandbox.config.max_output_bytes + 100

    def test_cpu_is_limited_to_one_cpu(self, sandbox):
        out = script(sandbox, """
            import os, subprocess, sys, time
            code = "import time\\nt = time.time()\\nwhile time.time() - t < 3: pass"
            start = time.time()
            for p in [subprocess.Popen([sys.executable, "-c", code]) for _ in range(4)]:
                p.wait()
            wall = time.time() - start
            t = os.times()
            print("RATIO=%.2f" % ((t.children_user + t.children_system) / wall))
        """, timeout=60).output
        assert float(marker(out, "RATIO")) <= 1.6, "four busy processes used more than the 1 CPU quota"

    def test_a_timeout_kills_the_process_tree_but_detached_processes_survive_until_the_sandbox_is_destroyed(self, sandbox):
        """Characterisation: a process that detaches itself (new session) escapes the timeout, but not the container."""
        script(sandbox, """
            import subprocess
            subprocess.Popen(["sleep", "300"], start_new_session=True)
            print("SPAWNED=1")
        """)
        alive = script(sandbox, """
            import os
            names = []
            for pid in filter(str.isdigit, os.listdir("/proc")):
                try:
                    names.append(open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\\0", b" ").decode())
                except Exception:
                    pass
            print("SLEEPERS=%d" % sum(1 for n in names if n.strip() == "sleep 300"))
        """).output
        assert int(marker(alive, "SLEEPERS")) == 1  # documented: survives the exec
        sandbox.destroy()
        assert not sandbox.container_present()  # ...but never survives the sandbox


# ===================================================================== hostile repositories, through the real tools
HANG = "def test_hang():\n    while True:\n        pass\n"
EXFIL = f'''import os, socket
import pytest

def test_probe():
    report = []
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2); report.append("NET-REACHED")
    except OSError:
        report.append("NET-BLOCKED")
    try:
        open("/etc/pwned", "w").close(); report.append("ETC-WRITTEN")
    except OSError:
        report.append("ETC-BLOCKED")
    needle = "{SENTINEL[:12]}" + "{SENTINEL[12:]}"
    report.append("ENV-HAS-SECRET=%s" % any(needle in v for v in os.environ.values()))
    pytest.fail(" ".join(report))
'''
MEMORY_BOMB = "def test_bomb():\n    data = []\n    while True:\n        data.append(b'x' * (50 * 1024 * 1024))\n"
FLOOD = "import sys\n\n\ndef test_flood():\n    for _ in range(20):\n        sys.stderr.write('x' * (1024 * 1024))\n    assert False, 'after the flood'\n"
FORKS = ("import subprocess\n\n\ndef test_forks():\n    kids = []\n    try:\n        for _ in range(1200):\n"
         "            kids.append(subprocess.Popen(['sleep', '20']))\n    except OSError:\n        pass\n    count = len(kids)\n"
         "    for k in kids:\n        k.kill()\n        k.wait()\n    assert count < 400\n")
WIPE = "import os, shutil\n\n\ndef test_wipe():\n    for name in os.listdir('/workspace'):\n        p = os.path.join('/workspace', name)\n        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)\n"
LINKS = "import os\n\n\ndef test_links():\n    os.symlink('/etc/passwd', '/workspace/link_passwd')\n    os.symlink('/proc/self/environ', '/workspace/link_env')\n    os.symlink('/', '/workspace/link_root')\n"


@pytest.mark.docker
class TestHostileRepositories:
    def test_an_infinite_loop_is_reported_as_a_timeout(self, hostile_toolbox):
        toolbox, _ = hostile_toolbox({"tests/test_evil.py": HANG})
        started = time.monotonic()
        result = toolbox.run_tests({"timeout_seconds": 5})
        assert result.status == "timeout" and result.summary == "Test execution exceeded the allowed time."
        assert time.monotonic() - started < 40

    def test_network_writes_and_secrets_are_all_blocked_from_inside_a_test(self, hostile_toolbox, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", SENTINEL)
        toolbox, _ = hostile_toolbox({"tests/test_evil.py": EXFIL})
        result = toolbox.run_tests()
        text = result.output_excerpt
        assert "NET-BLOCKED" in text and "ETC-BLOCKED" in text and "ENV-HAS-SECRET=False" in text, text[-600:]
        assert "NET-REACHED" not in text and "ETC-WRITTEN" not in text and SENTINEL not in text.replace("ENV-HAS-SECRET", "")

    def test_a_memory_bomb_ends_as_an_unusable_run_never_a_pass(self, hostile_toolbox):
        toolbox, manager = hostile_toolbox({"tests/test_evil.py": MEMORY_BOMB})
        result = toolbox.run_tests({"timeout_seconds": 60})
        assert result.status in ("unparseable", "error", "timeout") and result.status != "passed"
        assert manager.exec(["python", "-c", "print('ok')"]).ok  # the sandbox itself survived

    def test_an_output_flood_keeps_the_summary_line_so_the_result_stays_correct(self, hostile_toolbox):
        toolbox, _ = hostile_toolbox({"tests/test_evil.py": FLOOD})
        result = toolbox.run_tests({"timeout_seconds": 90})
        assert result.output_truncated and result.status == "failed"
        assert (result.counts.failed, result.counts.passed) == (1, 1)

    def test_a_fork_storm_inside_a_test_is_contained(self, hostile_toolbox):
        toolbox, manager = hostile_toolbox({"tests/test_evil.py": FORKS})
        result = toolbox.run_tests({"timeout_seconds": 60})
        assert result.status == "passed", result.output_excerpt[-400:]  # the assertion inside the test checks the cap
        assert eventually_alive(manager)

    def test_deleting_the_whole_workspace_breaks_the_session_cleanly_not_the_host(self, hostile_toolbox):
        toolbox, _ = hostile_toolbox({"tests/test_evil.py": WIPE})
        toolbox.run_tests()
        for result in (toolbox.git_diff(), toolbox.reset_changes(), toolbox.list_files()):
            assert result.ok in (True, False)  # structured result, never an exception
        assert toolbox.run_tests().status != "passed"

    def test_symlinks_created_at_runtime_cannot_be_used_to_read_host_or_container_secrets(self, hostile_toolbox):
        toolbox, _ = hostile_toolbox({"tests/test_evil.py": LINKS})
        toolbox.run_tests()
        for path in ("link_passwd", "link_env", "link_root/etc/passwd", "link_root/etc/shadow"):
            for tool in ("read_file", "read_file_complete"):
                out = getattr(toolbox, tool)({"path": path})
                assert not out.ok and out.error_code == "invalid_path", (tool, path, out.error_code)
        found = toolbox.search_code({"pattern": "root:x:0:0"})
        assert not any("root:x" in m.text for m in found.matches)

    def test_odd_repository_metadata_is_harmless(self, hostile_toolbox):
        toolbox, _ = hostile_toolbox({
            ".gitattributes": "* filter=pwn diff=pwn\n*.py ident\n", ".gitmodules": "[submodule \"x\"]\n\tpath = x\n\turl = http://evil.example/x\n",
            "-rf": "dash file\n", "weird name with spaces.txt": "x\n", "unicode_\u00e9\u4e2d.txt": "x\n",
        })
        listed = toolbox.list_files({"max_files": 100}).files
        assert "-rf" in listed and "weird name with spaces.txt" in listed
        assert toolbox.read_file({"path": "-rf"}).error_code == "invalid_path"  # leading dash is refused outright
        assert toolbox.run_tests().status == "passed"

    def test_an_approved_malicious_patch_only_ever_runs_inside_the_sandbox(self, hostile_toolbox):
        toolbox, manager = hostile_toolbox({})
        host_marker = Path(tempfile.gettempdir()) / "repodoctor-inside-marker"
        host_marker.unlink(missing_ok=True)
        core = toolbox.read_file_complete({"path": "app/core.py"})
        evil_new = ("import socket\ntry:\n    open('/tmp/repodoctor-inside-marker', 'w').write('x')\n"
                    "    socket.create_connection(('1.1.1.1', 53), timeout=1)\n    open('/etc/pwned', 'w')\nexcept OSError:\n    pass\n\n\ndef add(a, b):")
        diff = make_edit_diff("app/core.py", core.content + "\n", "def add(a, b):", evil_new)
        checked = toolbox.check_patch({"diff": diff})
        assert checked.ok, checked.error  # the policy cannot judge intent: this is why a human approves
        toolbox.register_approval(checked.sha256)
        assert toolbox.apply_patch({"diff": diff}).ok
        assert toolbox.run_tests().status == "passed"
        inside = manager.exec(["python", "-c", "import os; print(os.path.exists('/tmp/repodoctor-inside-marker'), os.path.exists('/etc/pwned'))"])
        assert inside.output.strip() == "True False"  # it ran, inside the container, and could not write outside it
        assert not host_marker.exists()  # nothing reached the host


# ===================================================================== whole sessions against hostile repositories
class Managers:
    def __init__(self, config):
        self.config, self.created = config, []

    def __call__(self):
        manager = SandboxManager(self.config)
        self.created.append(manager)
        ALL_MANAGERS.append(manager)
        return manager


@pytest.mark.docker
class TestHostileSessions:
    def run_session(self, config, tmp_path, extra, **agent):
        managers = Managers(config)
        orch = Orchestrator(SessionStore(), FakeModel(["unused"]), managers, config=AgentConfig(**agent))
        sid = orch.create_session(make_repo(tmp_path, extra))
        started = time.monotonic()
        snap = orch.start(sid)
        return snap, managers, time.monotonic() - started

    def test_a_repository_whose_test_never_finishes_ends_in_a_timeout_and_cleans_up(self, config, tmp_path):
        snap, managers, seconds = self.run_session(config, tmp_path, {"tests/test_evil.py": HANG}, test_timeout_seconds=5)
        assert snap.state is State.FAILED and snap.error_code == "test_timeout" and snap.ui_state == "timeout"
        assert snap.message == "Test execution exceeded the allowed time." and seconds < 90
        assert not managers.created[0].container_present()

    def test_a_memory_bomb_repository_fails_honestly_and_cleans_up(self, config, tmp_path):
        snap, managers, _ = self.run_session(config, tmp_path, {"tests/test_evil.py": MEMORY_BOMB}, test_timeout_seconds=60)
        assert snap.state is State.FAILED and snap.error_code in ("tests_unusable", "test_timeout")
        assert "DEBUGGING COMPLETE" not in snap.report.headline
        assert not managers.created[0].container_present()


# ===================================================================== last: nothing is left behind
@pytest.mark.docker
def test_no_container_from_this_module_is_left_behind(config):
    leftovers = [m.container_name for m in ALL_MANAGERS if m.container_present()]
    assert leftovers == [], f"containers still exist: {leftovers}"
