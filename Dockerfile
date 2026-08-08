# agent-harness — controller image.
#
# This image is the **controller**, not an agent sandbox. It holds the queue,
# the gates, the model client and the credentials; it creates a separate,
# disposable container per work item through the selected execution backend and
# never runs an agent's commands itself.
#
# That distinction decides two things here:
#
#   * the Docker CLI is installed, because `adapters/docker.py` shells out to
#     it — but no Docker socket is baked in. The daemon is supplied at runtime
#     through `DOCKER_HOST`, which in the Node B stack points at a dedicated
#     DinD sidecar on an internal network. The controller therefore never holds
#     root-equivalent access to the deploy host (STATUS.md §2.7).
#   * item worktrees live under a path that must be **identical** in this
#     container and in whichever daemon creates the item containers. A bind
#     mount is resolved by the daemon, not by the client, so a controller that
#     mounts `/harness/work` while the daemon knows that content by another
#     path would silently mount an empty directory into every agent's
#     checkout. `HARNESS_WORK_ROOT` names that shared path.
#
# Targets, per STATUS.md §2.7's "publish deliberately different image targets":
#
#   test     the four repository gates, with dev dependencies and the tests
#   runtime  the service, without them
#
# Base pinned by tag; the resolved digest is recorded by preflight and in item
# evidence so a result stays explicable after a tag moves.

# ---------------------------------------------------------------- base

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# git is not optional: the harness allocates a worktree per item, computes the
# candidate diff and drives plan-branch promotion. docker-cli talks to the
# daemon named by DOCKER_HOST. ca-certificates is needed to reach a gateway.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        git \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# A committing identity, because the harness commits: the suite builds real
# git repositories and the executor commits an item branch. Without one, git
# refuses with "Please tell me who you are" in an image where no human can.
RUN git config --system user.name "agent-harness" \
    && git config --system user.email "agent-harness@invalid" \
    && git config --system init.defaultBranch main \
    && git config --system --add safe.directory '*'

WORKDIR /app

# Dependency layer first, so a source-only change does not re-resolve.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --all-extras

# The whole tree, not a hand-listed subset. The `test` stage below runs the
# repository's own gates, and those gates read the repository: the suite
# asserts against `docs/DEPLOYMENT.md`, builds a wheel from `pyproject.toml`,
# and reads `README.md`. A curated COPY list makes that a build failure every
# time a test starts reading a file nobody remembered to add — which is
# exactly how the first build of this image failed. `.dockerignore` names what
# must stay out.
COPY . .
RUN uv sync --frozen --all-extras

# ---------------------------------------------------------------- test

FROM base AS test

# The same four gates the repository runs, in the image that will be deployed.
# TMPDIR matters: the suite creates temporary git repositories heavily.
ENV TMPDIR=/tmp
RUN uv run ruff check . \
    && uv run ruff format --check . \
    && uv run mypy \
    && uv run pytest -q

# ------------------------------------------------------------- runtime

FROM base AS runtime

# Runtime carries no dev dependencies. Re-synced rather than copied from a
# clean layer so the lock file remains the single source of what is installed.
RUN uv sync --frozen --no-dev --extra agent-loop

# `/harness/work` is where a project's checkout must live. It is not a setting
# the harness reads — a project's `work_dir` is a row on the project, supplied
# when the project is registered — it is a **deployment constraint**: the
# daemon that creates item containers resolves bind mounts by its own paths, so
# a project registered outside the shared volume would hand every agent an
# empty checkout. Register projects under this path and nowhere else.
ENV HARNESS_DB=/harness/state/queue.sqlite \
    HARNESS_AUDIT_DB=/harness/state/audit.sqlite \
    PATH="/app/.venv/bin:$PATH"

# The controller does not need root, and an agent's commands never run here
# anyway. The Docker CLI only needs to reach DOCKER_HOST over TCP.
RUN groupadd --gid 1000 harness \
    && useradd --uid 1000 --gid 1000 --create-home harness \
    && mkdir -p /harness/work /harness/state \
    && chown -R harness:harness /harness /app
USER harness

EXPOSE 8080

# `serve` is the deployment entry point; `run` is the one-shot CLI. Neither
# claims work until a project is started through the API.
CMD ["agent-harness", "serve", "--host", "0.0.0.0", "--port", "8080"]
