# Stage 2 live evidence — the execution boundary, against a real daemon

**Date:** 2026-08-08
**Where:** Node B (`winrarhost`), Komodo stack `personal-agent-harness`, against
a dedicated Docker-in-Docker daemon deployed beside the controller.
**Scope:** the two tests in `tests/test_execution_environment_live.py`, run in
the deployed stack. No model was called, no workload was run, and no remote
repository was contacted.

This is the first time any part of this repository has executed against a real
Docker daemon. Everything before it was a mocked subprocess.

**It is not the Stage 2 exit.** §"What this does not cover" below says what is
still missing, and that list is not short.

## What ran, and how to run it again

```bash
# Komodo: POST /execute/RunStackService, then poll /read/GetUpdate.
{"stack": "personal-agent-harness", "service": "agent-harness-tests"}
```

```console
success: True
..                                                                  [100%]
```

Update `6a76fcfbe1e8f310d4eae2c2`. Two tests, two passes, zero skips — the
suite skips itself unless `HARNESS_STAGE2_IMAGE` names a pullable image and a
daemon answers, so a pass cannot be a silent no-op.

### The exact artefacts

Recorded because "it passed" is not reproducible and a tag moves:

| artefact | identity |
|---|---|
| controller image | `agent-harness:75e76ed5ad5e70d94b68e55c4582cb9efb2f1120` → `sha256:65a4f2073df81572f4e8c4100d89af425dd4e47450f883918471d94ba8d7b527` |
| acceptance image | `agent-harness-test:75e76ed5ad5e70d94b68e55c4582cb9efb2f1120` → `sha256:9808728bd93bf33417ed3151840f3744198c9c3fca22bfb64ab34b640eb652fa` |
| item sandbox image | `alpine:3.21` → `sha256:2607caa9805847fac4de202017bb1b830deb09f4c07dc9964a0157abbc604577` |
| nested daemon | `docker:28-dind` → `sha256:6a68f64cf32d98b09a11c208de78f59f17c0a6fff33c13f11acac853d6aad5ae` |

## What the two tests prove

Each row is an assertion that fails if the property stops holding, executed
against the daemon named above.

| criterion | result | how |
|---|---|---|
| An agent can **read** its own worktree | pass | `cat /workspace/inside.txt` returned `inside`, from a file the controller wrote outside the container |
| An agent can **write** its own worktree | pass | `printf changed > /workspace/result.txt`, then read back by the controller |
| A **declared** dependency mount is readable | pass | `test -f /opt/dependency/readme.txt` |
| An **undeclared** host/sibling path is unreachable | pass | `test ! -e "$HOST_SIBLING"`, where the path exists on the host and holds content |
| Controller **credentials** do not enter the agent environment | pass | `HARNESS_STAGE2_CONTROLLER_SECRET` is set in the controller process and absent in the container |
| An explicitly passed variable **does** arrive | pass | `DECLARED=yes`, so the credential result above is not merely an empty environment |
| `network=none` **denies** outbound access | pass | `wget https://example.com` → `bad address` (DNS and egress both fail) |
| `network=bridge` **allows** outbound access | pass | the same fetch succeeds — P6's "internet is available" is real, not aspirational |
| The container is **gone** after teardown | pass | `docker inspect <id>` fails after `close()` |

## What this does not cover

Named explicitly, because a green run invites over-reading.

- **The security profile is asserted only in argv.** `--read-only`,
  `--cap-drop ALL`, `--security-opt`, the resource limits and the resolved
  image digest are covered by `tests/test_execution_environment.py` at the
  `docker create` command line, and are **not** verified from inside a live
  container. Nothing here proves the kernel applied them.
- **The nested daemon's own confinement is untested.** It is `privileged`, on
  an internal network with nothing published, and no test asserts either
  property.
- **One image, one shape.** Alpine/BusyBox only. A workload toolchain image —
  Rust, for the first real workload — has never been used, and the last defect
  this suite found was precisely an assumption about which userland is present.
- **No workload ran.** No model call, no item, no plan. Stage 2 says a real
  workload is not authorised until its exit is met, and it is not met.
- **The controller has no model routes**, so the deployed service cannot claim
  work at all. That is deliberate for a first deployment, and it means nothing
  here exercises the fleet.

## What the live run found that nothing else did

Recorded because it is the argument for having deployed at all. Each of these
was invisible to local runs and to CI, and each is fixed:

| defect | why only a real daemon found it |
|---|---|
| `timeout --signal=TERM 30s` is GNU-only | against BusyBox **every** sandbox command returned 1 with `timeout: unrecognized option`, which reads as the agent failing rather than the harness's wrapper being unportable — the misattribution class of #216 |
| two divergent copies of the daemon check | `DockerItemEnvironment.check()` kept the `--format` template defect after the factory's copy was fixed; an unreachable daemon reported a Go reflect error instead of naming the daemon |
| `serve` exited 2 with a fleet and no routes | the API and GUI never came up to say why, and the supervisor restarted it every 60 seconds |
| mounts owned by root, controller uid 1000 | a mount replaces the image's directory, so the image's `chown` is not the runtime truth; the controller crash-looped on `unable to open database file` |
| the acceptance pinned `:latest` | `docker compose run` does not re-pull a tag it already holds, so the suite silently re-ran a **stale** image and reproduced an already-fixed defect. `agent-harness-test:latest` was `sha256:d73e54bd…` while the current build was `sha256:9808728b…` |

The last one is worth keeping in mind when reading any evidence produced this
way: an acceptance run that cannot name the image it ran is not evidence. Both
images are now pinned to a commit sha and the build bumps both.

## The uid that made the difference

The confinement test first failed with

```
/bin/sh: can't create /workspace/result.txt: Permission denied
```

That is not a boundary failure. The acceptance container ran as root, so its
fixture worktrees were root-owned, and the item container — uid 1000, which is
`EnvironmentSpec.user`'s default and the uid the deployed controller runs as —
could not write to its own checkout.

The fix was to make the acceptance mirror the deployment (`user: "1000:1000"`),
not to relax the assertion. An agent being able to write its worktree is one of
the things Stage 2 exists to prove, and weakening it to get a green run would
have produced exactly the kind of evidence this repository refuses to accept.
