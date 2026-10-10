# RepoDoctor sandbox image. Hackathon-grade, see docs/security.md.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# Test dependencies are baked in so tests can run with --network none.
RUN pip install --no-cache-dir "pytest>=7.4,<10"

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
 && mkdir /workspace \
 && chown sandbox:sandbox /workspace

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
