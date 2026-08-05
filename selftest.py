#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py -- every check here is proven by breaking it first.

Each case builds a tiny repository in a temp dir, runs the gate, and asserts on
the exit code and the report. A check that cannot fail is not a check, so for
every "it passes" case there is a mutant that makes it fail.

Run:  python3 selftest.py          (no dependencies, no network)
"""
import io
import json
import os
import shutil
import sys
import tempfile
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# A runner has no PyYAML, this laptop does -- so the whole suite runs twice, and
# the second pass forces the built-in parser. CI found two divergences that the
# single local pass could not see; this is how they stay found.
NO_YAML = os.environ.get("CLAIM_CHECK_SELFTEST_NO_YAML") == "1"
if NO_YAML:
    sys.modules["yaml"] = None            # makes `import yaml` raise ImportError

import claim_check as cc  # noqa: E402

print("config parser under test: %s" % ("built-in (PyYAML blocked)" if NO_YAML
                                        else "PyYAML if installed"))

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print("  %-6s %s%s" % ("ok" if condition else "FAIL", name,
                           "" if condition else "  <- " + str(detail)))


@contextlib.contextmanager
def repo(files):
    root = tempfile.mkdtemp(prefix="claim-check-selftest-")
    try:
        for rel, body in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run(root, config="claims.yml", **kw):
    """Run the gate quietly; return (exit_code, stdout, report_dict)."""
    buf = io.StringIO()
    kw.setdefault("annotations", False)
    kw.setdefault("report_path", "report.json")
    with contextlib.redirect_stdout(buf):
        code = cc.run(config, root=root, **kw)
    report_path = os.path.join(root, "report.json")
    report = {}
    if os.path.isfile(report_path):
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    return code, buf.getvalue(), report


def status_of(report, cid):
    for row in report.get("claims", []):
        if row["id"] == cid:
            return row["status"]
    return "<absent>"


BASE_METRICS = json.dumps({"a": {"b": 12345}, "rate": 0.6197,
                           "rows": [{"n": 7}], "name": "nightly"})
BASE_CONFIG = """
defaults:
  doc: DOC.md
  json: metrics.json
claims:
  - id: total
    path: a.b
    format: comma
  - id: rate
    path: rate
    format: percent:1
    pattern: "rate is {value}% today"
"""
BASE_DOC = "Total: <!--claim:total-->12,345<!--/claim-->\n\nThe rate is 62.0% today.\n"


def base_files(doc=BASE_DOC, config=BASE_CONFIG, metrics=BASE_METRICS):
    return {"DOC.md": doc, "claims.yml": config, "metrics.json": metrics}


# --------------------------------------------------------------------------- #
print("\n1. the happy path, and the mutants that must break it")

with repo(base_files()) as root:
    code, out, report = run(root)
    check("clean repo exits 0", code == 0, out)
    check("both claims reported ok", report["ok"] == 2 and report["drift"] == 0, report)
    check("verdict line is last", out.strip().splitlines()[-1].startswith("CLAIM-CHECK:"), out)

with repo(base_files(doc=BASE_DOC.replace("12,345", "12,000"))) as root:
    code, out, report = run(root)
    check("MUTANT stale marker -> exit 1", code == 1, out)
    check("MUTANT stale marker -> DRIFT row", status_of(report, "total") == cc.DRIFT, report)
    check("MUTANT stale marker names both sides", "12,000" in out and "12,345" in out, out)

with repo(base_files(doc=BASE_DOC.replace("62.0%", "61.0%"))) as root:
    code, out, report = run(root)
    check("MUTANT stale sentence -> exit 1", code == 1, out)
    check("MUTANT stale sentence -> DRIFT row", status_of(report, "rate") == cc.DRIFT, report)

with repo(base_files(doc="nothing here\n")) as root:
    code, out, report = run(root)
    check("MUTANT deleted claim -> exit 1", code == 1, out)
    check("MUTANT deleted claim -> MISSING", status_of(report, "total") == cc.MISSING, report)

with repo(base_files(metrics=json.dumps({"a": {"b": 99999}, "rate": 0.6197,
                                         "rows": [{"n": 7}]}))) as root:
    code, out, report = run(root)
    check("MUTANT source moved on -> exit 1", code == 1, out)


print("\n2. a broken checker must not look like a clean result")

with repo({"DOC.md": BASE_DOC, "claims.yml": BASE_CONFIG}) as root:
    code, out, report = run(root)
    check("missing source file -> exit 2", code == 2, out)
    check("missing source file -> ERROR row", status_of(report, "total") == cc.ERROR, report)
    check("missing source file is named", "metrics.json" in out, out)

with repo(base_files(config=BASE_CONFIG.replace("path: a.b", "path: a.nope"))) as root:
    code, out, report = run(root)
    check("bad json path -> exit 2", code == 2, out)
    check("bad json path names the key", "nope" in out, out)

with repo(base_files(metrics="{not json")) as root:
    code, out, report = run(root)
    check("unparsable source -> exit 2", code == 2, out)

with repo({"DOC.md": BASE_DOC}) as root:
    code, out, report = run(root)
    check("no config file -> exit 2", code == 2, out)

with repo(base_files(config="defaults:\n  doc: DOC.md\n")) as root:
    code, out, report = run(root)
    check("config without claims: -> exit 2", code == 2, out)

with repo(base_files(config=BASE_CONFIG.replace("json: metrics.json",
                                                "json: metrics.json\n  value: 1"))) as root:
    code, out, report = run(root)
    check("two sources on one claim -> exit 2", code == 2, out)

with repo(base_files(doc=BASE_DOC.replace("12,345", "12,000"))) as root:
    code, out, report = run(root, fail_on_drift=False)
    check("--no-fail-on-drift turns DRIFT into exit 0", code == 0, out)
    check("--no-fail-on-drift still reports the drift", report["drift"] == 1, report)

with repo({"DOC.md": BASE_DOC, "claims.yml": BASE_CONFIG}) as root:
    code, out, report = run(root, fail_on_drift=False)
    check("--no-fail-on-drift cannot hide an ERROR", code == 2, out)


print("\n3. commands are off until you say otherwise")

CMD_CONFIG = """
claims:
  - id: total
    doc: DOC.md
    cmd: "echo 12345"
    format: comma
"""
with repo(base_files(config=CMD_CONFIG)) as root:
    code, out, report = run(root)
    check("cmd: refused by default -> exit 2", code == 2, out)
    check("refusal explains the fork risk", "fork" in out, out)
    code, out, report = run(root, allow_commands=True)
    check("cmd: works when allowed", code == 0, out)

with repo(base_files(config=CMD_CONFIG.replace('echo 12345', 'exit 3'))) as root:
    code, out, report = run(root, allow_commands=True)
    check("MUTANT failing command -> exit 2", code == 2, out)

JSON_CMD = """
claims:
  - id: total
    doc: DOC.md
    cmd: "echo '{\\"a\\": {\\"b\\": 12345}}'"
    path: a.b
    format: comma
"""
with repo(base_files(config=JSON_CMD)) as root:
    code, out, report = run(root, allow_commands=True)
    check("cmd: + path: parses JSON stdout", code == 0, out)


print("\n4. sources other than JSON")

TEXT_CONFIG = """
claims:
  - id: total
    doc: DOC.md
    text: build.log
    regex: "processed (\\\\d+) rows"
    format: comma
"""
with repo({"DOC.md": BASE_DOC, "claims.yml": TEXT_CONFIG,
           "build.log": "... processed 12345 rows ...\n"}) as root:
    code, out, report = run(root)
    check("text: + regex: reads a log", code == 0, out)

with repo({"DOC.md": BASE_DOC, "claims.yml": TEXT_CONFIG,
           "build.log": "nothing matched here\n"}) as root:
    code, out, report = run(root)
    check("MUTANT regex matches nothing -> exit 2", code == 2, out)

VALUE_CONFIG = """
claims:
  - id: total
    doc: DOC.md
    value: "12,345"
"""
with repo(base_files(config=VALUE_CONFIG)) as root:
    code, out, report = run(root)
    check("value: pins a constant", code == 0, out)


print("\n5. formatting is forgiven, arithmetic is not")

check("1234 == '1,234'", cc.matches("1,234", "1234", None))
check("0 is not 0.4", not cc.matches("0", "0.4", None))
check("tolerance 0.5 absorbs 0.4", cc.matches("12.0", "12.4", 0.5))
check("tolerance 0.5 rejects 0.6", not cc.matches("12.0", "12.6", 0.5))
check("tolerance 2% absorbs 1%", cc.matches("100", "101", "2%"))
check("tolerance 2% rejects 3%", not cc.matches("100", "103", "2%"))
check("text compares as text", cc.matches("nightly", "nightly", None))
check("different text is drift", not cc.matches("nightly", "weekly", None))
check("comma format", cc.render(1234567, "comma") == "1,234,567", cc.render(1234567, "comma"))
check("round format", cc.render(3.14159, "round:2") == "3.14")
check("percent format", cc.render(0.6197, "percent:1") == "62.0")
check("python spec format", cc.render(3.14159, "{:.3f}") == "3.142")
check("raw int stays int", cc.render(7, None) == "7")
check("whole float loses the .0", cc.render(7.0, None) == "7")
try:
    cc.render("nightly", "comma")
    check("MUTANT comma on text raises", False, "no exception")
except cc.ConfigError:
    check("MUTANT comma on text raises", True)


print("\n6. --fix rewrites the doc and nothing else")

drifted = BASE_DOC.replace("12,345", "12,000").replace("62.0%", "61.0%")
with repo(base_files(doc=drifted)) as root:
    code, out, report = run(root, fix=True)
    check("--fix exits 0", code == 0, out)
    with open(os.path.join(root, "DOC.md"), encoding="utf-8") as fh:
        fixed = fh.read()
    check("--fix restores the marker value", "<!--claim:total-->12,345<!--/claim-->" in fixed, fixed)
    check("--fix restores the sentence value", "rate is 62.0% today" in fixed, fixed)
    check("--fix keeps the prose intact", fixed == BASE_DOC, fixed)
    code, out, report = run(root)
    check("--fix leaves the repo green", code == 0, out)

TWICE = ("A: <!--claim:total-->12,345<!--/claim-->\n"
         "B: <!--claim:total-->12,000<!--/claim-->\n\nThe rate is 62.0% today.\n")
with repo(base_files(doc=TWICE)) as root:
    code, out, report = run(root)
    check("one bad copy of two -> DRIFT", code == 1 and status_of(report, "total") == cc.DRIFT, out)
    code, out, report = run(root, fix=True)
    with open(os.path.join(root, "DOC.md"), encoding="utf-8") as fh:
        fixed = fh.read()
    check("--fix repairs every copy", fixed.count("12,345") == 2, fixed)


print("\n7. the config parser without PyYAML")

HERE = os.path.dirname(os.path.abspath(__file__))
sample = os.path.join(HERE, "examples", "claims.yml")
if os.path.isfile(sample):
    with open(sample, encoding="utf-8") as fh:
        raw = fh.read()
    mine = cc.mini_yaml(raw, sample)
    try:
        import yaml
        theirs = yaml.safe_load(raw)
        check("mini parser agrees with PyYAML on examples/claims.yml", mine == theirs,
              json.dumps({"mini": mine, "yaml": theirs}, indent=1, default=str)[:600])
    except ImportError:
        check("PyYAML absent -- parity untested (mini parser still ran)", True)
    check("mini parser found every example claim", len(mine.get("claims", [])) == 6, mine)

nested = cc.mini_yaml("defaults:\n  doc: A.md\nclaims:\n  - id: x\n    value: 3\n  - id: y\n"
                      "    json: m.json\n    path: a.b\n")
check("mini parser: defaults block", nested["defaults"]["doc"] == "A.md", nested)
check("mini parser: two claims", len(nested["claims"]) == 2, nested)
check("mini parser: int stays int", nested["claims"][0]["value"] == 3, nested)
check("mini parser: comments stripped",
      cc.mini_yaml("a: 1 # trailing\n# whole line\nb: '2 # not a comment'\n")
      == {"a": 1, "b": "2 # not a comment"})
try:
    cc.mini_yaml("claims: [a, b]\n  weird\n")
    check("MUTANT unsupported syntax raises", False, "no exception")
except cc.ConfigError as exc:
    check("MUTANT unsupported syntax raises with a line number", "line" in str(exc), exc)

# The two divergences CI found on 2026-08-04: escapes inside double-quoted
# scalars. Checked against PyYAML itself whenever it is importable, so the two
# parsers cannot drift apart again unnoticed.
TRICKY = [
    'a: "processed (\\\\d+) rows"\n',
    'a: "echo \'{\\"k\\": 1}\'"\n',
    "a: 'literal \\d stays'\n",
    'a: "tab\\there"\n',
    "a: 'it''s quoted'\n",
    'a: "trailing space "\n',
]
try:
    import yaml as _real_yaml
except ImportError:
    _real_yaml = None
for snippet in TRICKY:
    mine = cc.mini_yaml(snippet)
    if _real_yaml is None:
        check("escape snippet parses: %r" % snippet.strip()[:28], isinstance(mine, dict))
    else:
        theirs = _real_yaml.safe_load(snippet)
        check("parser parity: %r" % snippet.strip()[:28], mine == theirs,
              "mini=%r yaml=%r" % (mine, theirs))
try:
    cc.mini_yaml('a: "bad \\q escape"\n')
    check("MUTANT unknown escape raises", False, "no exception")
except cc.ConfigError as exc:
    check("MUTANT unknown escape raises by name", "\\q" in str(exc), exc)


print("\n8. strict-unbacked: numbers bound to nothing (experimental)")

LOOSE = ("Total: <!--claim:total-->12,345<!--/claim--> and a stray 4,096 somewhere.\n\n"
         "The rate is 62.0% today. Released in 2026, version v1.2.3, see #4711.\n"
         "```\nignored 999999\n```\n")
with repo(base_files(doc=LOOSE)) as root:
    code, out, report = run(root, strict_unbacked=True)
    values = [u["value"] for u in report["unbacked"]]
    check("stray number is surfaced", "4,096" in values, values)
    check("claimed marker value is not surfaced", "12,345" not in values, values)
    check("claimed sentence value is not surfaced", "62.0" not in " ".join(values), values)
    check("year ignored", "2026" not in values, values)
    check("version ignored", "1.2" not in " ".join(values), values)
    check("issue ref ignored", "4711" not in values, values)
    check("fenced code ignored", "999999" not in values, values)
    check("unbacked numbers alone do not fail the build", code == 0, out)


print("\n9. what an external reviewer broke (Codex, 2026-08-04)")

ESCAPE = """
claims:
  - id: total
    doc: ../victim.md
    value: 1
"""
with repo(base_files(config=ESCAPE)) as root:
    victim = os.path.join(os.path.dirname(root), "victim.md")
    with open(victim, "w", encoding="utf-8") as fh:
        fh.write("Total: <!--claim:total-->999<!--/claim-->\n")
    try:
        code, out, report = run(root, fix=True)
        check("doc outside the repo -> exit 2", code == 2, out)
        check("escape refusal is named", "escapes the repository" in out, out)
        with open(victim, encoding="utf-8") as fh:
            check("the file outside the repo is untouched", "999" in fh.read())
    finally:
        os.remove(victim)

ABS = "claims:\n  - id: total\n    doc: /etc/hosts\n    value: 1\n"
with repo(base_files(config=ABS)) as root:
    code, out, report = run(root)
    check("absolute doc path -> exit 2", code == 2, out)

ESCAPE_SRC = "claims:\n  - id: total\n    doc: DOC.md\n    json: ../secrets.json\n    path: a\n"
with repo(base_files(config=ESCAPE_SRC)) as root:
    code, out, report = run(root)
    check("source outside the repo -> exit 2", code == 2, out)

DUP = """
defaults:
  doc: DOC.md
  json: metrics.json
claims:
  - id: total
    path: a.b
    format: comma
  - id: total
    value: 999
"""
with repo(base_files(config=DUP)) as root:
    code, out, report = run(root, fix=True)
    check("two claims on one marker -> exit 2", code == 2, out)
    check("the collision is named", "duplicate claim id" in out, out)
    with open(os.path.join(root, "DOC.md"), encoding="utf-8") as fh:
        check("a config collision does not rewrite the doc", "12,345" in fh.read())

SHARED = ("The rate is 62.0% today.\nElsewhere: the rate is 40.0% today.\n"
          "Total: <!--claim:total-->12,345<!--/claim-->\n")
with repo(base_files(doc=SHARED)) as root:
    code, out, report = run(root)
    check("a pattern binds every occurrence -> DRIFT", code == 1, out)
    code, out, report = run(root, fix=True)
    with open(os.path.join(root, "DOC.md"), encoding="utf-8") as fh:
        fixed = fh.read()
    check("--fix sets every occurrence to the source value",
          fixed.count("rate is 62.0% today") == 2, fixed)

CRLF = BASE_DOC.replace("\n", "\r\n")
with repo(base_files(doc=CRLF)) as root:
    code, out, report = run(root)
    check("CRLF line endings still pass", code == 0, out)
with repo(base_files(doc=CRLF.replace("12,345", "12,000"))) as root:
    code, out, report = run(root, fix=True)
    with open(os.path.join(root, "DOC.md"), "r", newline="", encoding="utf-8") as fh:
        fixed = fh.read()
    check("--fix does not eat CRLF", "\r\n" in fixed and "12,345" in fixed, repr(fixed[:60]))

check("as_number rejects a date", cc.as_number("2026-08-04") is None)
check("as_number rejects a range", cc.as_number("10-20") is None)
check("as_number rejects a version", cc.as_number("1.2.3") is None)
check("as_number rejects a bool", cc.as_number(True) is None)
check("as_number reads a signed float", cc.as_number("-3.5") == -3.5)

SLOW = 'claims:\n  - id: total\n    doc: DOC.md\n    cmd: "sleep 30"\n    timeout: 1\n'
with repo(base_files(config=SLOW)) as root:
    code, out, report = run(root, allow_commands=True)
    check("a hanging cmd is killed, not waited on -> exit 2", code == 2, out)
    check("the timeout is named", "timeout" in out.lower(), out)


print("\n10. the report file")

with repo(base_files(doc=BASE_DOC.replace("12,345", "12,000"))) as root:
    code, out, report = run(root)
    check("report has counts", report["checked"] == 2 and report["drift"] == 1, report)
    check("report names the line", any(r.get("line") for r in report["claims"]), report)
    check("report carries the version", report["version"] == cc.__version__, report)


print("\n11. the installed console script keeps the exit-code contract")
# `python3 claim_check.py` runs under a __main__ guard; `pip install claim-check`
# installs a console script that never sees that guard. So the "ConfigError ->
# exit 2" rule has to live inside cli(), or a pip user meets a traceback and
# exit 1 -- the code the contract reserves for DRIFT, which would make a dead
# checker look like a drifted document.
#
# Every ConfigError raised today is already caught inside run(), so no real
# input reaches this path. That is exactly why the escape is injected here: an
# unreachable guard still has to be a working guard, and an untested one is not
# a guard at all.


def _main_that_escapes(argv=None):
    raise cc.ConfigError("escaped from main")


_real_main = cc.main
try:
    cc.main = _main_that_escapes
    check("cli() converts an escaped ConfigError into ERROR",
          cc.cli([]) == cc.EXIT_ERROR, cc.cli([]))
    # the mutant: without the wrapper the same input raises instead of returning
    escaped = False
    try:
        cc.main([])
    except cc.ConfigError:
        escaped = True
    check("main() alone would not -- the wrapper is load-bearing", escaped)
finally:
    cc.main = _real_main

# Found by an external reviewer on the packaging pass: a config that is not
# UTF-8 used to escape as a UnicodeDecodeError and exit 1 -- DRIFT -- so a
# broken checkout was reported as a changed document.
with repo(base_files()) as root:
    with open(os.path.join(root, "claims.yml"), "wb") as fh:
        fh.write(b"\xff\xfeclaims:\n")
    code, out, _ = run(root)
    check("a non-UTF-8 config is ERROR, not DRIFT", code == cc.EXIT_ERROR, (code, out))
    check("and it names the file and the reason", "not UTF-8" in out, out)

with repo(base_files()) as root:
    with open(os.path.join(root, "metrics.json"), "wb") as fh:
        fh.write(b'{"a": {"b": 1\xff2}}')
    code, out, _ = run(root)
    check("a non-UTF-8 source is ERROR, not DRIFT", code == cc.EXIT_ERROR, (code, out))

with repo(base_files()) as root:
    with open(os.path.join(root, "DOC.md"), "wb") as fh:
        fh.write(b"Total: <!--claim:total-->12,\xff345<!--/claim-->\n")
    code, out, _ = run(root)
    check("a non-UTF-8 doc is ERROR, not DRIFT", code == cc.EXIT_ERROR, (code, out))

# ...and nothing at all may reach the caller as an exit 1 it did not choose.
_real_run = cc.run
try:
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    cc.run = _boom
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        crash_code = cc.cli(["--config", "claims.yml"])
    check("an unexpected crash is ERROR, not DRIFT", crash_code == cc.EXIT_ERROR,
          (crash_code, buf.getvalue()))
    check("the crash still ends with a verdict line",
          buf.getvalue().strip().splitlines()[-1].startswith("CLAIM-CHECK:"), buf.getvalue())
finally:
    cc.run = _real_run

with io.open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8") as fh:
    _pyproject = fh.read()
check("pyproject.toml wires the console script to cli, not main",
      'claim-check = "claim_check:cli"' in _pyproject)
check("the packaged version is the module version",
      'path = "claim_check.py"' in _pyproject)

SECOND_PASS_FAILED = False
if not NO_YAML:
    print("\n12. the same suite again, with PyYAML blocked")
    import subprocess
    env = dict(os.environ, CLAIM_CHECK_SELFTEST_NO_YAML="1")
    proc = subprocess.run([sys.executable, os.path.abspath(__file__)], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-3:]
    for line in tail:
        print("  | " + line)
    SECOND_PASS_FAILED = proc.returncode != 0
    check("built-in parser pass is green", not SECOND_PASS_FAILED,
          "\n".join(proc.stdout.splitlines()[-25:]))

print("\n%d checks: %d ok, %d failed" % (len(PASSED) + len(FAILED), len(PASSED), len(FAILED)))

# The README claims how many checks live here. This file is that claim's source,
# and .github/claims.yml binds the two -- the gate is held to its own rule.
with open(os.path.join(HERE, "selftest-report.json"), "w", encoding="utf-8") as fh:
    json.dump({"checks": len(PASSED) + len(FAILED), "ok": len(PASSED),
               "failed": len(FAILED), "version": cc.__version__}, fh, indent=2)

if FAILED:
    print("FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("SELFTEST: all green")
