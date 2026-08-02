# Widget service

A worked example. Narrative like this paragraph is ignored — only items become
work, and everything skipped is reported so you can see what was left out.

## Background

More narrative. Not work.

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

depends on: W1
labels: area:api
