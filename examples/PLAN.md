# Widget service

A worked example. Narrative like this paragraph is ignored — only items become
work, and everything skipped is reported so you can see what was left out.

## Background

More narrative. Not work.

## Dependencies

The graph can be stated in one place instead of repeated per item. The arrow
follows the work: `W1 -> W3` means W3 waits for W1.

```dependencies
W1 -> W3
```

### W1: Add a serial-number column

Add a `serial` column to the widgets table, unique and non-null, with a
migration. Two widgets must not be able to share a serial number.

labels: area:store
**Acceptance:** a migration exists and a test asserts the uniqueness constraint.

### W2: Reject duplicate serials at the API

Return 409 with a useful message when a widget is created with a serial that
already exists, rather than surfacing a database error.

depends on: W1
labels: area:api

### W3: Show serials in the listing

Add the serial to the widget list response and its test.

Its dependency on W1 is declared by the arrow block above rather than here.

labels: area:api

### W4: Announce the change

Update the changelog once the tracking issue is closed. An external target has
to name its kind and its resolver: nothing here can see the other system, so
`unresolved` is a blocker rather than an assumption.

depends on: W3, external:github-issue:owner/name#42
labels: area:docs
