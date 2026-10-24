"""python -m repodoctor.api  ->  serve the API on 127.0.0.1:8000 (local use only)."""

from __future__ import annotations

import ipaddress
import logging

import uvicorn

from .app import create_app
from .settings import ApiSettings


def _assert_loopback_bind(host: str) -> None:
    """Refuse network-facing binds until the API has authentication.

    RepoDoctor accepts repository uploads and can run untrusted project code in a
    sandbox. Host/Origin checks are not a substitute for authentication on a
    shared or public interface.
    """
    candidate = host.strip().lower()
    if candidate == "localhost":
        return
    # Accept bracketed IPv6 literals as well as the unbracketed form.
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(
            "RepoDoctor has no API authentication; REPODOCTOR_HOST must be a loopback address "
            "(127.0.0.1, ::1, or localhost). Do not expose it to a network."
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "RepoDoctor has no API authentication; REPODOCTOR_HOST must be a loopback address "
            "(127.0.0.1, ::1, or localhost). Do not expose it to a network."
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = ApiSettings.from_env()
    _assert_loopback_bind(settings.host)
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port, log_level="info"
    )


if __name__ == "__main__":
    main()
