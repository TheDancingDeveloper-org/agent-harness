# Evidence packages

Files in this directory are versioned, append-only evidence packages. Once a
report has been published, do not rewrite its measurements or silently repair
its gaps. A later run gets a new dated report (or an explicitly numbered
addendum) that links back to the earlier report.

Every reported number needs both a denominator and either the command that
produced it or an immutable source reference containing that command. Missing
run metadata, raw artifacts, checksums, commands and telemetry are recorded as
missing evidence, not inferred. Corrections are appended and retain the
superseded claim.

These reports distinguish:

- **repository fact**: reproducible from a named repository revision;
- **live observation**: captured from a deployment or provider;
- **hypothesis**: an explanation not yet established by a reproducible test or
  retained live artifact.

An evidence report records what was known at its publication point. It is not
a mutable dashboard or a second source of truth for the event store.
