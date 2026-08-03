# Deploying `agent-harness serve`

The contract between whatever starts this process — systemd, a compose file,
a Kubernetes manifest, a session host's supervisor — and what the service can
then actually do.

There are **two supported modes**, and the difference is deliberate rather
than a degraded state:

| | Monitoring-only | Supervised |
|---|---|---|
| Flags | `--db`, `--host`, `--port`, `--root-path` | those, plus `--session-host`, `--agent`, `--reviewer`, `--endpoint` |
| Reads (work, events, audit, projects) | yes | yes |
| `POST /api/projects/{id}/start` | **refuses, by design** | starts real workers, after preflight |
| Who it is for | a dashboard over someone else's harness | the deployment that does the work |

The trap this document exists to close: **a monitoring-only process is
healthy.** `/healthz` returns `ok`, the API answers, the Work tab renders a
backlog — and nothing can execute a single item. A process manager started
with only the monitoring arguments produces exactly that, and nothing about it
looks wrong until someone presses start.

Nothing here is specific to a repository, a language, a backlog or a secret
store. Where a credential comes from is the deployment's business; the harness
only requires that the process has one.

---

## Monitoring-only

```bash
HARNESS_TOKEN=… \
agent-harness --db /var/lib/harness/harness.sqlite serve \
    --host 127.0.0.1 --port 8099 \
    --root-path /api/harness
```

The process says so on startup:

```
monitoring only: no --session-host, so no worker pool is attached and
starting a project will be refused.
```

`start` returning **409** here is correct behaviour and not something to work
around. Marking a project `running` with nothing able to claim is the failure
this refusal exists to prevent.

---

## Supervised execution

```bash
export HARNESS_TOKEN=…      # the API's bearer token
export HARNESS_API_KEY=…    # the reviewer's model credential
export AIDEVENV_TOKEN=…     # the session host's token

agent-harness --db /var/lib/harness/harness.sqlite serve \
    --host 127.0.0.1 --port 8099 \
    --root-path /api/harness \
    --session-host https://your-session-host.example \
    --agent 'claude -p {prompt_file}' \
    --reviewer claude-sonnet-4-6 \
    --endpoint https://api.your-gateway.example
```

What the deployment must provide, and why each one:

| Requirement | Why |
|---|---|
| `--session-host` reachable, and `AIDEVENV_TOKEN` accepted by it | Agents run as terminal sessions on it. Without it there is no worker pool and `start` refuses. |
| `--agent` runnable **in the session host's environment**, with the credentials it needs to clone, commit and push | The agent — not the harness — does the implementing. Its environment is where `git` and `gh` credentials have to be. The harness never injects them. |
| `gh` authenticated with **push** permission on each project's repo | Preflight asks GitHub for `permissions.push` rather than looking for a token: a token that exists and lacks the scope fails at the point where an agent has already done the work. |
| `--reviewer` + `--endpoint`, and `HARNESS_API_KEY` | The reviewer is the only role that needs a model in this mode. With none routed, every review fails closed, so every item fails *after* the implementation has been paid for. |
| A writable `--db` path, and ideally `HARNESS_AUDIT_DB` on a different volume | History must not share a fate with the queue, which is a reasonable thing to delete and rebuild from the plan. |
| Per project: a checkout (`work_dir`), a `repo`, and `checks` | Registered with `POST /api/projects`, not passed as flags — one deployment serves several projects and cannot have one checkout on its command line. |

Two properties worth stating explicitly, because deployments tend to assume
the opposite:

- **Booting starts nothing.** Every project is set `stopped` on boot, and
  building the worker pool creates no workers. An auto-resuming fleet turns a
  routine restart into unattended spend, and a crash-looping deploy restarts it
  on every loop. `POST /api/projects/{id}/start` is the only thing that starts
  work, and it runs preflight first.
- **Stopping drains.** Process shutdown and `POST /api/projects/{id}/stop`
  both stop *new* claims and join in-flight work. The HTTP action returns once
  `control.state` is `draining`; `GET /api/projects/{id}` shows the
  `draining_items` it is waiting for and reports `stopped` after the join.
  Process shutdown itself still blocks. Killing an agent mid-item destroys
  the context that makes its work resumable.

---

## Deployment smoke test

Non-destructive: it creates no worker, no session, no branch and no item, and
changes no state. Run it after every deploy.

```bash
BASE=http://127.0.0.1:8099
AUTH="Authorization: Bearer $HARNESS_TOKEN"

# 1. The service is up. Needs no credential — that is the point of /healthz.
curl -fsS "$BASE/healthz" | jq -e '.ok and .queue'

# 2. The token works and the projects you expect are registered.
curl -fsS -H "$AUTH" "$BASE/api/projects" | jq -e '.projects | length > 0'

# 3. The one that matters: can anything actually RUN?
curl -fsS -H "$AUTH" "$BASE/api/readiness" | jq
```

```json
{
  "mode": "supervised",
  "ready_to_start": true,
  "workers":      {"configured": true, "ok": true, "detail": "0 worker(s) running"},
  "session_host": {"configured": true, "ok": true, "detail": "reachable and authenticated, 3 live session(s)"},
  "reviewer":     {"configured": true, "ok": true, "detail": "reviewer routed to claude-sonnet-4-6; …"},
  "projects": [
    {"project_id": "widgets", "ready_to_start": true, "summary": "ready", "blockers": [], "warnings": []}
  ]
}
```

Assertions worth putting in the deploy script:

```bash
# Supervised deployments must be supervised. This is the check that catches a
# process manager started with the monitoring arguments.
curl -fsS -H "$AUTH" "$BASE/api/readiness" | jq -e '.mode == "supervised"'

# And every project you deployed for must be startable, with the reasons
# printed when it is not.
curl -fsS -H "$AUTH" "$BASE/api/readiness" \
  | jq -e '.projects | all(.ready_to_start)' \
  || curl -fsS -H "$AUTH" "$BASE/api/readiness" \
     | jq -r '.projects[] | select(.ready_to_start | not)
              | "\(.project_id): \(.summary)"'
```

`readiness` reports `configured` and `ok` separately for each capability,
because *not deployed* and *deployed but refusing* need different fixes. For a
monitoring-only deployment, assert `.mode == "monitoring-only"` instead — the
expected result there is that nothing can start.

Each project costs one read of GitHub's permissions for its repo, so a
deployment that polls this should name the project it cares about:
`/api/readiness?project_id=widgets`.

To run the configured argv checks in a detached, clean worktree of the base
branch, start a run and poll it:

```bash
curl -sH "$AUTH" -X POST localhost:8099/api/projects/widgets/preflight/base
curl -sH "$AUTH" localhost:8099/api/projects/widgets/preflight/base | jq
```

The run happens on a background thread, and the POST returns as soon as it has
*started*. That is not a convenience: this probe compiles and tests an entire
repository, so a request that waited for it would outlive any proxy timeout,
and the caller would get a transport error for a build that was running fine.
Calling POST again while one is in flight joins it rather than starting a
second.

`&check_base=true` on readiness, project preflight and `POST
/api/projects/{id}/start` then reports **the most recent run**, and never
starts one — a readiness read stays a read. Before any run it reports
`not_run`, which is blocking, and names the call that would answer it.

**Do not** smoke-test by calling `start`. It is a state-changing request: on a
correctly configured deployment it begins spending money, and on a
misconfigured one it tells you less than the line above.

---

## When readiness says no

| `blockers[].name` | Fix |
|---|---|
| `workers` | The process is monitoring-only. Restart it with `--session-host` (and the agent/reviewer flags). |
| `session host` | The host is configured but refused a read. Check it is up, and that `AIDEVENV_TOKEN` is the token it expects. |
| `checkout` | The project's `work_dir` is missing or is not a git repository *inside this process's filesystem* — a container needs it mounted. |
| `disk space` | The volume holding `work_dir` is below the project's configured `min_free_disk_gb` floor. Free and total GiB are included in the detail. |
| `github write` | `gh` is missing, unauthenticated, or the account lacks push on that repo. |
| `reviewer` | No reviewer route. `PUT /api/roles`, or restart with `--reviewer`/`--endpoint`. |
| `base checks` | A configured command failed on an unmodified base-branch worktree — fix the command or its prerequisites before starting. Also reported when no run has happened yet (`not_run`) or one is still going. |

Warnings do not block a start and are still worth reading: `checks` means
nothing verifies a diff before the reviewer sees it, and `reviewer
independence` means some share of reviews is a model grading its own work.
