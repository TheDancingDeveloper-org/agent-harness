"""Adapters: readers for logs the harness did not write.

Opt-in by design. The core understands its own event stream and nothing
else, so no workload inherits another project's file layout, and no adapter
can become a dependency of the core.

Each adapter's job is translation, and its hardest job is representing what
the source format *could not say*. See `oxidex` for the worked case: a text
log that recorded rate-limit errors without recording which kind they were,
where the honest translation is an explicit `unclassified`, never a guess.

Route presets live here too, for the same reason. `chat_completions` describes
a widely-served request shape; `claw_bay` describes one gateway's error
envelope. Core resolves them **by name** through the
``agent_harness.route_presets`` entry points this distribution declares, so a
build that never names one never imports one, and adding a vendor is a module
here rather than an edit to `model_client.py`.
"""
