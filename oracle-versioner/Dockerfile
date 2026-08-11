# Oracle DDL Versioning Agent (docs/architecture.md §18).
#
# Multi-stage: dependencies resolve in the builder, only the installed packages
# and the application land in the runtime stage. Thin-mode python-oracledb needs
# no Oracle Instant Client, so a slim base is sufficient and buys a much smaller
# attack surface.
#
# The base image is pinned by digest, not by tag (§18.3): `python:3.13-slim`
# moves, and an image rebuilt in six months must behave identically. To refresh
# it deliberately, resolve the new digest and update BOTH stages:
#     docker manifest inspect python:3.13-slim | head -1

FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# Only requirements.txt: the runtime image never carries pytest, ruff, or mypy.
COPY requirements.txt ./
RUN pip install --prefix=/install -r requirements.txt


FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS runtime

# Determinism (§18.3): locale and timezone must never influence normalized
# output, and Python must not write .pyc files into a read-only-ish layer.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    GIT_TERMINAL_PROMPT=0

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 agent

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=agent:agent src/ ./src/
COPY --chown=agent:agent config/ ./config/

# No secrets in any layer (§18.2): credentials arrive only through the
# environment, never through ARG or ENV defaults.
USER agent

# Fail the build if the application cannot even be imported.
RUN python -c "import src.main; print('import ok')"

# One synchronization, then exit.
ENTRYPOINT ["python", "-m", "src.main"]
