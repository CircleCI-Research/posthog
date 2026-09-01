#!/usr/bin/env python3
"""Compute CircleCI pipeline parameters from the set of files a branch changes.

The setup stage runs this, writes the JSON it prints, and hands that file to
`continuation/continue`. It replaces the `changes` job that every
`.github/workflows/ci-*.yml` runs through the vendored paths-filter action.

Matching follows .github/actions/paths-filter/README.md: includes are OR-ed,
any `!` exclude vetoes the file, and a mapping entry restricts its patterns to
the listed git change statuses.

Run `--selftest` to exercise the matcher without touching git.
"""

from __future__ import annotations

import os
import re
import sys
import json
import base64
import argparse
import subprocess
from typing import Any

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_FILTERS = os.path.join(REPO_ROOT, ".circleci", "path-filters.yml")

# git's --name-status letters, mapped onto the change-status words paths-filter uses.
STATUS_WORDS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "modified",
}


class Pattern:
    """One include or exclude glob, with the change statuses it applies to."""

    def __init__(self, glob: str, statuses: frozenset[str] | None = None) -> None:
        self.negated = glob.startswith("!")
        self.glob = glob[1:] if self.negated else glob
        self.statuses = statuses
        self.regex = re.compile(glob_to_regex(self.glob))

    def matches(self, path: str, status: str) -> bool:
        if self.statuses is not None and status not in self.statuses:
            return False
        return self.regex.match(path) is not None


def glob_to_regex(glob: str) -> str:
    """Translate a picomatch-style glob into an anchored regex.

    `**` spans directory separators, `*` and `?` do not. A leading `**/` also
    matches a path with no leading directory at all, so `**/*.py` covers
    `manage.py` as well as `posthog/manage.py`.
    """
    out: list[str] = ["^"]
    if glob.startswith("**/"):
        # Globstar absorbs zero or more leading segments.
        out.append("(?:[^/]+/)*")
        glob = glob[3:]

    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("/**/", i):
            out.append("/(?:[^/]+/)*")
            i += 4
        elif glob.startswith("/**", i) and i + 3 == n:
            # A trailing `/**` covers everything below the directory.
            out.append("/.*")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            close = glob.find("]", i + 1)
            if close == -1:
                out.append(re.escape(c))
                i += 1
            else:
                body = glob[i + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = close + 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return "".join(out)


def parse_filters(raw: dict[str, Any]) -> dict[str, list[Pattern]]:
    parsed: dict[str, list[Pattern]] = {}
    for name, entries in raw.items():
        patterns: list[Pattern] = []
        for entry in entries:
            if isinstance(entry, str):
                patterns.append(Pattern(entry))
                continue
            if isinstance(entry, dict):
                for status_spec, globs in entry.items():
                    statuses = frozenset(str(status_spec).split("|"))
                    for glob in globs if isinstance(globs, list) else [globs]:
                        if str(glob).startswith("!"):
                            raise ValueError(f"{name}: '!' patterns are not supported inside a change-status array")
                        patterns.append(Pattern(str(glob), statuses))
                continue
            raise ValueError(f"{name}: unsupported filter entry {entry!r}")
        parsed[name] = patterns
    return parsed


def filter_matches(patterns: list[Pattern], changes: list[tuple[str, str]]) -> bool:
    includes = [p for p in patterns if not p.negated]
    excludes = [p for p in patterns if p.negated]
    for path, status in changes:
        if excludes and any(p.matches(path, status) for p in excludes):
            continue
        if not includes or any(p.matches(path, status) for p in includes):
            return True
    return False


def evaluate(filters: dict[str, list[Pattern]], changes: list[tuple[str, str]]) -> dict[str, bool]:
    return {name: filter_matches(patterns, changes) for name, patterns in filters.items()}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout


def changed_files(base: str) -> list[tuple[str, str]]:
    """Return (path, status-word) for every file this branch changes against `base`."""
    merge_base = git("merge-base", base, "HEAD").strip()
    raw = git("diff", "--name-status", "--no-renames", f"{merge_base}..HEAD")
    changes: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        letter = parts[0][:1]
        path = parts[-1]
        changes.append((path, STATUS_WORDS.get(letter, "modified")))
    return changes


def _out(message: str) -> None:
    # sys.stdout rather than print(): the repo's ruff config selects T2.
    sys.stdout.write(message + "\n")


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")


JEST_SOURCE_PREFIXES = ("frontend/src/", "products/")
JEST_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")


def encode_jest_selection(changes: list[tuple[str, str]]) -> str:
    """Base64 the frontend sources jest should narrow to, or '' for a full run.

    The value reaches the jest job through a pipeline parameter, and CircleCI
    substitutes a parameter into a `run` command as raw text — an unencoded
    path list would be re-read by the shell. Base64 keeps the payload opaque
    until the job decodes it.
    """
    selected = sorted(
        path
        for path, status in changes
        if status != "deleted" and path.startswith(JEST_SOURCE_PREFIXES) and path.endswith(JEST_SOURCE_SUFFIXES)
    )
    if not selected:
        return ""
    return base64.b64encode("\n".join(selected).encode()).decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--filters", default=DEFAULT_FILTERS)
    ap.add_argument("--base", default=os.environ.get("CI_BASE_REF", "origin/master"))
    ap.add_argument(
        "--all-true",
        action="store_true",
        help="Skip path filtering and turn every parameter on (master builds).",
    )
    ap.add_argument("--output", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    with open(args.filters) as fh:
        filters = parse_filters(yaml.safe_load(fh))

    params: dict[str, bool | int | str]
    if args.all_true:
        params = dict.fromkeys(filters, True)
        changes: list[tuple[str, str]] = []
    else:
        changes = changed_files(args.base)
        params = evaluate(filters, changes)

    # Non-path parameters the continue config declares. Kept here so the emitted
    # set and the declared set stay identical — the continuation API rejects a
    # parameter the target config does not declare.
    params["changed-file-count"] = len(changes)
    params["selected-jest-paths-b64"] = encode_jest_selection(changes)

    body = json.dumps(params, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(body)
    _out(body)
    return 0


def selftest() -> int:
    cases: list[tuple[str, list[str], list[tuple[str, str]], bool]] = [
        # name, patterns, changes, expected
        ("include hit", ["posthog/**"], [("posthog/models/team.py", "modified")], True),
        ("include miss", ["posthog/**"], [("frontend/src/App.tsx", "modified")], False),
        (
            "globstar spans dirs",
            ["products/**/backend/**/*"],
            [("products/surveys/backend/api/views.py", "modified")],
            True,
        ),
        (
            "exclude vetoes the only match",
            ["posthog/**", "!**/*.md"],
            [("posthog/README.md", "modified")],
            False,
        ),
        (
            "exclude leaves a sibling match",
            ["posthog/**", "!**/*.md"],
            [("posthog/README.md", "modified"), ("posthog/urls.py", "modified")],
            True,
        ),
        (
            "leading globstar reaches the repo root",
            ["**/*.py"],
            [("manage.py", "modified")],
            True,
        ),
        (
            "single star stops at a slash",
            ["products/*/product.yaml"],
            [("products/a/b/product.yaml", "modified")],
            False,
        ),
        (
            "single star matches one segment",
            ["products/*/product.yaml"],
            [("products/surveys/product.yaml", "added")],
            True,
        ),
        ("trailing slash-globstar", ["rust/**"], [("rust/common/src/lib.rs", "modified")], True),
        ("no change set", ["posthog/**"], [], False),
        ("dot file", ["**/*.md"], [(".github/AGENTS.md", "modified")], True),
    ]
    failures = 0
    for name, globs, changes, expected in cases:
        got = filter_matches([Pattern(g) for g in globs], changes)
        if got != expected:
            failures += 1
            _err(f"FAIL {name}: expected {expected}, got {got}")
        else:
            _out(f"ok   {name}")

    status_pattern = parse_filters({"dockerfiles": [{"added|modified": ["**/Dockerfile"]}]})["dockerfiles"]
    for label, changes, expected in [
        ("status added matches", [("Dockerfile", "added")], True),
        ("status deleted does not match", [("Dockerfile", "deleted")], False),
    ]:
        got = filter_matches(status_pattern, changes)
        if got != expected:
            failures += 1
            _err(f"FAIL {label}: expected {expected}, got {got}")
        else:
            _out(f"ok   {label}")

    jest_cases: list[tuple[str, list[tuple[str, str]], list[str]]] = [
        (
            "jest selection keeps frontend sources",
            [("frontend/src/App.tsx", "modified"), ("posthog/urls.py", "modified")],
            ["frontend/src/App.tsx"],
        ),
        (
            "jest selection drops deleted files",
            [("frontend/src/Gone.tsx", "deleted")],
            [],
        ),
        (
            "jest selection survives a path with shell metacharacters",
            [("products/a b;`echo x`/c.tsx", "added")],
            ["products/a b;`echo x`/c.tsx"],
        ),
    ]
    for label, changes, expected_paths in jest_cases:
        encoded = encode_jest_selection(changes)
        decoded = base64.b64decode(encoded).decode().split("\n") if encoded else []
        if decoded != expected_paths:
            failures += 1
            _err(f"FAIL {label}: expected {expected_paths}, got {decoded}")
        else:
            _out(f"ok   {label}")

    _out(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
