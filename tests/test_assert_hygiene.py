#!/usr/bin/env python3
"""Regression guard: test modules must never contain bare `assert` statements.

Calibre's embedded interpreter runs with ``sys.flags.optimize == 2``, which
compiles bare ``assert`` statements out of the bytecode entirely.  A test suite
built on bare asserts therefore reports "passing" tests while checking nothing
at all.  Every test expectation must be an unstrippable guard instead::

    if not (condition):
        raise AssertionError("what went wrong")

This guard statically parses every ``.py`` file under ``tests/`` with ``ast``
and fails if ANY ``ast.Assert`` node is present.  Static parsing is deliberate:
it works even though this very process runs with ``-O2`` (``ast.parse`` never
strips nodes; only bytecode compilation does).

Usage (the Calibre interpreter is the supported runner on this machine)::

    & "C:\\Program Files\\Calibre2\\calibre-debug.exe" -e tests\\test_assert_hygiene.py

Stdlib only: ast, os, sys.  No third-party imports.
"""

import ast
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def find_bare_asserts(source, filename):
    """Return [(filename, lineno), ...] for every ast.Assert node in *source*.

    ``ast.parse`` reflects the source text, not the optimized bytecode, so bare
    asserts are still visible even under ``sys.flags.optimize == 2``.
    """
    tree = ast.parse(source, filename=filename)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            hits.append((filename, node.lineno))
    return hits


def iter_test_files(tests_dir):
    """Yield every .py file under tests_dir (recursive, deterministic order)."""
    paths = []
    for root, dirnames, filenames in os.walk(tests_dir):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                paths.append(os.path.join(root, name))
    return paths


def self_check_detector():
    """Prove the detector works on in-memory modules, so it cannot silently pass.

    A detector that never finds anything would make this whole guard vacuous.
    Feed it one module containing a bare assert (must be detected) and one
    module written in guard style (must not be flagged).
    """
    bare_probe = "def probe():\n    assert 1 + 1 == 2, 'in-memory probe'\n"
    bare_hits = find_bare_asserts(bare_probe, "<in-memory-bare-probe>")
    if not (len(bare_hits) == 1):
        raise SystemExit(
            "ERROR: self-check failed - detector found %d bare asserts in the "
            "in-memory probe, expected exactly 1" % len(bare_hits)
        )

    guard_probe = (
        "def probe():\n"
        "    if not (1 + 1 == 2):\n"
        "        raise AssertionError('guard-style check')\n"
    )
    guard_hits = find_bare_asserts(guard_probe, "<in-memory-guard-probe>")
    if guard_hits:
        raise SystemExit(
            "ERROR: self-check failed - detector wrongly flagged a guard-style "
            "module as containing %d bare assert(s)" % len(guard_hits)
        )
    print("self-check: bare-assert detector verified on in-memory probes")


def scan_tests_dir(tests_dir):
    """Return (scanned_count, [(path, lineno), ...]) for the whole tests dir."""
    violations = []
    scanned = 0
    for path in iter_test_files(tests_dir):
        scanned += 1
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            source = handle.read()
        try:
            violations.extend(find_bare_asserts(source, path))
        except SyntaxError as exc:
            # An unparsable test module is itself a failure of this guard.
            violations.append((path, getattr(exc, "lineno", 0) or 0))
    return scanned, violations


def main():
    self_check_detector()
    scanned, violations = scan_tests_dir(TESTS_DIR)

    if violations:
        print(
            "FAIL: found %d bare assert statement(s) across %d scanned test file(s)."
            % (len(violations), scanned)
        )
        print(
            "Calibre strips bare asserts under sys.flags.optimize == 2; "
            "use 'if not (condition): raise AssertionError(...)' instead."
        )
        for path, lineno in violations:
            print("  %s:%d: bare assert" % (path, lineno))
        raise SystemExit(1)

    print("scanned %d files, 0 bare asserts" % scanned)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
