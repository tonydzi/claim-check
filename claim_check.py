#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
claim_check.py -- every number you publish is a claim. Prove it in CI.

A deterministic gate: each number in your docs is bound to the artifact it came
from (a JSON file, a text report, or a command's output). On every run the number
is recomputed and compared with what the doc actually says.

No LLM, no API key, no network, no dependencies. The verdict is an exit code.

CONTRACT
    python3 claim_check.py --config .github/claims.yml
    exit 0 -- every claim reproduces
    exit 1 -- DRIFT (a doc number no longer matches its source, or vanished)
    exit 2 -- ERROR (config broken, source missing, path not found)

The LAST line of stdout is always a single-line verdict.

A dead checker must not look like a clean result: any failure to READ a source
is exit 2, never a silent pass. That is the same rule as the exit-code contract
in verified-ops-starter, and it is why `--no-fail-on-drift` still cannot turn an
ERROR into a zero.
"""
import argparse
import json
import os
import re
import subprocess
import sys

__version__ = "1.1.0"

OK, DRIFT, MISSING, ERROR = "OK", "DRIFT", "MISSING", "ERROR"

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2


class ConfigError(Exception):
    """The config, not the docs, is what is broken."""


# --------------------------------------------------------------------------- #
# config loading: PyYAML when present, otherwise a strict small parser
# --------------------------------------------------------------------------- #

def read_text(path, what, newline=None):
    """Read a UTF-8 file, or say why not in the one language this tool speaks.

    A file that is unreadable -- wrong encoding, a permission bit, a broken
    symlink -- is an ERROR, not a DRIFT. Letting the decoder raise would exit 1,
    and exit 1 means "your documented number changed", which is a lie about the
    document and hides a broken checkout.
    """
    try:
        with open(path, "r", encoding="utf-8", newline=newline) as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise ConfigError("%s is not UTF-8 text (%s at byte %d)"
                          % (what, exc.reason, exc.start))
    except (IOError, OSError) as exc:
        raise ConfigError("cannot read %s: %s" % (what, exc))


def load_config(path):
    if not os.path.isfile(path):
        raise ConfigError("no config file: %s" % path)
    raw = read_text(path, path)
    if path.endswith(".json"):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ConfigError("%s is not valid JSON: %s" % (path, exc))
    try:
        import yaml  # optional; absent on a bare runner
    except ImportError:
        return mini_yaml(raw, path)
    try:
        data = yaml.safe_load(raw)
    except Exception as exc:                      # noqa: BLE001 -- surface it
        raise ConfigError("%s is not valid YAML: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ConfigError("%s must be a mapping at the top level" % path)
    return data


_SCALAR = re.compile(r"^(?P<key>[A-Za-z_][\w.-]*)\s*:\s*(?P<val>.*)$")


def mini_yaml(raw, path="<config>"):
    """A deliberately small YAML subset: mappings, lists of mappings, scalars.

    Anything else (anchors, flow collections, multi-line strings) is refused BY
    NAME with a line number instead of being half-parsed. Install PyYAML and the
    full grammar is used instead -- this exists so the gate runs on a runner with
    nothing installed.
    """
    lines = []
    for no, text in enumerate(raw.splitlines(), 1):
        stripped = _strip_comment(text)
        if stripped.strip():
            lines.append((no, stripped))

    def indent_of(text):
        return len(text) - len(text.lstrip(" "))

    def fail(no, text, why):
        raise ConfigError(
            "%s line %d: %s -- %r. Install PyYAML (pip install pyyaml) if you "
            "need full YAML." % (path, no, why, text.strip())
        )

    pos = [0]

    def parse_block(indent):
        if pos[0] >= len(lines):
            return None
        no, text = lines[pos[0]]
        if text.lstrip().startswith("- "):
            return parse_list(indent)
        return parse_map(indent)

    def parse_list(indent):
        out = []
        while pos[0] < len(lines):
            no, text = lines[pos[0]]
            cur = indent_of(text)
            if cur < indent or not text.lstrip().startswith("- "):
                break
            if cur > indent and out:
                fail(no, text, "unexpected indent inside a list")
            indent = cur
            body = text.lstrip()[2:]
            pos[0] += 1
            match = _SCALAR.match(body.strip())
            if match and not body.strip().startswith("-"):
                item = {}
                key, val = match.group("key"), match.group("val").strip()
                item[key] = scalar(val) if val else parse_block(cur + 2)
                item.update(parse_map(cur + 2, existing=True) or {})
                out.append(item)
            elif body.strip():
                out.append(scalar(body.strip()))
            else:
                out.append(parse_block(cur + 2))
        return out

    def parse_map(indent, existing=False):
        out = {}
        while pos[0] < len(lines):
            no, text = lines[pos[0]]
            cur = indent_of(text)
            if cur < indent:
                break
            if text.lstrip().startswith("- "):
                if existing:
                    break
                fail(no, text, "list item where a mapping key was expected")
            if cur > indent and out:
                fail(no, text, "unexpected indent")
            indent = cur
            match = _SCALAR.match(text.strip())
            if not match:
                fail(no, text, "not a 'key: value' line")
            pos[0] += 1
            key, val = match.group("key"), match.group("val").strip()
            if val:
                out[key] = scalar(val)
            else:
                out[key] = parse_block(cur + 1)
        return out

    data = parse_block(0)
    if not isinstance(data, dict):
        raise ConfigError("%s must be a mapping at the top level" % path)
    return data


def _strip_comment(text):
    out, quote = [], None
    for ch in text:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r",
            "0": "\0", "b": "\b", "f": "\f", "e": "\x1b", " ": " "}


def unescape(text):
    """YAML double-quoted escapes. Unknown ones are refused, not passed through.

    Getting this wrong is invisible: `regex: "(\\d+)"` would silently become a
    pattern matching a literal backslash, and the claim would fail for a reason
    that has nothing to do with the number.
    """
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(text):
            raise ConfigError("string ends with a lone backslash: %r" % text)
        nxt = text[i + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        raise ConfigError(
            "unsupported escape \\%s in %r -- single-quote the string, or install "
            "PyYAML for the full grammar" % (nxt, text))
    return "".join(out)


def scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        inner = text[1:-1]
        # single quotes are literal in YAML apart from the doubled quote
        return inner.replace("''", "'") if text[0] == "'" else unescape(inner)
    low = text.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


# --------------------------------------------------------------------------- #
# sources: where the true value comes from
# --------------------------------------------------------------------------- #

def safe_path(root, rel):
    """Resolve a config-supplied path, refusing anything outside the repository.

    The claims file arrives from the checkout -- on a fork's pull request that
    means from a stranger. `doc: ../../etc/passwd` with --fix must not be a way
    to write outside the workspace.
    """
    if os.path.isabs(str(rel)):
        raise ConfigError("path must be relative to the repository: %s" % rel)
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, str(rel)))
    if full != base and not full.startswith(base + os.sep):
        raise ConfigError("path escapes the repository: %s" % rel)
    return full


DEFAULT_CMD_TIMEOUT = 60


def json_path(data, path):
    """Walk a dotted path with optional [i] indices. '$.' prefix is allowed."""
    node, walked = data, ""
    path = re.sub(r"^\$\.?", "", str(path))
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        walked += token if token.startswith("[") else ("." + token if walked else token)
        if token.startswith("["):
            idx = int(token[1:-1])
            if not isinstance(node, list) or idx >= len(node):
                raise ConfigError("path %r: no index %d at %r" % (path, idx, walked))
            node = node[idx]
        else:
            if not isinstance(node, dict) or token not in node:
                raise ConfigError("path %r: no key %r" % (path, walked))
            node = node[token]
    return node


def resolve_source(claim, root, allow_commands):
    """Return the true value for one claim, or raise ConfigError naming why not."""
    kinds = [k for k in ("json", "text", "cmd", "value") if claim.get(k) is not None]
    if not kinds:
        raise ConfigError("no source: give one of json:, text:, cmd:, value:")
    if len(kinds) > 1:
        raise ConfigError("more than one source: %s" % ", ".join(kinds))
    kind = kinds[0]

    if kind == "value":
        return claim["value"]

    if kind == "cmd":
        if not allow_commands:
            raise ConfigError(
                "cmd: is disabled. Pass --allow-commands (action input "
                "allow-commands: true) only for configs you trust -- a config "
                "coming from a fork's pull request can run anything on the runner"
            )
        try:
            limit = float(claim.get("timeout", DEFAULT_CMD_TIMEOUT))
        except (TypeError, ValueError):
            raise ConfigError("timeout: must be a number of seconds")
        try:
            proc = subprocess.run(
                claim["cmd"], shell=True, cwd=root, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True, timeout=limit,
            )
        except subprocess.TimeoutExpired:
            raise ConfigError(
                "cmd exceeded %gs and was killed -- a hanging source must fail the "
                "job, not idle the runner (raise it with timeout: on this claim)" % limit)
        if proc.returncode != 0:
            raise ConfigError(
                "cmd exited %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:200])
            )
        out = proc.stdout
        if claim.get("regex"):
            return _regex_pick(out, claim["regex"], "command output")
        if claim.get("path"):
            try:
                return json_path(json.loads(out), claim["path"])
            except ValueError as exc:
                raise ConfigError("command output is not JSON: %s" % exc)
        return out.strip()

    path = safe_path(root, claim[kind])
    if not os.path.isfile(path):
        raise ConfigError("source file not found: %s" % claim[kind])
    body = read_text(path, claim[kind])

    if kind == "json":
        if not claim.get("path"):
            raise ConfigError("json: needs path:")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ConfigError("%s is not valid JSON: %s" % (claim[kind], exc))
        return json_path(data, claim["path"])

    if not claim.get("regex"):
        raise ConfigError("text: needs regex: with one capture group")
    return _regex_pick(body, claim["regex"], claim[kind])


def _regex_pick(body, pattern, where):
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ConfigError("bad regex %r: %s" % (pattern, exc))
    match = rx.search(body)
    if not match:
        raise ConfigError("regex %r found nothing in %s" % (pattern, where))
    return match.group(1) if match.groups() else match.group(0)


# --------------------------------------------------------------------------- #
# rendering and comparing
# --------------------------------------------------------------------------- #

def render(value, fmt):
    if fmt is None:
        if isinstance(value, float) and value == int(value):
            return str(int(value))
        return str(value)
    fmt = str(fmt).strip()
    if fmt.startswith("{"):
        try:
            return fmt.format(value)
        except (ValueError, KeyError, IndexError) as exc:
            raise ConfigError("format %r failed: %s" % (fmt, exc))
    name, _, arg = fmt.partition(":")
    name = name.strip().lower()
    digits = int(arg) if arg.strip().isdigit() else None
    number = as_number(value)
    if name in ("raw", ""):
        return render(value, None)
    if number is None:
        raise ConfigError("format %r needs a number, got %r" % (fmt, value))
    if name == "comma":
        return "{:,.{d}f}".format(number, d=digits or 0)
    if name == "round":
        return "{:.{d}f}".format(number, d=0 if digits is None else digits)
    if name == "percent":
        return "{:.{d}f}".format(number * 100, d=0 if digits is None else digits)
    raise ConfigError("unknown format %r (use raw, comma, round:N, percent:N or a "
                      "python format spec like '{:.2f}')" % fmt)


_NUM = re.compile(r"^[-+]?\d*\.?\d+$")


def as_number(text):
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = str(text).strip()
    for junk in (",", "_", " ", " ", "%", "$", "€", "£"):
        cleaned = cleaned.replace(junk, "")
    if not _NUM.match(cleaned):
        return None
    return float(cleaned)


def matches(expected_text, found_text, tolerance):
    """Formatting is forgiven, arithmetic is not."""
    want, got = as_number(expected_text), as_number(found_text)
    if want is None or got is None:
        return str(expected_text).strip() == str(found_text).strip()
    if tolerance is None:
        return abs(want - got) < 1e-9
    tol = str(tolerance).strip()
    if tol.endswith("%"):
        allowed = abs(want) * float(tol[:-1]) / 100.0
    else:
        allowed = abs(float(tol))
    return abs(want - got) <= allowed + 1e-12


# --------------------------------------------------------------------------- #
# the doc side
# --------------------------------------------------------------------------- #

def marker_regex(claim_id):
    return re.compile(
        r"<!--\s*claim:%s\s*-->(.*?)<!--\s*/\s*claim\s*-->" % re.escape(claim_id),
        re.DOTALL,
    )


def pattern_regex(pattern):
    if "{value}" not in pattern:
        raise ConfigError("pattern %r must contain {value}" % pattern)
    head, _, tail = pattern.partition("{value}")
    return re.compile(re.escape(head) + r"([-+]?[\d][\d,_.  ]*\d|[-+]?\d)" + re.escape(tail))


def line_of(body, offset):
    return body.count("\n", 0, offset) + 1


def check_claim(claim, root, allow_commands):
    """One claim -> one row of the report. Never raises for doc problems."""
    cid = claim.get("id")
    row = {"id": cid, "doc": claim.get("doc"), "status": ERROR,
           "expected": None, "found": None, "line": None, "occurrences": 0,
           "note": ""}
    if not cid:
        row["note"] = "claim without id:"
        return row, None
    doc_rel = claim.get("doc")
    if not doc_rel:
        row["note"] = "no doc: given"
        return row, None

    try:
        true_value = resolve_source(claim, root, allow_commands)
        expected = render(true_value, claim.get("format"))
        rx = (pattern_regex(claim["pattern"]) if claim.get("pattern")
              else marker_regex(cid))
    except ConfigError as exc:
        row["note"] = str(exc)
        return row, None

    row["expected"] = expected
    try:
        doc_path = safe_path(root, doc_rel)
    except ConfigError as exc:
        row["note"] = str(exc)
        return row, None
    if not os.path.isfile(doc_path):
        row["note"] = "doc not found: %s" % doc_rel
        return row, None
    try:
        body = read_text(doc_path, doc_rel, newline="")
    except ConfigError as exc:
        row["note"] = str(exc)
        return row, None

    hits = list(rx.finditer(body))
    row["occurrences"] = len(hits)
    if not hits:
        row["status"] = MISSING
        row["note"] = ("no <!--claim:%s-->...<!--/claim--> marker in %s" % (cid, doc_rel)
                       if not claim.get("pattern")
                       else "pattern not found in %s" % doc_rel)
        return row, None

    tolerance = claim.get("tolerance")
    bad = [m for m in hits if not matches(expected, m.group(1), tolerance)]
    row["found"] = hits[0].group(1).strip()
    row["line"] = line_of(body, hits[0].start())
    if bad:
        row["status"] = DRIFT
        row["found"] = bad[0].group(1).strip()
        row["line"] = line_of(body, bad[0].start())
        row["note"] = "doc says %s, source says %s" % (row["found"], expected)
        return row, (doc_path, rx, expected)
    row["status"] = OK
    return row, None


def apply_fix(fixes):
    """Rewrite drifted values in place. Returns the set of touched files."""
    touched = set()
    for doc_path, rx, expected in fixes:
        body = read_text(doc_path, doc_path, newline="")
        def swap(match):
            whole, inner = match.group(0), match.group(1)
            start = match.start(1) - match.start(0)
            return whole[:start] + expected + whole[start + len(inner):]
        new = rx.sub(swap, body)
        if new != body:
            with open(doc_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            touched.add(doc_path)
    return touched


# --------------------------------------------------------------------------- #
# experimental: numbers nobody claimed
# --------------------------------------------------------------------------- #

DEFAULT_UNBACKED_IGNORE = [
    r"\bv?\d+\.\d+(\.\d+)?\b",          # versions
    r"\b(19|20)\d{2}\b",                 # years
    r"\b\d{1,2}:\d{2}\b",                # times
    r"#\d+",                             # issue references
]


def scan_unbacked(body, ignore):
    """Numbers of 2+ digits living outside code, links and claim markers."""
    masked = re.sub(r"```.*?```", lambda m: " " * len(m.group(0)), body, flags=re.DOTALL)
    masked = re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group(0)), masked)
    masked = re.sub(r"<!--.*?-->", lambda m: " " * len(m.group(0)), masked, flags=re.DOTALL)
    masked = re.sub(r"\]\([^)]*\)", lambda m: " " * len(m.group(0)), masked)
    masked = re.sub(r"https?://\S+", lambda m: " " * len(m.group(0)), masked)
    for pattern in ignore:
        masked = re.sub(pattern, lambda m: " " * len(m.group(0)), masked)
    out = []
    for match in re.finditer(r"(?<![\w.])\d[\d,_.]*\d\s*%?", masked):
        out.append((match.group(0).strip(), line_of(masked, match.start())))
    return out


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def annotate(row):
    level = "error" if row["status"] in (DRIFT, MISSING, ERROR) else "notice"
    where = ""
    if row.get("doc"):
        where = ",file=%s" % row["doc"]
        if row.get("line"):
            where += ",line=%d" % row["line"]
    message = row["note"] or "%s reproduces (%s)" % (row["id"], row["expected"])
    print("::%s%s,title=claim-check: %s::%s" % (level, where, row["id"], message))


def summary_table(rows):
    icons = {OK: "PASS", DRIFT: "DRIFT", MISSING: "MISSING", ERROR: "ERROR"}
    out = ["| claim | doc | status | doc says | source says |",
           "|---|---|---|---|---|"]
    for row in rows:
        out.append("| `%s` | %s | **%s** | %s | %s |" % (
            row["id"] or "?", row["doc"] or "-", icons.get(row["status"], row["status"]),
            row["found"] if row["found"] is not None else "-",
            row["expected"] if row["expected"] is not None else "-"))
    return "\n".join(out)


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s=%s\n" % (name, value))
    except OSError:
        pass


# --------------------------------------------------------------------------- #

def run(config_path, root=".", fix=False, allow_commands=False,
        strict_unbacked=False, report_path=None, fail_on_drift=True,
        annotations=True):
    try:
        config = load_config(os.path.join(root, config_path)
                             if not os.path.isabs(config_path) else config_path)
    except ConfigError as exc:
        print("::error::claim-check: %s" % exc)
        print("CLAIM-CHECK: config unusable -> ERROR (%s)" % exc)
        return EXIT_ERROR

    claims = config.get("claims")
    if not isinstance(claims, list) or not claims:
        print("::error::claim-check: config has no claims: list")
        print("CLAIM-CHECK: config has no claims -> ERROR")
        return EXIT_ERROR

    defaults = config.get("defaults") or {}

    def pass_over_claims():
        out_rows, out_fixes = [], []
        for raw_claim in claims:
            if not isinstance(raw_claim, dict):
                out_rows.append({"id": None, "doc": None, "status": ERROR,
                                 "expected": None, "found": None, "line": None,
                                 "occurrences": 0,
                                 "note": "claim is not a mapping: %r" % (raw_claim,)})
                continue
            claim = dict(defaults)
            claim.update(raw_claim)
            row, fix_job = check_claim(claim, root, allow_commands)
            out_rows.append(row)
            if fix_job:
                out_fixes.append(fix_job)
        return out_rows, out_fixes

    rows, fixes = pass_over_claims()

    seen = {}
    for row in rows:
        cid = row.get("id")
        if not cid:
            continue
        key = (row.get("doc"), cid)
        if key in seen:
            row["status"] = ERROR
            row["note"] = ("duplicate claim id %r on the same doc -- two claims "
                           "would fight over one place in the text" % cid)
        seen[key] = True

    fixed = set()
    if fix and fixes and not [r for r in rows if r["status"] == ERROR]:
        fixed = apply_fix(fixes)
        # Re-check instead of assuming the rewrite worked: two claims can target
        # overlapping text, and the second write would otherwise be reported as
        # a clean pass on top of a value it just destroyed.
        rows, _ = pass_over_claims()
        touched = {os.path.relpath(p, root) for p in fixed}
        for row in rows:
            if row["status"] == OK and row["doc"] in touched:
                row["note"] = row["note"] or "rewritten in place"

    unbacked = []
    if strict_unbacked:
        ignore = config.get("unbacked_ignore") or DEFAULT_UNBACKED_IGNORE
        claimed_docs = sorted({row["doc"] for row in rows if row["doc"]})
        for doc in claimed_docs:
            doc_path = os.path.join(root, doc)
            if not os.path.isfile(doc_path):
                continue
            try:
                body = read_text(doc_path, doc, newline="")
            except ConfigError:
                continue        # already an ERROR row; do not fail twice for it
            for cid in [row["id"] for row in rows if row["doc"] == doc and row["id"]]:
                body = marker_regex(cid).sub(lambda m: " " * len(m.group(0)), body)
            for text, line in scan_unbacked(body, ignore):
                unbacked.append({"doc": doc, "value": text, "line": line})

    if annotations:
        for row in rows:
            annotate(row)
        for item in unbacked:
            print("::warning file=%s,line=%d,title=claim-check: unbacked number::"
                  "%s is not bound to any source" % (item["doc"], item["line"], item["value"]))

    errors = [r for r in rows if r["status"] == ERROR]
    drifted = [r for r in rows if r["status"] in (DRIFT, MISSING)]

    print("")
    print(summary_table(rows))
    problems = [r for r in rows if r["status"] != OK]
    if problems:
        print("\nwhat went wrong:")
        for row in problems:
            where = row["doc"] or "-"
            if row.get("line"):
                where += ":%d" % row["line"]
            print("  %-8s %-22s %s  (%s)" % (row["status"], row["id"] or "?",
                                             row["note"], where))
    if unbacked:
        print("\n%d number(s) in the docs are bound to nothing (strict-unbacked):" % len(unbacked))
        for item in unbacked[:20]:
            print("  %s:%d  %s" % (item["doc"], item["line"], item["value"]))
    if fixed:
        print("\nrewritten in place: %s" % ", ".join(sorted(os.path.relpath(p, root) for p in fixed)))

    report = {"version": __version__, "checked": len(rows),
              "ok": len(rows) - len(errors) - len(drifted),
              "drift": len(drifted), "errors": len(errors),
              "fixed": sorted(os.path.relpath(p, root) for p in fixed),
              "unbacked": unbacked, "claims": rows}
    if report_path:
        try:
            with open(safe_path(root, report_path), "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
        except ConfigError as exc:
            print("::error::claim-check: --report %s" % exc)
            print("CLAIM-CHECK: --report %s -> ERROR" % exc)
            return EXIT_ERROR

    set_output("checked-count", len(rows))
    set_output("drift-count", len(drifted))
    set_output("error-count", len(errors))

    if errors:
        verdict, code = "ERROR", EXIT_ERROR
    elif drifted:
        verdict, code = "DRIFT", (EXIT_DRIFT if fail_on_drift else EXIT_OK)
    else:
        verdict, code = "OK", EXIT_OK

    line = ("CLAIM-CHECK: %d claims - %d ok - %d drift - %d errors -> %s"
            % (len(rows), report["ok"], len(drifted), len(errors), verdict))
    write_summary("### claim-check: %s\n\n%s\n" % (verdict, summary_table(rows)))
    print("")
    print(line)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify that every number in your docs still reproduces from its source.")
    parser.add_argument("--config", default=".github/claims.yml")
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--fix", action="store_true",
                        help="rewrite drifted values in the docs instead of only reporting")
    parser.add_argument("--allow-commands", action="store_true",
                        help="permit cmd: sources (arbitrary shell -- trusted configs only)")
    parser.add_argument("--strict-unbacked", action="store_true",
                        help="also warn about numbers not bound to any source (experimental)")
    parser.add_argument("--report", default=None, help="write a JSON report here")
    parser.add_argument("--no-fail-on-drift", action="store_true",
                        help="report drift but exit 0 (errors still exit 2)")
    parser.add_argument("--no-annotations", action="store_true",
                        help="do not print ::error:: workflow commands")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    return run(args.config, root=args.root, fix=args.fix,
               allow_commands=args.allow_commands,
               strict_unbacked=args.strict_unbacked,
               report_path=args.report,
               fail_on_drift=not args.no_fail_on_drift,
               annotations=not args.no_annotations)


def cli(argv=None):
    """Entry point for the installed `claim-check` command.

    The ConfigError -> exit 2 rule cannot live in the `__main__` guard alone: a
    console script installed by pip never runs that guard, so a broken config
    would surface as a traceback and exit 1 -- which the exit-code contract
    reserves for DRIFT. A dead checker must not look like a clean result, and it
    must not look like a drifted one either.
    """
    try:
        return main(argv)
    except ConfigError as exc:                    # anything we forgot to catch
        print("::error::claim-check: %s" % exc)
        print("CLAIM-CHECK: %s -> ERROR" % exc)
        return EXIT_ERROR
    except Exception:                             # noqa: BLE001 -- see below
        # A crash is a broken checker, and a broken checker must not be reported
        # as DRIFT. An uncaught exception exits 1, which is the code reserved for
        # "your documented number changed" -- so CI would blame the document for
        # a bug in this file. The traceback still goes to stderr; only the exit
        # code is corrected.
        import traceback
        traceback.print_exc()
        print("CLAIM-CHECK: claim-check crashed -> ERROR")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(cli())
