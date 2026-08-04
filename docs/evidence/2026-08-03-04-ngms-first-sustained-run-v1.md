# Evidence package: first sustained NGMS workload, 2026-08-03/04 (v1)

**Evidence package ID:** `ngms-first-sustained-2026-08-03-04-v1`
**Report version:** 1
**Report date:** 2026-08-04
**Observation window:** 2026-08-03 through 2026-08-04 UTC
**Status:** historical reconstruction; raw run artifacts are not retained here

## Decision

**Continue to the deterministic Stage A safety slice. Stop using this report
as a quantitative before/after baseline.**

The issue and pull-request record is sufficient to attribute the discovered
defects and select deterministic regression scenarios. It is not sufficient
to reproduce one run: the evidence describes several related supervised NGMS
attempts at different harness commits, later direct-API runs on a small local
repository, and a subsequent NGMS direct-API attempt. No common run identifier,
complete configuration snapshot, raw-artifact checksums or complete provider
traffic record was preserved.

Consequently this report accepts defect discovery as evidence and rejects all
claims that require a reproducible aggregate baseline, including a universal
defect count, a percentage attributed to patch application, total spend, a
complete 429/error-class distribution, or a before/after improvement. A new
live measurement must be a new append-only report with those artifacts; it
must not rewrite this one.

## Evidence classification and scope

Labels in this report mean:

- **Repository fact (RF):** a statement tied to a named agent-harness commit,
  source location, test, or merged pull request. It can be checked out and
  inspected without the original deployment.
- **Live observation (LO):** output reported from Node B, NGMS, a provider, or
  a direct executor run. The GitHub record is retained, but the original raw
  artifact is not available in this repository.
- **Hypothesis (H):** a causal explanation or extrapolation which is useful for
  investigation but is not an acceptance result.
- **Missing (M):** evidence required by the proposal but unavailable. Missing
  does not mean zero.

This package uses the stable GitHub records
[#113](https://github.com/TheDancingDeveloper-org/agent-harness/issues/113)
through
[#146](https://github.com/TheDancingDeveloper-org/agent-harness/issues/146)
as its source register. Issue bodies and comments contain the original
reproductions; merged PR commits provide repository-verifiable fixes and
regression claims. This report does not assert that a closed issue proves the
live problem moved.

## Run identity and artifacts

| Required field | Evidence retained | Class / disposition |
|---|---|---|
| Run identifier | **Missing.** No deployment-generated run ID appears in #113–#146. This report's package ID identifies the reconstruction, not the run. | M |
| Harness commit | Multiple: supervised observations name `70a9aae`, `bcb4a79`, `a444319` and `cbfdb7c`; the later fixes progressed through `c6d15a8`, `a7ce6df`, `cfc4ccd`, `7c5f122` and `6a909a7`. There is no single commit under test. | RF/LO; attributable per observation |
| Configuration | Partial only: supervised `serve` on `aidevenv-feat` / Node B; NGMS project sometimes named `default`; `max_workers: 3`; one recorded `max_attempts: 5`; four initial Cargo checks and a later eight-command check list. Full project JSON, environment, timeouts, role map and secrets-redacted config are **missing**. | LO/M |
| Executor mode | Both modes occurred. Early NGMS observations used supervised session execution; #129–#143 include direct API-executor runs; #145–#146 are a later direct API NGMS attempt. No single mode describes the package. | LO |
| Project | `TheDancingDeveloper-org/NGMS`; the direct patch-placement fixture was an unnamed local repository containing `calc.py` and `test_calc.py`. Exact NGMS commit/base revision is **missing**. | LO/M |
| Provider route | Partial only: reviewer `claude-sonnet-4-6` via `https://api.theclawbay.com/v1`; later direct runs mention `gpt-5.4`, `deepseek-v4-flash` and `glm-5.2`. Session-mode implementer route/model was outside `ModelClient`; full auth/protocol/pricing configuration is **missing**. | LO/M |
| Raw event/log locations | Reported live locations: `/var/lib/aidevenv/events.jsonl`, `/var/lib/aidevenv/audit.sqlite`, and the queue/event database used by the deployment. The latter's exact path is **missing**. Direct-run event and preserved-patch paths are **missing**. These paths referred to Node B and are not repository artifacts. | LO/M |
| Artifact integrity | No checksum, object-store URI, release attachment, or immutable copy of any raw JSONL/SQLite/provider response was retained in #113–#146. | M; historical numbers cannot be independently recomputed |
| Time bounds | Issue/PR creation timestamps bound discovery from 2026-08-03T10:20:09Z (#113) through 2026-08-04T01:42:47Z (#146). Exact run start/end timestamps are **missing**. | RF/M |

### Raw-artifact references and integrity status

| Reported artifact | What it was said to contain | Integrity reference |
|---|---|---|
| `/var/lib/aidevenv/events.jsonl` | Work-stage events, including T35 and session lifecycle events | **Missing checksum/immutable copy.** Excerpts are retained in #113, #115, #123 and #129. |
| `/var/lib/aidevenv/audit.sqlite` | Audit projections and later work events | **Missing checksum/immutable copy.** Query output is retained in #113 and #128. |
| Harness queue database | Item state, attempts, branch, PR URL and last error | **Missing exact path and checksum.** Selected API/JSON output is retained in #115, #123, #124 and #126. |
| Preserved model patches | Malformed or rejected unified diffs | **Missing path/checksum.** Relevant patch excerpts are retained in #132, #133, #142, #143 and #145. |
| Provider sweep result | Availability of 42 advertised models | The referenced `docs/2026-08-03-clawbay-model-availability.md` is not present in this checkout and no checksum is given. Summary only in #131 and #141. |

## Observed measurements

Every row gives its denominator. “Reported” means the retained GitHub record is
the evidence; without the raw artifact the value is not independently
recomputed here.

| ID | Measurement and denominator | Class | Source and collection evidence |
|---|---|---|---|
| M1 | **6/6 dispatched work items** were in the first reported supervised workload; the same excerpt reports **27 total execution events**, **3/6 items with check failures**, **1/6 with a draft PR**, and **1 reviewer ladder of 6 attempts**. These outcomes overlap and are not a partition. | LO | [#113](https://github.com/TheDancingDeveloper-org/agent-harness/issues/113); exact event-count command missing. |
| M2 | Audit database contained **0 events, 0 baselines and 0 daily rollups** against **27 JSONL execution events** in the reported first workload. API audit/error projections also returned zero rows/counts. | LO | #113; SQLite/API commands are in “Collection commands”. |
| M3 | **3/3 named items** (T32, T33, T37) paid for complete agent sessions before encountering the same impossible base-check configuration; T32 ran for approximately **19 minutes**. The total set considered was those three named attempts, not the whole backlog. | LO | [#114](https://github.com/TheDancingDeveloper-org/agent-harness/issues/114); exact duration command missing. |
| M4 | Reconciliation reported **27/27 queried PRs skipped**, including the **1 known harness-created draft**, NGMS #108. This is a reconciliation denominator, not evidence that the harness created 27 PRs. | LO | [#115 comment](https://github.com/TheDancingDeveloper-org/agent-harness/issues/115#issuecomment-5165226912); `curl` command below. |
| M5 | **2/2 in-flight worktrees** occupied **32 GiB total** (13 GiB + 19 GiB); **2/2 older orphan worktrees** occupied 85 MiB each. The volume reported **67 GiB free** and the configured pool was **3 workers**. A projected third 16 GiB worktree was an extrapolation, not observed. | LO/H for projection | [#117](https://github.com/TheDancingDeveloper-org/agent-harness/issues/117); `df`/`du` below. |
| M6 | Item T27 exhausted **3/3 attempts** after the reported disk-related link failure. | LO | #117; exact queue query missing. |
| M7 | **1/1 stop request** shown returned 502 although the project reached stopped with 0 workers. The request allowed up to 900 seconds; exact elapsed time was not retained. | LO | [#118](https://github.com/TheDancingDeveloper-org/agent-harness/issues/118); `curl` command below. |
| M8 | **2/2 base-check requests** returned 502 at the proxy's approximately 15-second limit and left **2/2 builds** running, using 782 MiB and 2.7 GiB at observation time. | LO | [#121](https://github.com/TheDancingDeveloper-org/agent-harness/issues/121); timed `curl`, worktree, `du`, and `ps` commands below. |
| M9 | T27's agent stage ran **915.3 seconds** against a **900-second lease**, an observed overrun of **15.3 seconds**. It was at **5/5 attempts** and left a **13 GiB** worktree. | LO; arithmetic derived from retained timestamps | [#123](https://github.com/TheDancingDeveloper-org/agent-harness/issues/123). |
| M10 | **3/3 open NGMS PRs on recorded harness branches** (#108–#110) had no queue `pr_url` in the reported reconciliation check. | LO | [#124](https://github.com/TheDancingDeveloper-org/agent-harness/issues/124); `gh pr list` and API query below. |
| M11 | Session telemetry query returned **16/16 events of kind `work`, 0/16 with a non-null model, and 0 `model_call` JSONL records**. Cost had 0 rows and errors reported total 0; those zeroes mean unobserved traffic, not zero traffic. | LO | [#128](https://github.com/TheDancingDeveloper-org/agent-harness/issues/128); SQLite/grep/curl commands below. |
| M12 | Endpoint sweep reported **8/42 advertised models answering (19.0%)** and **34/42 not answering (81.0%)**. The stated subcounts—19 service-unavailable, 8 maintenance, 5 no response—sum to 32, leaving **2/42 unclassified in the retained summary**. | LO/M | [#131](https://github.com/TheDancingDeveloper-org/agent-harness/issues/131) and [#141](https://github.com/TheDancingDeveloper-org/agent-harness/pull/141); sweep command/raw table missing. Percentages are arithmetic over 42. |
| M13 | A direct three-item fixture went from **0/3 reported complete before applied-diff review**, to **1/3 complete** with 2/3 honestly rejected for placement, and ultimately **3/3 complete** after safe hunk recounting. The intermediate “0/3” report also says only two items were attempted, so it must not be read as three observed failures. | LO | [#134](https://github.com/TheDancingDeveloper-org/agent-harness/pull/134), [#133](https://github.com/TheDancingDeveloper-org/agent-harness/issues/133), [#143](https://github.com/TheDancingDeveloper-org/agent-harness/pull/143); exact run commands/config missing. |
| M14 | Over-counted hunk headers reproduced on **4/4 consecutive runs across 2 models**; one retained complete response used **266 output tokens**. | LO | [#142 correction](https://github.com/TheDancingDeveloper-org/agent-harness/issues/142#issuecomment-5173111728) and #143; provider command/usage artifact missing. |
| M15 | A fallback trial made **3 route calls** in approximately **8 seconds**, with **0 backoff waits**, and completed **1/1 item** after the first two routes were unavailable. | LO | #141; exact invocation/timing command missing. |
| M16 | On an approximately **26,000-file** NGMS tree and a **60,000-character configured content budget**, context supplied **54 files** totaling **60,981 characters**. It was identical for **2/2 unrelated items** and omitted the named target; both **2/2 attempts** failed at apply. | LO | [#146](https://github.com/TheDancingDeveloper-org/agent-harness/issues/146); context calculation command and exact tracked-file denominator missing. |
| M17 | An uncommitted implementation was reported as **891 changed lines across 24 files**, written by another agent and recovered into #120. This is evidence of an authorship/provenance gap, not a measurement of NGMS product code. | LO | [#120](https://github.com/TheDancingDeveloper-org/agent-harness/pull/120); original diff/stat command missing. |

### Measurements that cannot be accepted

- **Aggregate defect count:** #113–#146 contain overlapping root causes,
  follow-up defects and fix PRs. Counting issue numbers would double-count
  behavior and is not a denominator over executed items.
- **Percentage due to patch application:** no complete attempt-level outcome
  table survives. M13 and M16 are bounded fixture/run results, not a workload
  percentage.
- **NGMS completed/merged-work rate:** #115/#124 identify draft PRs, but no
  complete backlog denominator and final merge reconciliation are retained.
- **Total cost or token use:** session implementer traffic was structurally
  absent from telemetry (#128); unknown is not zero.
- **429/error-class baseline or improvement:** initial classified model events
  were discarded (#113), and session implementer calls remained invisible
  (#128). There is no comparable before/after error-class distribution.
- **Lines of unaccounted NGMS work:** M17 is an agent-harness diff-stat claim,
  not a retained inventory of unaccounted NGMS changes.

## Defect inventory

The inventory lists the behavior discovered in this observation window, not
the number of independent root causes. Each row has a retained source or
reproduction. Fix PRs are corroboration only; they do not prove the live fleet
improved.

| Defect | Classification | Reproduction or source | Fix/corroboration in #113–#146 |
|---|---|---|---|
| Execution and `ModelClient` telemetry bypassed both audit/event stores, leaving projections empty. | RF + LO | #113 source trace and live 27-event/0-audit comparison | [#120](https://github.com/TheDancingDeveloper-org/agent-harness/pull/120) |
| Base checks were not validated before paid work, so an incomplete check list failed after each agent. | RF + LO | #114, T32/T33/T37 and clean-base counterfactual | #120 |
| A post-draft failure discarded branch/PR checkpoint data. | RF + LO | #115, T35/NGMS#108 queue row versus GitHub | #120 |
| Transient retry exhaustion terminally failed work while a cost cap re-queued it. | RF + LO | #116, T35 six-attempt reviewer failure | #120 |
| Worktree/build disk was unbounded, leaked, and an environment failure was attributed to item checks. | RF + LO | #117 `df`, `du`, T27 3-attempt result | #120 |
| Project stop blocked through the drain and a successful transition surfaced as 502. | RF + LO | #118 live stop/API/process observations | #120 |
| The stop route rejected `{}` or a reason-only body because it required an unrelated state field. | RF + LO | #119 curl/OpenAPI reproduction | #120 |
| The new base-check probe repeated the synchronous-request defect and duplicated work after proxy timeout. | RF + LO | #121 two timed 502s and two running check worktrees | [#125](https://github.com/TheDancingDeveloper-org/agent-harness/pull/125) |
| `latest` and `/api/events` still read the store that supervised execution did not populate. | RF + LO | #122 audit events versus empty API/latest | #125 |
| A 900-second lease expired during a 915.3-second healthy agent stage and retired live work. | RF + LO | #123 retained timestamps, queue state and worktree | #125 |
| Reconciliation could not recover an open PR from a recorded harness branch when `pr_url` was absent. | RF + LO | #124 three open harness branches with null URLs | #125 |
| Retrying an exhausted item preserved the exhausted attempt count and erased its failure reason. | RF + LO | #126 `exhausted → pending → exhausted` live API trace | [#138](https://github.com/TheDancingDeveloper-org/agent-harness/pull/138) |
| Session mode advertised planner/implementer routes it did not call and computed independence from the wrong route. | RF + LO | #127 role API versus the single session `ModelClient` call site | [#140](https://github.com/TheDancingDeveloper-org/agent-harness/pull/140) |
| Session-agent planning/implementation traffic was absent from model cost/error telemetry. | RF + LO | #128 16 work events, 0 model-bearing/model-call events | **Open:** no fix in this range |
| Logging was never configured and direct `run` hid a 209.9-second retry wait. | RF + LO | #129 source search and direct-run event trace | [#130](https://github.com/TheDancingDeveloper-org/agent-harness/pull/130) |
| Preflight checked that a reviewer was named, not that it answered. | RF + LO | #131 failing reviewer plus 42-model sweep | #140 |
| Direct executor reviewed the proposed diff rather than the applied tree. | RF + LO | #132 proposed/applied diff and reviewer verdict | [#134](https://github.com/TheDancingDeveloper-org/agent-harness/pull/134) |
| `--unidiff-zero` applied an unplaceable existing-file hunk at line 1 while checks stayed green. | RF + LO | #133 direct `calc.py` reproduction | [#139](https://github.com/TheDancingDeveloper-org/agent-harness/pull/139) |
| Direct implementer received empty repository context and consequently emitted context-free hunks. | RF + LO | #135 default provider trace and three-item run | [#136](https://github.com/TheDancingDeveloper-org/agent-harness/pull/136) |
| Provider fallback existed in CLI/storage but was absent from the deployed `RoleRoute` API schema. | RF + LO | [#144](https://github.com/TheDancingDeveloper-org/agent-harness/pull/144) OpenAPI key query | #144 was open at report snapshot |
| Patch validation rejected ordinary create/delete/rename metadata, safe mid-diff over-counts, and complete hunkless file operations. | RF + LO | [#145](https://github.com/TheDancingDeveloper-org/agent-harness/pull/145), including T27/T28 parse failures | #145 was open at report snapshot |
| Smallest-file-first context exhausted the budget on stubs/artifacts and omitted task targets. | RF + LO | #146, 54-file context shared by T27/T28 | **Open:** Stage E2 blocker |

### Corrected diagnosis retained

[#142](https://github.com/TheDancingDeveloper-org/agent-harness/issues/142)
was initially filed as a truncated-response defect. The reporter then inspected
the preserved patch and corrected the diagnosis in an
[append-only comment](https://github.com/TheDancingDeveloper-org/agent-harness/issues/142#issuecomment-5173111728):
the body was complete, while both hunk counts were over-declared by one. This
report uses the correction and retains the original diagnosis as superseded,
not deleted. [#143](https://github.com/TheDancingDeveloper-org/agent-harness/pull/143)
implemented derivable recounting while retaining refusal for genuinely
truncated, ambiguous bodies.

## Source register, #113–#146

State is the GitHub state observed while producing this report on 2026-08-04.
An issue being closed means a PR referenced it; it is not a measured live-run
verification.

| Record | Kind/state | Role in this package |
|---|---|---|
| [#113](https://github.com/TheDancingDeveloper-org/agent-harness/issues/113) | issue/closed | Initial supervised telemetry defect and live counts |
| [#114](https://github.com/TheDancingDeveloper-org/agent-harness/issues/114) | issue/closed | Base-check configuration defect and three wasted runs |
| [#115](https://github.com/TheDancingDeveloper-org/agent-harness/issues/115) | issue/closed | Lost PR checkpoint and reconciliation result |
| [#116](https://github.com/TheDancingDeveloper-org/agent-harness/issues/116) | issue/closed | Transient-exhaustion lifecycle defect |
| [#117](https://github.com/TheDancingDeveloper-org/agent-harness/issues/117) | issue/closed | Disk measurements and environmental misclassification |
| [#118](https://github.com/TheDancingDeveloper-org/agent-harness/issues/118) | issue/closed | Blocking-stop/502 observation |
| [#119](https://github.com/TheDancingDeveloper-org/agent-harness/issues/119) | issue/closed | Stop request-schema reproduction |
| [#120](https://github.com/TheDancingDeveloper-org/agent-harness/pull/120) | PR/merged at `a444319` | Fix record for #113–#119; 535-test claim; recovered 891-line diff |
| [#121](https://github.com/TheDancingDeveloper-org/agent-harness/issues/121) | issue/closed | Blocking base-probe follow-up and duplicate-build measurements |
| [#122](https://github.com/TheDancingDeveloper-org/agent-harness/issues/122) | issue/closed | Remaining empty latest/events projection |
| [#123](https://github.com/TheDancingDeveloper-org/agent-harness/issues/123) | issue/closed | Lease/heartbeat live reproduction |
| [#124](https://github.com/TheDancingDeveloper-org/agent-harness/issues/124) | issue/closed | Branch-based PR recovery gap |
| [#125](https://github.com/TheDancingDeveloper-org/agent-harness/pull/125) | PR/merged at `cbfdb7c` | Fix record for #121–#124; 548-test claim |
| [#126](https://github.com/TheDancingDeveloper-org/agent-harness/issues/126) | issue/closed | Exhausted-item retry no-op |
| [#127](https://github.com/TheDancingDeveloper-org/agent-harness/issues/127) | issue/closed | Effective-role reporting defect |
| [#128](https://github.com/TheDancingDeveloper-org/agent-harness/issues/128) | issue/open | Explicit session-mode telemetry blind spot |
| [#129](https://github.com/TheDancingDeveloper-org/agent-harness/issues/129) | issue/closed | Silent runtime/retry-wait defect |
| [#130](https://github.com/TheDancingDeveloper-org/agent-harness/pull/130) | PR/merged at `0927bf8` | Logging/run-output fix and 548-test claim |
| [#131](https://github.com/TheDancingDeveloper-org/agent-harness/issues/131) | issue/closed | Role reachability defect and provider sweep |
| [#132](https://github.com/TheDancingDeveloper-org/agent-harness/issues/132) | issue/closed | Proposed-versus-applied review defect |
| [#133](https://github.com/TheDancingDeveloper-org/agent-harness/issues/133) | issue/closed | Wrong-location zero-context application |
| [#134](https://github.com/TheDancingDeveloper-org/agent-harness/pull/134) | PR/merged at `b1bb7f4` | Applied-diff review fix and bounded before/after result |
| [#135](https://github.com/TheDancingDeveloper-org/agent-harness/issues/135) | issue/closed | Empty direct-executor context root cause |
| [#136](https://github.com/TheDancingDeveloper-org/agent-harness/pull/136) | PR/merged at `cdf6a00` | First context-provider implementation |
| [#137](https://github.com/TheDancingDeveloper-org/agent-harness/pull/137) | PR/merged at `515ffbe` | Neighboring API/project-isolation regression record |
| [#138](https://github.com/TheDancingDeveloper-org/agent-harness/pull/138) | PR/merged at `4fba501` | Retry, dead-worker and mid-flight dependency fixes |
| [#139](https://github.com/TheDancingDeveloper-org/agent-harness/pull/139) | PR/merged at `66e92ac` | Placement refusal and non-killing pool resize |
| [#140](https://github.com/TheDancingDeveloper-org/agent-harness/pull/140) | PR/merged at `c6d15a8` | Effective routing, honest roles, reachability probe |
| [#141](https://github.com/TheDancingDeveloper-org/agent-harness/pull/141) | PR/merged at `a7ce6df` | Fallback-chain behavior and live proof |
| [#142](https://github.com/TheDancingDeveloper-org/agent-harness/issues/142) | issue/closed | Superseded truncation diagnosis plus corrected hunk-count evidence |
| [#143](https://github.com/TheDancingDeveloper-org/agent-harness/pull/143) | PR/merged at `cfc4ccd` | Safe hunk recount and 3/3 fixture result |
| [#144](https://github.com/TheDancingDeveloper-org/agent-harness/pull/144) | PR/open, head `7c5f122` | Deployed-API fallback configuration gap |
| [#145](https://github.com/TheDancingDeveloper-org/agent-harness/pull/145) | PR/open, head `6a909a7` | Validator rejection gaps found on NGMS |
| [#146](https://github.com/TheDancingDeveloper-org/agent-harness/issues/146) | issue/open | Current NGMS context-selection blocker |

## Collection commands

Commands are copied from the retained source where present. Ellipses and
environment variables were already redacted in the issue, so they are useful
provenance but are not independently executable here. Where no command was
retained, that absence is stated in the measurement table.

### Reconstruct this GitHub source register

This is the exact read-only command used for this report's record metadata:

```bash
for n in $(seq 113 146); do
  gh api "repos/TheDancingDeveloper-org/agent-harness/issues/$n" \
    --jq '[.number, (if .pull_request then "PR" else "issue" end), .state, .title, .created_at, .closed_at, .html_url] | @tsv'
done
```

PR merge/head commits were collected with:

```bash
for n in 120 125 130 134 136 137 138 139 140 141 143 144 145; do
  gh pr view "$n" -R TheDancingDeveloper-org/agent-harness \
    --json number,state,createdAt,mergedAt,url,headRefOid,mergeCommit,headRefName \
    --jq '[.number,.state,.createdAt,(.mergedAt // ""),.headRefOid,(.mergeCommit.oid // ""),.headRefName,.url] | @tsv'
done
```

### Telemetry and projection observations

The first audit-store comparison in #113 was collected with SQLite counts and
these API reads; the exact SQLite invocation was not retained:

```bash
curl -sS -H "$A" "$B/api/errors"
curl -sS -H "$A" "$B/api/audit/cost"
curl -sS -H "$A" "$B/api/audit/delivery"
curl -sS -H "$A" "$B/api/audit/health"
```

The later split-store observation in #122 used:

```bash
curl -sS -H "$A" "$B/api/harness/api/audit/events?limit=3" | jq '.events|length'
curl -sS -H "$A" "$B/api/harness/api/events?limit=5"
curl -sS -H "$A" "$B/api/harness/api/work/T27" | jq -c '{state,attempts,latest}'
```

The session-traffic blind spot in #128 used:

```bash
sqlite3 /var/lib/aidevenv/audit.sqlite "select kind, count(*) from events group by kind;"
sqlite3 /var/lib/aidevenv/audit.sqlite "select count(*) from events where model is not null;"
grep -c model_call /var/lib/aidevenv/events.jsonl
curl -sS -H "$A" "$B/api/harness/api/errors" | jq -c '{total, by_class}'
curl -sS -H "$A" "$B/api/harness/api/audit/cost" | jq -c '{total_cost_usd, rows: (.rows|length)}'
```

### Base-check and disk observations

The clean-base counterfactual in #114 was:

```bash
npm --prefix ui ci
npm --prefix ui run build
npm --prefix client ci
npm --prefix client run build
cargo clippy --workspace --all-features --locked -- -D warnings
```

The disk snapshot in #117 was:

```bash
df -h /home/dev/Working
du -sh /home/dev/Working/Active/apps/.harness-work/*
```

The timed asynchronous-probe reproduction in #121 was:

```bash
curl -sS -H "$A" -w '\nHTTP %{http_code} in %{time_total}s\n' \
  "$B/api/harness/api/projects/default/preflight?check_base=true"
curl -sS -o /dev/null -w '%{http_code} in %{time_total}s\n' \
  "$B/api/harness/api/summary"
git -C /path/to/ngms-unified-arr worktree list
du -sh /path/to/harness-check-*
ps -eo comm,etime --sort=-etime | grep cargo
```

`/path/to` replaces a path abbreviated with `...` in the issue; the original
absolute project path is missing.

### Stop, PR and queue observations

The stop request in #118 was reported as:

```bash
curl -m 900 -X POST "$B/api/harness/api/projects/default/stop" \
  -d '{"state":"stopped","reason":"shut down by operator: ..."}'
```

The stop-schema case in #119 was:

```bash
curl -X POST "$B/api/harness/api/projects/default/stop" -d '{}'
curl -X POST "$B/api/harness/api/projects/default/stop" \
  -d '{"state":"stopped","reason":"shut down by operator: ..."}'
```

The reconciliation observation in the #115 comment was:

```bash
curl -X POST "$B/api/harness/api/audit/reconcile?repo=TheDancingDeveloper-org/NGMS"
```

The branch recovery observation in #124 used:

```bash
gh pr list -R TheDancingDeveloper-org/NGMS --state open \
  --json number,headRefName,isDraft
curl -sS -H "$A" "$B/api/harness/api/work/T27" | jq -c '{branch, pr_url}'
```

The exhausted retry reproduction in #126 used:

```bash
curl -sS -X POST -H "$A" "$B/api/harness/api/work/T27/retry" | jq -c
curl -sS -H "$A" "$B/api/harness/api/work/T27"
```

### Routing, logging and context observations

The effective role inspection in #127 used:

```bash
curl -sS -H "$A" "$B/api/harness/api/roles" | jq .roles
grep -n "self.reviewer.call\|self.client" src/agent_harness/session_executor.py
```

The missing logging configuration in #129 was checked with:

```bash
grep -rn "basicConfig\|logging.config\|setLevel" src/agent_harness/
```

The deployed fallback API gap in #144 was checked with:

```bash
curl -sS "$B/api/harness/openapi.json" \
  | jq '.components.schemas.RoleRoute.properties|keys'
```

The source records do not retain exact commands for the 42-model provider
sweep, the three-item direct runs, the four hunk-count repetitions, the
fallback timing, or the NGMS context-size calculation. Their numbers remain
live observations, not reproducible acceptance measurements.

## Defect status versus measured outcome

Merged PRs in the source window reported local gates, but no post-fix live
artifact demonstrates a complete before/after movement across the whole
workload:

- #120 reported 535 tests plus Ruff formatting/lint and mypy.
- #125 and #130 reported 548 tests plus Ruff and mypy.
- #134, #136, #138, #139, #140, #141 and #143 reported their regression
  suites and static gates green; #140 reported 633 tests after rebase.
- #144 and #145 were open at this report's snapshot, though their head commits
  were present in the worktree lineage.
- #128 and #146 remained open.

These are repository facts about claimed verification at the respective PR
heads. They do not establish that the Node B deployment was upgraded, that the
same workload reran, or that the error-class/cost/delivery measurements moved.

## Known blind spots and hypotheses

1. **Session implementer telemetry (#128):** plan/implementation provider
   requests bypassed `ModelClient`; their tokens, cost, latency, rate limits
   and errors are unmeasured. This is a structural blind spot, not merely a
   missing query.
2. **No unified run boundary:** records span successive deployments and local
   direct runs. Cross-record percentages would mix populations.
3. **No raw integrity:** excerpts in issues may be accurate, but absent raw
   bytes/checksums prevent independent recomputation.
4. **No exact NGMS revision:** repository contents and external issue/PR state
   cannot be reconstructed at the same point in time.
5. **No external-state snapshot:** NGMS PR queries are live observations; the
   complete issue/branch/PR response bodies and repository ownership checks
   were not archived.
6. **No total spend:** `null`/empty cost output reflects missing traffic and
   unknown pricing, not free execution.
7. **Provider sweep inconsistency:** the reported failure classes account for
   32 of 34 non-answering models. Do not infer the missing two.
8. **Causal attributions:** claims such as disk pressure causing T27's missing
   binary are credible and motivated fixes, but without retained system logs
   they remain attributed live diagnoses rather than independently proven
   causes.
9. **Fleet success:** a later 3/3 deterministic local result and a single
   fallback success do not prove performance on NGMS or a real fleet.

## Gate result

| Stage 0 criterion | Result |
|---|---|
| Claims have a source or are labeled hypothesis/missing | **Pass for this reconstruction.** |
| Defects have a reproduction or source reference | **Pass.** See inventory and #113–#146 register. |
| Measurements state denominators | **Pass where a denominator survives; otherwise the number is rejected or marked missing.** |
| Exact collection command accompanies every accepted number | **Partial.** Commands missing from the historical record are explicitly identified; those numbers remain attributed live observations, not acceptance baselines. |
| Raw artifacts have checksums or immutable references | **Fail for the historical run.** Only GitHub source records and PR commits remain. |
| Known telemetry/executor blind spots are named | **Pass.** Session traffic and mixed executor populations are explicit. |
| Before/after movement is proved | **Fail.** No comparable follow-up run exists. |

**Continue:** implement Stage A using the defect inventory as scenario input.
**Stop:** do not claim Stage 0 has established a quantitative performance
baseline or that any merged fix changed live fleet outcomes. The next live run
must allocate a run ID, archive the secrets-redacted configuration, preserve
raw JSONL/SQLite/provider artifacts with checksums, record exact commands, and
publish a new report rather than modifying this one.
