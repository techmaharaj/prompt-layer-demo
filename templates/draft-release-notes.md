---
# A placeholder. The `attach` allowlist and `redact` rules below are a copy of
# templates/code-review.md's, which is the one worth reading: change a
# redaction rule and you are changing it in four files, not one. A real
# platform would factor these into a default every template inherits.
attach:
  - "*.md"
  - "*.txt"
redact:
  - pattern: "(?i)(api[_-]?key|secret|token|password)\\s*[:=]\\s*\\S+"
    replacement: "[redacted]"
---
Placeholder body for `draft-release-notes`. The catalogue needs more than one row to read
as a catalogue; only `code-review` is wired end to end in this repo.

{{ input }}
