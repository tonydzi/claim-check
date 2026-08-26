# claim-check

**Every number you publish is a claim. Prove it in CI.**

[![selftest](https://github.com/Palo-Alto-AI-Research-Lab/claim-check/actions/workflows/selftest.yml/badge.svg)](https://github.com/Palo-Alto-AI-Research-Lab/claim-check/actions/workflows/selftest.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/Palo-Alto-AI-Research-Lab/claim-check/blob/main/LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](#requirements)
[![deps: none](https://img.shields.io/badge/dependencies-none-lightgrey.svg)](#requirements)
[![no LLM](https://img.shields.io/badge/LLM-not%20used-lightgrey.svg)](#what-this-is-not)

A README says *"handles 12,000 req/s"*, *"44 tests"*, *"0 known CVEs"*, *"61% autonomous"*.
The benchmark was rerun in March, the suite grew to 96 checks, a CVE landed in May.
Nothing in CI noticed, because nothing in CI knew the number was ever tied to anything.

`claim-check` binds each published number to the artifact it came from — a JSON file, a
log, a command's output — and recomputes it on every run. When the doc and the artifact
disagree, the build fails and names both sides.

```
| claim            | doc                | status    | doc says | source says |
|------------------|--------------------|-----------|----------|-------------|
| `unique-events`  | examples/REPORT.md | **DRIFT** | 700      | 744         |
| `autonomous-rate`| examples/REPORT.md | **DRIFT** | 61.9     | 61.97       |
| `tier2-total`    | examples/REPORT.md | **PASS**  | 17       | 17          |

what went wrong:
  DRIFT    unique-events    doc says 700, source says 744  (examples/REPORT.md:4)
  DRIFT    autonomous-rate  doc says 61.9, source says 61.97  (examples/REPORT.md:7)

CLAIM-CHECK: 6 claims - 4 ok - 2 drift - 0 errors -> DRIFT
```

No LLM, no API key, no network, no dependencies. The verdict is an exit code.

## Install

Two ways to run the same gate, and they share one file.

**In CI, as a GitHub Action** — nothing to install:

```yaml
- uses: Palo-Alto-AI-Research-Lab/claim-check@v1
  with:
    claims-file: .github/claims.yml
```

**On your machine, as a command** — for checking a doc before you push it:

```bash
pip install "git+https://github.com/Palo-Alto-AI-Research-Lab/claim-check"
claim-check --config .github/claims.yml
```

<!-- pypi-install-marker -->

> **Note the hyphen.** `claimcheck` (no hyphen) on PyPI is an unrelated project
> about retrieval-based fact-checking of prose. This one is `claim-check`, and it
> only ever asks whether a number still comes out of the artifact you bound it to.

The command and the Action run the same `claim_check.py` and honour the same
[exit-code contract](#exit-code-contract). PyYAML is optional in both
(`pip install "claim-check[yaml] @ git+…"` if your claims file needs full YAML);
without it a small strict parser handles the file and refuses, by line number,
anything it does not understand.

## Quickstart (three files)

**1. `.github/workflows/claims.yml`**

```yaml
name: claims
on: [push, pull_request]

jobs:
  claim-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Palo-Alto-AI-Research-Lab/claim-check@v1
        with:
          claims-file: .github/claims.yml
```

**2. `.github/claims.yml`** — what each number is bound to

```yaml
defaults:
  doc: README.md
  json: benchmarks/latest.json

claims:
  - id: throughput
    path: results.requests_per_second
    format: comma

  - id: pass-rate
    path: suite.pass_rate
    format: percent:1
    tolerance: 0.1
    pattern: "pass rate of {value}%"
```

**3. `README.md`** — the numbers themselves

```markdown
Handles <!--claim:throughput-->12,000<!--/claim--> req/s
at a pass rate of 99.4%.
```

That is the whole setup. `throughput` is checked through its marker, `pass-rate`
through the sentence it lives in.

## Two ways to bind a number

| | how the doc looks | when to use it |
|---|---|---|
| **marker mode** (default) | `<!--claim:id-->12,000<!--/claim-->` | you control the doc; unambiguous, survives rewording |
| **pattern mode** (`pattern:`) | `pass rate of 99.4%` | you want the doc free of markup; the sentence is the anchor |

Both modes support `--fix`. Both report the exact line number.

One rule worth knowing before you write a pattern: **a claim binds every occurrence of
its marker or pattern in the doc.** Three sentences matching `"pass rate of {value}%"`
are three copies of one claim — they must all agree with the source, and `--fix` sets all
three. If two sentences share a skeleton but mean different numbers, make the patterns
more specific or give each its own marker.

## Sources

Exactly one per claim:

| key | reads | needs |
|---|---|---|
| `json: path.json` | a value out of JSON | `path:` — dotted, with `[i]` indices (`runs[0].passed`) |
| `text: build.log` | a value out of any text | `regex:` with one capture group |
| `cmd: "..."` | a command's stdout | `allow-commands` — see [security](#security-cmd-and-forks) |
| `value: 17` | a constant you pin by hand | — |

`cmd:` also accepts `path:` (parses stdout as JSON) or `regex:`, and is killed after
60 seconds unless the claim raises it with `timeout: 300`.

## Formats and tolerance

The source is the truth; the doc is allowed to be readable.

| `format:` | `0.6197` becomes | | |
|---|---|---|---|
| *(none)* | `0.6197` | `comma` | `1,234,567` |
| `round:2` | `0.62` | `percent:1` | `62.0` |
| `{:.3f}` (any Python format spec) | `0.620` | | |

`percent:N` multiplies by 100, so it expects a fraction. If your artifact already stores
`62.0`, use `round:1` and keep the `%` in the surrounding text.

Formatting differences are forgiven — `1234` in the artifact matches `1,234` in the doc.
Arithmetic differences are not, unless you allow them explicitly:

```yaml
tolerance: 0.5     # absolute
tolerance: "2%"    # relative
```

## Exit-code contract

```
0  every claim reproduces
1  DRIFT   -- a number no longer matches its source, or vanished from the doc
2  ERROR   -- the check itself could not run: missing config, missing source, bad path
```

The last line of stdout is always a one-line verdict, so a wrapper can read it without
parsing anything.

`fail-on-drift: false` downgrades **1** to **0** when you want a warning-only rollout.
It cannot downgrade **2**: a checker that could not read its source must never look like
a clean result. That is the whole point of the tool applied to itself.

## Fixing instead of failing

```yaml
      - uses: Palo-Alto-AI-Research-Lab/claim-check@v1
        with:
          fix: 'true'
          fail-on-drift: 'false'
      - uses: peter-evans/create-pull-request@v6   # or commit it yourself
        with:
          commit-message: 'docs: refresh published numbers'
```

`--fix` rewrites only the claimed values — every occurrence, in place. The selftest
asserts the rest of the file comes out byte-identical.

## Security: `cmd:` and forks

`cmd:` runs arbitrary shell, and on `pull_request` the claims file is checked out **from
the fork**. So `cmd:` is refused unless you pass `allow-commands: true`, and the refusal
says why. Recommended split:

- `pull_request` workflows: leave `allow-commands` off, use `json:` / `text:` sources;
- `push` on your own branches: turn it on if you need it.

Even with `allow-commands: true`, the action **refuses to run commands when the pull
request comes from a fork** — that combination is a shell handed to a stranger. Override
it with `trust-fork-commands: true` only if you have read the sentence before this one
twice.

Two more things follow from the claims file being attacker-controlled on a fork PR:

- **every path is confined to the checkout.** `doc: ../../etc/passwd` or any absolute
  path is refused by name, so `--fix` cannot write outside the workspace;
- **a command cannot idle the runner.** `cmd:` is killed after 60 seconds (`timeout:`
  per claim) and a killed command is an ERROR, not a pass.

Nothing else in this action touches the network, and it never needs a token.

## Unbacked numbers (experimental)

`strict-unbacked: true` also flags numbers that are bound to *nothing* — the house rule
that a figure without a source is a guess. It ignores versions, years, times, issue
references and fenced code, and it only warns; it never fails the build. It is noisy on
purpose. Tune it with:

```yaml
unbacked_ignore:
  - "\\b\\d+ms\\b"
```

## Running it locally

```bash
git clone https://github.com/Palo-Alto-AI-Research-Lab/claim-check
cd claim-check
python3 selftest.py                                  # 108 checks, every one broken by a mutant first
python3 claim_check.py --config examples/claims.yml  # the worked example
```

The selftest count above is itself a claim: `selftest.py` writes
`selftest-report.json`, [`.github/claims.yml`](https://github.com/Palo-Alto-AI-Research-Lab/claim-check/blob/main/.github/claims.yml) binds this README to
it, and CI runs the gate on its own repository. If someone adds a check and forgets this
line, the build goes red.

## What this is not

- **Not an LLM documentation reviewer.** Tools that ask a model *"does this prose still
  describe the code?"* answer a broader question, cost tokens, and answer differently on
  Tuesday. `claim-check` answers one narrow question deterministically: *does this exact
  number still come out of that exact artifact?* The two sit well together.
- **Not a metrics collector.** It does not run your benchmark. It checks that what you
  published matches what your benchmark last wrote down.
- **Not a linter.** It has no opinion about your prose.

## Where it came from

The lab publishes measurements about its own agent fleet, and it runs on one rule:
*a number is a claim, and a claim without a reproducible source is a guess.* The first
version of this was a frozen ledger snapshot with a `verify_claims.py` shipped next to
the report — ten headline numbers that either reproduced or printed `DRIFT`. Every number
in the writeup had to survive it before publication.

This action is that pattern generalized, so it can sit in anyone's CI.

Related gates from the same practice:

- [verified-ops-starter](https://github.com/Palo-Alto-AI-Research-Lab/verified-ops-starter) —
  your scheduled job says `exit 0`; prove it did the work.
- [verbatim-citation-gate](https://github.com/Palo-Alto-AI-Research-Lab/verbatim-citation-gate) —
  catch fabricated RAG citations before they reach the user.

## Roadmap

**Now — [v1.0.1](https://github.com/Palo-Alto-AI-Research-Lab/claim-check/releases).** The action
itself: bind a number in prose to the artifact that produced it, `cmd:` and file sources, tolerance
and format handling, an exit-code contract, fix-instead-of-fail, and the experimental unbacked-number
pass.

**Next**, in the order we would take them:

- **The Marketplace listing.** The action works and is public; it is *not* listed, because listing
  needs a legal agreement and 2FA that only the account owner can complete. "Not listed" and "not
  working" are different things, and this is the first.
- **More source kinds.** Today a claim binds to a file or a command. JSON pointers into a results
  file are the most-asked next one.
- **A quieter first run.** The unbacked-number pass is experimental precisely because on a real
  README it finds more than you want on day one.

Versioning is semver and **every noticeable change ships as a new release** — for an action that
gates other people's CI, the release feed is the only honest way to see what changed under you
before you bump the tag you depend on.

## Requirements

Python 3.8+ on the runner. Nothing else. PyYAML is used if it happens to be installed;
otherwise a small strict parser handles the claims file and refuses, by line number,
anything it does not understand.

## When it breaks

| symptom | cause | fix |
|---|---|---|
| `ERROR ... source file not found` | the artifact is generated, not committed | generate it in a step **before** this one, or commit a snapshot |
| `MISSING ... no marker in README.md` | the marker was reworded or deleted | restore `<!--claim:id-->…<!--/claim-->`, or switch that claim to `pattern:` |
| `ERROR ... path 'a.b': no key 'b'` | the artifact's schema changed | update `path:` — the number moved, the claim did not |
| `ERROR ... cmd: is disabled` | `cmd:` source without opt-in | set `allow-commands: true`, having read [security](#security-cmd-and-forks) |
| `ERROR ... duplicate claim id` | two claims aim at one place in one doc | give them separate ids, or one claim and one marker |
| `ERROR ... path escapes the repository` | a `doc:` / `json:` / `text:` path leaves the checkout | keep every path relative and inside the repo |
| `ERROR ... cmd exceeded 60s` | the command hangs (waiting on a prompt, a lock, the network) | fix the command, or raise `timeout:` on that claim |
| passes locally, fails in CI | local artifact is newer than the committed one | regenerate the artifact in the workflow before the gate |

## License

MIT — see [LICENSE](https://github.com/Palo-Alto-AI-Research-Lab/claim-check/blob/main/LICENSE).

---

<!--ecosystem-map:start-->

## 🧩 One piece of a working system

This repository is one piece lifted out of a live operation: one non-technical founder, an AI
cofounder, and a fleet of machines that reach consensus with each other and wake the human only
for money or the irreversible. It was extracted after it survived production, not written as a
demo — and it runs on its own: nothing here phones home to the rest.

**See how the whole thing fits together → [SYSTEM.md](https://github.com/tonydzi/tonydzi/blob/main/SYSTEM.md)**

Its closest neighbours in the **gates** layer: [`break-it-first`](https://github.com/tonydzi/break-it-first) · [`verbatim-citation-gate`](https://github.com/tonydzi/verbatim-citation-gate) · [`verdict-contract`](https://github.com/tonydzi/verdict-contract)

<!--ecosystem-map:end-->

## AI contributors

This project is built by a human + AI team, and the git log says so: Claude writes most of
the code, Codex and Grok review it, Gemini feeds the research. Each is credited on a commit
**only if its output changed that commit's content** — no decorative credits. Lab-wide
policy, one source for every repo: [AI-CONTRIBUTORS.md](https://github.com/Palo-Alto-AI-Research-Lab/.github/blob/main/AI-CONTRIBUTORS.md).
