# Changelog

## v1.1.0 - 2026-08-05

**Installable as a command.** `pip install claim-check` gives a `claim-check` CLI that
runs the same file as the Action and honours the same exit-code contract — for checking a
doc before you push it, not only after. The Action is unchanged and keeps working exactly
as before.

**A crash no longer masquerades as DRIFT.** Found by an external reviewer on the packaging
pass: a config, source, or doc that is not valid UTF-8 escaped as a `UnicodeDecodeError`
and exited **1** — the code reserved for *"your documented number changed"*. CI would have
blamed the document for a broken checkout. Now:

- every text read goes through one `read_text()` that reports an unreadable file as ERROR,
  naming the file and the byte;
- a per-claim read failure is one ERROR row, not a dead run;
- and as a backstop, *any* uncaught exception exits 2 with its traceback intact, because a
  broken checker must never be reported as a drifted document.

The suite grew 98 → 108 checks, each one proven by a mutant that makes it fail, and a new
CI job installs the built wheel into a bare environment and asserts all three exit codes
through the installed console script — the wheel is a different artifact from the
checkout, and nothing was testing it.

## v1.0.1 - 2026-08-04

Fixes a bug that only appeared on a real runner: without PyYAML installed, the built-in
config parser did not process escapes inside double-quoted strings, so
`regex: "processed (\\d+) rows"` became a pattern looking for a literal backslash and the
claim failed for a reason unrelated to the number. Escapes are now handled, and an escape
the parser does not know is refused by name instead of being passed through.

The blind spot mattered more than the bug: the local machine has PyYAML, so the built-in
parser was never exercised by a full run. `selftest.py` now runs the entire suite twice,
the second time with PyYAML blocked, and asserts both parsers agree on the awkward strings.

## v1.0.0 - 2026-08-04

First public release.

- Two ways to bind a number to its source: `<!--claim:id-->` markers and `pattern:` sentences.
- Sources: `json:` + `path:`, `text:` + `regex:`, `cmd:` (opt-in), `value:`.
- Formats (`comma`, `round:N`, `percent:N`, any Python format spec) and `tolerance` (absolute or `"N%"`).
- `--fix` rewrites drifted values in place, every occurrence, leaving the rest of the file byte-identical.
- Exit-code contract 0 / 1 / 2, where an unreadable source can never present as a clean result.
- Workflow annotations with file and line, a job-summary table, and an optional JSON report.
- `--strict-unbacked` (experimental) flags numbers bound to no source at all.
- No dependencies: PyYAML is used when present, otherwise a strict small parser that refuses
  unsupported syntax by line number.

Hardened before release, after an adversarial review pass found three ways to break it:

- every config-supplied path is confined to the checkout, so `doc: ../victim.md` with
  `--fix` can no longer write outside the workspace;
- two claims aiming at the same place in the same doc are an ERROR instead of both being
  reported clean while one silently overwrote the other, and `--fix` now re-verifies from
  disk rather than assuming its own rewrite landed;
- `cmd:` is killed after 60 seconds (`timeout:` per claim) instead of idling the runner.

Each has a mutant in `selftest.py`.
