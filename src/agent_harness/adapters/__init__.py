"""Adapters: readers for logs the harness did not write.

Opt-in by design. The core understands its own event stream and nothing
else, so no workload inherits another project's file layout, and no adapter
can become a dependency of the core.

Each adapter's job is translation, and its hardest job is representing what
the source format *could not say*. See `oxidex` for the worked case: a text
log that recorded rate-limit errors without recording which kind they were,
where the honest translation is an explicit `unclassified`, never a guess.
"""
