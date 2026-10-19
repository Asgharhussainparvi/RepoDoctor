"""CLI: python -m repodoctor.sandbox [check|build|cleanup]"""

from __future__ import annotations

import argparse

from .config import SandboxConfig
from .errors import SandboxError
from .manager import SandboxManager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m repodoctor.sandbox")
    parser.add_argument("command", choices=["check", "build", "cleanup"])
    args = parser.parse_args(argv)
    config = SandboxConfig.from_env()
    try:
        SandboxManager.check_docker(config)
        if args.command == "check":
            print("Docker: OK (Linux containers)")
            present = SandboxManager.image_exists(config)
            print(
                f"Image {config.image}: {'present' if present else 'MISSING (run: python -m repodoctor.sandbox build)'}"
            )
            return 0 if present else 1
        if args.command == "build":
            print(
                f"Building {config.image} (first build downloads the base image; can take a few minutes)..."
            )
            output = SandboxManager.build_image(config)
            print("\n".join(output.strip().splitlines()[-5:]))
            print("Build complete.")
            return 0
        removed = SandboxManager.cleanup_stale(config)
        print(f"Removed {removed} RepoDoctor container(s).")
        return 0
    except SandboxError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
