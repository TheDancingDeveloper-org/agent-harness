# Widget service

This is a parser-compatible, local-only worked example. It includes the
human-facing sections and version-one executable manifest required by the
target contract in [`minimal.md`](../minimal.md).

## Project identity and brief

- Project key: `widgets`
- Local repository: supplied when the plan is admitted
- Brief: add unique widget serial numbers to an existing local service and show
  them through its API.

## Scope and non-goals

### In scope

- Persist a unique serial number for every widget.
- Reject duplicate serial numbers at the API boundary.
- Return serial numbers from the existing list endpoint.
- Document the local change.

### Non-goals

- No new authentication scheme.
- No remote repository, hosted CI/CD, or non-local deployment work.

## Rough architecture

The existing service has a persistence layer, an HTTP API, and automated tests.
The migration changes persistence first; API validation and response changes
depend on that schema. The changelog describes the accepted integrated result.

## Data model

`Widget` gains a non-null `serial` string with a uniqueness constraint. The
change requires a forward migration and a safe failure for existing duplicate
or missing data. No other entity changes ownership or lifecycle.

## GUI and interfaces

There is no GUI in this plan. The existing create and list HTTP endpoints are
the user-facing interfaces. Duplicate creation returns HTTP 409 with a useful
error, and list results include `serial`.

## Execution and local delivery

The runner uses the repository's locked Python toolchain and no supporting
service or secret. Checks run without network access. Delivery is a local
process owned by the harness; readiness and acceptance run on the host.

```harness
version = 1

[project]
key = "widgets"
name = "Widget service"

[repository]
base_ref = "main"
integration_ref = "harness/widgets"

[agents]
role_runner = "agent-loop"
required_roles = ["implementer", "reviewer"]
max_workers = 2
max_attempts = 5
max_item_seconds = 3600
max_item_spend_usd = 0.0
max_hold_seconds = 21600

[execution]
backend = "docker"
network = "none"
toolchains = ["Python 3.12"]
system_packages = []

[execution.image]
strategy = "existing"
reference = "example/runner@sha256:0000000000000000000000000000000000000000000000000000000000000000"
pull = false

[execution.limits]
command_timeout_seconds = 300
memory = "2g"
cpus = "2"
pids = 512
user = "1000:1000"
rootfs_read_only = true
tmpfs_size = "512m"

[[execution.probes]]
name = "python"
command = ["python", "--version"]
expect_regex = "^Python 3\\.12"

[checks]
item = [["python", "-m", "pytest", "-q"]]
integration = [["python", "-m", "pytest", "-q"]]

[local_delivery]
backend = "local-process"
build = ["python", "-m", "build"]
build_context = "runner"
readiness = ["python", "scripts/readiness.py"]
readiness_context = "host"
acceptance = ["python", "-m", "pytest", "-q", "acceptance"]
acceptance_context = "host"
required_host_tools = ["python"]
command_timeout_seconds = 900
readiness_timeout_seconds = 120
readiness_interval_seconds = 2
```

## Cross-cutting requirements

- Security: error responses must not expose database or query details.
- Privacy: serial numbers are product identifiers, not personal data.
- Accessibility: not applicable; this plan adds no GUI.
- Performance: the list endpoint must not add a per-row query.
- Observability: duplicate rejections use the service's existing structured
  error event without logging request credentials.
- Compatibility: preserve all existing API fields and behaviours.
- Licensing: use only dependencies permitted by the repository's existing
  licence policy.

## Work items

The dependency block follows the work: `W1 -> W3` means W3 waits for W1.

```dependencies
W1 -> W3
```

### W1: Add a serial-number column

**Deliverable:** A migration and persistence model that require a unique,
non-null widget serial number.

**Acceptance:**

- The migration applies to a clean and representative existing local database.
- A test proves two widgets cannot share a serial number.
- A failed migration leaves the original local database recoverable.

Dependencies: none.

labels: area:store

### W2: Reject duplicate serials at the API

**Deliverable:** The create endpoint returns a stable conflict response for a
serial number that already exists.

**Acceptance:**

- An API test expects HTTP 409 and the documented error code.
- The response contains no database implementation detail.

depends on: W1

labels: area:api

### W3: Show serials in the listing

**Deliverable:** The existing widget list response includes each serial number.

**Acceptance:**

- The response-schema and endpoint tests include `serial`.
- The query-count assertion proves the change adds no per-row query.

depends on: W1

Its dependency is also declared by the graph block above; repeated declarations
must agree.

labels: area:api

### W4: Document the accepted local change

**Deliverable:** The local changelog describes the serial-number behaviour and
the compatibility impact.

**Acceptance:**

- The entry links the schema, conflict response, and list-response changes.
- Documentation checks pass on the integrated local branch.

depends on: W2, W3

labels: area:docs

## Local definition of done

- W1–W4 are accepted and promoted into the local plan branch.
- Integrated checks pass from the exact accepted commit.
- The service builds, deploys locally, becomes ready, and passes the API
  acceptance suite.
- Teardown succeeds and evidence names the plan revision, accepted commit,
  execution profile, commands, and outcomes.
- No remote repository or deployment state is changed.

## Assumptions, risks, and open questions

- Assumption: the repository already has a migration and local service test
  convention; preflight must reject the plan if it does not.
- Risk: existing data may lack valid serials. The migration item owns detection,
  a documented remediation path, and recovery testing.
- Open questions: none. A discovered material ambiguity becomes a batched plan
  question before admission or a durable item hold after admission.
