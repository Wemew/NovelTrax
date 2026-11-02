"""Rewrite bare ``assert`` statements into guards that survive ``python -O``.

Why this exists
---------------
Calibre's embedded interpreter (``calibre-debug.exe``) runs with
``sys.flags.optimize == 2``. At optimisation level 2 CPython removes every
``assert`` statement while compiling, so a test suite executed through
``calibre-debug -e <file>`` can report a 100% pass rate while checking
nothing at all. That is a false-pass suite, and it is the reason the
Milestone 5 forensic audit failed.

This tool rewrites each ``assert <test>[, <msg>]`` into::

    if not (<test>): raise AssertionError(<msg>)

which is ordinary control flow and therefore unaffected by optimisation
level.

Usage
-----
    python tools/assert_hardening.py tests/test_e2e_suite.py [more.py ...]
    python tools/assert_hardening.py --check tests/          # dry run, exit 1 if found
    python tools/assert_hardening.py --dry-run tests/x.py    # show counts only

The rewrite is conservative: it only touches statements that begin a line
(so ``x = 1; assert y`` style lines are reported and skipped, never
corrupted), and it re-parses plus re-compiles the result before writing.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from typing import List, Tuple

SKIP_REASON_SEMICOLON = "statement does not start its line (semicolon chain)"


def _line_starts(source: str) -> List[int]:
    """Absolute character offset of the start of every physical line."""
    starts = [0]
    for idx, char in enumerate(source):
        if char == "\n":
            starts.append(idx + 1)
    return starts


def _char_col(line_text: str, byte_col: int) -> int:
    """Convert a UTF-8 byte column (what CPython AST reports) to a char column.

    ``col_offset`` / ``end_col_offset`` are documented as UTF-8 byte offsets,
    so any line containing non-ASCII text (e.g. ``Lu自主规制``) shifts every
    later character position. Slicing the source with the raw value mangles
    such lines, so map it back to characters first.
    """
    if byte_col <= 0:
        return 0
    raw = line_text.encode("utf-8")
    if byte_col >= len(raw):
        return len(line_text)
    # AST columns always land on token boundaries, so no partial code point.
    return len(raw[:byte_col].decode("utf-8", "ignore"))


def _offset(starts: List[int], lines: List[str], lineno: int, byte_col: int) -> int:
    """Convert a 1-based (lineno, byte col_offset) pair into an absolute char offset."""
    return starts[lineno - 1] + _char_col(lines[lineno - 1], byte_col)



def _candidate_asserts(tree: ast.AST) -> List[ast.Assert]:
    nodes: List[ast.Assert] = [
        node for node in ast.walk(tree) if isinstance(node, ast.Assert)
    ]
    # Rewrite from the bottom of the file upwards so earlier offsets stay valid.
    nodes.sort(key=lambda n: (n.lineno, n.col_offset), reverse=True)
    return nodes


def harden_source(source: str, filename: str = "<string>") -> Tuple[str, int, List[str]]:
    """Return (new_source, rewritten_count, skipped_reasons)."""
    tree = ast.parse(source, filename=filename)
    nodes = _candidate_asserts(tree)
    if not nodes:
        return source, 0, []

    starts = _line_starts(source)
    lines = source.split("\n")
    skipped: List[str] = []
    rewritten = 0
    updated = source

    for node in nodes:
        if node.end_lineno is None or node.end_col_offset is None:
            skipped.append(f"line {node.lineno}: missing end position")
            continue

        start = _offset(starts, lines, node.lineno, node.col_offset)
        end = _offset(starts, lines, node.end_lineno, node.end_col_offset)

        line_start = starts[node.lineno - 1]
        prefix = source[line_start:start]
        if prefix.strip():
            skipped.append(f"line {node.lineno}: {SKIP_REASON_SEMICOLON}")
            continue

        test = node.test
        test_src = ast.get_source_segment(source, test)
        if test_src is None:
            skipped.append(f"line {node.lineno}: could not recover condition source")
            continue

        if node.msg is not None:
            msg_src = ast.get_source_segment(source, node.msg)
            if msg_src is None:
                skipped.append(f"line {node.lineno}: could not recover message source")
                continue
            message = msg_src
        else:
            message = f'"assertion failed at line {node.lineno}"'

        replacement = f"if not ({test_src}): raise AssertionError({message})"
        updated = updated[:start] + replacement + updated[end:]
        rewritten += 1

    if rewritten:
        # Hard guarantee: the result must parse, compile, and contain no asserts
        # beyond the ones we deliberately skipped.
        new_tree = ast.parse(updated, filename=filename)
        compile(updated, filename, "exec")
        leftovers = [n for n in ast.walk(new_tree) if isinstance(n, ast.Assert)]
        if len(leftovers) > len(skipped):
            raise RuntimeError(
                f"{filename}: {len(leftovers)} assert(s) survived hardening "
                f"but only {len(skipped)} were skipped"
            )

    return updated, rewritten, skipped


def harden_file(path: str) -> Tuple[int, List[str]]:
    # newline="" keeps the original CRLF/LF style untouched on rewrite.
    with open(path, "r", encoding="utf-8", newline="") as handle:
        original = handle.read()

    updated, count, skipped = harden_source(original, filename=path)
    if count:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    return count, skipped


def collect_targets(entries: List[str]) -> List[str]:
    files: List[str] = []
    for entry in entries:
        if os.path.isdir(entry):
            for name in sorted(os.listdir(entry)):
                if name.endswith(".py"):
                    files.append(os.path.join(entry, name))
        else:
            files.append(entry)
    return files


def count_asserts(path: str) -> int:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        tree = ast.parse(handle.read(), filename=path)
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Python files or directories")
    parser.add_argument("--check", action="store_true", help="report only; exit 1 if asserts exist")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args(argv)

    targets = collect_targets(args.paths)
    total = 0
    dirty = 0

    for path in targets:
        if args.check or args.dry_run:
            try:
                found = count_asserts(path)
            except SyntaxError as exc:
                print(f"SKIP  {path}: syntax error: {exc}")
                continue
            if found:
                dirty += 1
                total += found
                print(f"FOUND {found:4d}  {path}")
            continue

        count, skipped = harden_file(path)
        total += count
        if count:
            dirty += 1
            print(f"FIXED {count:4d}  {path}")
        for reason in skipped:
            print(f"  skipped {path}: {reason}")

    verb = "found" if (args.check or args.dry_run) else "rewritten"
    print(f"\n{total} bare assert(s) {verb} across {dirty} file(s) of {len(targets)} scanned")

    if args.check:
        return 1 if total else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
