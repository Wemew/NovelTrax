#!/usr/bin/env python3
"""NovelTrax assert-aware test runner.

WHY THIS EXISTS
---------------
``& "C:\\Program Files\\Calibre2\\calibre-debug.exe" -e tests\\test_foo.py``
runs the embedded interpreter with ``sys.flags.optimize == 2``
(``__debug__ is False``).  CPython removes every bare ``assert`` statement when
the source is compiled with optimize >= 1, so a suite executed that way can
print "ALL TESTS PASSED" and exit 0 while its assertions did not exist in the
bytecode at all.  That is not evidence: it is a vacuous run.

This runner removes that trap.  For every selected suite it:

  * reads the source file and reports ``sys.flags.optimize`` / ``__debug__``;
  * counts every ``ast.Assert`` node in the source (the "compiled" count --
    they are all kept because of the next step);
  * recompiles the source itself with ``compile(source, path, "exec",
    optimize=0)``, so the asserts are present in the bytecode no matter which
    optimisation level the surrounding interpreter was started with;
  * executes that code object in a fresh ``__main__`` module namespace, with
    the repo root, ``backend/`` and ``tests/`` on ``sys.path`` and
    ``sys.argv == [suite_path]``, so a suite's own
    ``if __name__ == "__main__":`` block runs exactly as it would under a
    direct ``-e`` invocation;
  * counts *observed* assert executions with real interpreter evidence
    (``sys.monitoring`` line events on the suite's own code objects, or a
    ``sys.settrace`` line tracer when monitoring is unavailable) -- the counter
    is never inferred from "it did not raise";
  * isolates suites from each other (``sys.path`` / ``sys.modules`` / cwd /
    environment / argv / recursion limit are snapshotted and restored, stdout
    and stderr are captured per suite), so one suite's crash or monkeypatch
    cannot stop or poison the rest;
  * prints the failing suite path, exception, deepest failing file:line and
    source statement via ``traceback.extract_tb`` plus the full traceback, and
    exits non-zero if any selected suite failed.

WARNINGS
--------
When a suite contains bare asserts the runner says so loudly: a direct ``-e``
run of that suite is vacuous (the asserts are compiled away) and only this
runner makes its PASS/FAIL meaningful.  (NovelTrax suites currently use
explicit ``raise AssertionError(...)`` checks, so most suites compile to zero
bare asserts -- the warning is for the future, and for planted/regression
suites.)

ENVIRONMENT PROBES
------------------
A suite that reads the compile-time ``__debug__`` constant is probing the
interpreter's native optimisation mode.  ``optimize=0`` recompilation
*necessarily* makes ``__debug__`` True (that is the same flag that keeps bare
asserts), so such a suite cannot pass when recompiled and cannot honestly be
run natively if it has bare asserts either.  The runner therefore executes a
``__debug__``-referencing suite natively **only when it contains zero bare
asserts** -- nothing is stripped, because there is nothing to strip -- and
prints a loud ``[NOTE]`` saying so.  Every other suite is executed with
optimization stripped.  This is the only documented deviation from optimize=0
execution and it is visible per suite in the run log and the summary.

LIMITATION
----------
Only the *selected suite file* is recompiled with optimize=0.  Modules that a
suite imports (other test modules, ``backend.app.*``) are imported normally and
therefore still compiled at the interpreter's native optimisation level.  All
current NovelTrax suites are assert-hygienic and use explicit raises, so this
does not weaken today's evidence; a bare assert inside an *imported* module
would still be stripped.  A hard process kill from inside a suite (``os._exit``,
segfault) also cannot be contained by an in-process runner; Python exceptions
and ``SystemExit`` are caught and reported per suite.

USAGE
-----
Run every ``tests/test_*.py`` suite::

    & "C:\\Program Files\\Calibre2\\calibre-debug.exe" -e tests\\run_tests.py

Run suites whose path contains any of the given substrings (case-insensitive,
OR semantics)::

    ... -e tests\\run_tests.py -- chapter media
    ... -e tests\\run_tests.py -- adversarial --verbose

Run one explicit file (works for out-of-tree throwaway suites too)::

    ... -e tests\\run_tests.py -- scratch\\planted_failure.py

IMPORTANT: ``calibre-debug.exe`` parses leading-hyphen options for itself, so
script options must come after its ``--`` separator::

    & calibre-debug.exe -e tests\\run_tests.py -- --verbose
    & calibre-debug.exe -e tests\\run_tests.py -- --list

(Plain substring/path arguments do not need the separator, but using it is
always safe.)

Other options::

    --verbose / -v   show each suite's captured output even when it passes
    --list / -l      print the selected suites and exit
    --help / -h      print this documentation and exit

Exit codes: 0 = every selected suite passed; 1 = at least one failed;
2 = usage error / no suites selected.

Stdlib only: os, sys, ast, types, traceback, time, io.  No third-party
imports, no test framework, and no bare ``assert`` in this file (it is itself
scanned by ``tests/test_assert_hygiene.py``).
"""

import ast
import io
import os
import sys
import time
import traceback
import types

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_FILE = os.path.abspath(globals().get("__file__") or sys.argv[0])
TESTS_DIR = os.path.dirname(_THIS_FILE)
REPO_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")

SUITE_PREFIX = "test_"
SUITE_SUFFIX = ".py"

# A suite that prints this much is pathological; keep memory finite.
MAX_CAPTURE_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# Assertion counting
# ---------------------------------------------------------------------------
# Preferred monitoring tool ids.  0/1/2 are the conventional
# debugger/coverage/profiler ids; 4 and 3 are free in practice on CPython 3.12+.
_MONITORING_TOOL_NAME = "noveltrax-assert-runner"
_MONITORING_TOOL_IDS = (4, 3, 5, 2, 0, 1)

_SOURCE_LINE_CACHE = {}


def iter_code_objects(code):
    """Yield *code* and every nested code object (functions, comprehensions...)."""
    stack = [code]
    while stack:
        current = stack.pop()
        yield current
        for const in current.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)


def collect_assert_stats(source, filename):
    """Analyse *source* and return ``(compiled, per_line, untraceable)``.

    ``compiled``     -- total ``ast.Assert`` nodes (all kept by optimize=0).
    ``per_line``     -- ``{line_number: count}`` restricted to assert statements
                        that begin their own source line, i.e. lines for which
                        a line-trace event is unambiguous evidence that the
                        assert executed.
    ``untraceable``  -- asserts that share a line with another statement
                        (e.g. ``if debug: assert x``); a line event on such a
                        line would not prove the assert body ran, so they are
                        deliberately excluded from the observed counter rather
                        than guessed at.

    ``ast.parse`` reflects the text, not the optimized bytecode, so this works
    even though the runner itself is running under optimize=2.
    """
    tree = ast.parse(source, filename=filename)
    source_lines = source.splitlines()
    compiled = 0
    per_line = {}
    untraceable = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        compiled += 1
        text = ""
        if 1 <= node.lineno <= len(source_lines):
            text = source_lines[node.lineno - 1].strip()
        if text.startswith("assert"):
            per_line[node.lineno] = per_line.get(node.lineno, 0) + 1
        else:
            untraceable += 1
    return compiled, per_line, untraceable


def references_debug_constant(source, filename):
    """Return True when *source* reads the compile-time ``__debug__`` constant.

    Such a suite is an *environment probe*: it may assert the interpreter's
    native optimisation mode (``__debug__ is False`` under Calibre's -O2).
    That expectation is, by construction, impossible to satisfy in code
    recompiled with optimize=0 (asserts kept implies ``__debug__ is True``).
    The runner therefore executes a zero-bare-assert environment probe
    natively, and says so loudly -- no assertion is lost because the suite has
    no bare asserts to strip.  ``ast.Name`` is used (not a text search) so
    docstrings and print-only mentions are not misdetected.
    """
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__debug__":
            return True
    return False


class AssertCounter:
    """Counts *executed* assert statements using real interpreter events.

    Preferred backend: ``sys.monitoring`` (PEP 669) LINE events set as *local*
    events on the suite's code objects only.  The runner's own code and the
    backend modules are not instrumented, so there is no measurable slowdown
    and no distortion of timing-sensitive suites.

    Fallback backend: a ``sys.settrace`` line tracer installed for the duration
    of the suite and restricted to frames whose ``co_filename`` is the suite
    file.  It is slower but provides the same evidence on interpreters without
    ``sys.monitoring``.

    In both cases the callback maps a line number to the number of assert
    statements known (from the AST) to start on that line.  A line event is
    emitted when execution reaches the line, so a non-zero observed count is
    evidence that the assert was actually reached -- not a guess.
    """

    def __init__(self):
        self.mode = "unavailable"
        self.tool_id = None
        self.executed = 0
        self._per_line = {}
        self._filename = None
        self._instrumented = []

        monitoring = getattr(sys, "monitoring", None)
        if monitoring is not None:
            for candidate in _MONITORING_TOOL_IDS:
                try:
                    monitoring.use_tool_id(candidate, _MONITORING_TOOL_NAME)
                except (ValueError, RuntimeError):
                    continue
                self.tool_id = candidate
                self.mode = "sys.monitoring"
                break

        if self.mode == "unavailable":
            self.mode = "sys.settrace"

    # -- sys.monitoring backend -------------------------------------------
    def _monitoring_line(self, code, line_number):
        multiplicity = self._per_line.get(line_number)
        if multiplicity:
            self.executed += multiplicity

    # -- sys.settrace backend ---------------------------------------------
    def _trace_dispatch(self, frame, event, arg):
        if event == "call" and frame.f_code.co_filename == self._filename:
            return self._trace_lines
        return None

    def _trace_lines(self, frame, event, arg):
        if event == "line":
            multiplicity = self._per_line.get(frame.f_lineno)
            if multiplicity:
                self.executed += multiplicity
        return self._trace_lines

    # -- lifecycle ---------------------------------------------------------
    def start(self, code, per_line):
        """Begin counting assert executions for *code*."""
        self.executed = 0
        self._per_line = per_line
        self._filename = code.co_filename
        self._instrumented = []

        if self.mode == "sys.monitoring":
            monitoring = sys.monitoring
            monitoring.register_callback(
                self.tool_id, monitoring.events.LINE, self._monitoring_line
            )
            for nested in iter_code_objects(code):
                if nested.co_filename != self._filename:
                    continue
                try:
                    monitoring.set_local_events(
                        self.tool_id, nested, monitoring.events.LINE
                    )
                    self._instrumented.append(nested)
                except (ValueError, RuntimeError):
                    # A code object that cannot be instrumented simply is not
                    # counted; the failure to instrument is reported by the
                    # caller's observed==0 warning rather than hidden.
                    pass
        elif self.mode == "sys.settrace":
            sys.settrace(self._trace_dispatch)

    def stop(self):
        """Remove all instrumentation.  Safe to call after a failed start."""
        if self.mode == "sys.monitoring" and self.tool_id is not None:
            monitoring = sys.monitoring
            for nested in self._instrumented:
                try:
                    monitoring.set_local_events(self.tool_id, nested, 0)
                except (ValueError, RuntimeError):
                    pass
            self._instrumented = []
            try:
                monitoring.register_callback(
                    self.tool_id, monitoring.events.LINE, None
                )
            except (ValueError, RuntimeError):
                pass
        elif self.mode == "sys.settrace":
            sys.settrace(None)

    def release(self):
        """Free the monitoring tool id (called once, at the end of the run)."""
        if self.mode == "sys.monitoring" and self.tool_id is not None:
            try:
                sys.monitoring.free_tool_id(self.tool_id)
            except (ValueError, RuntimeError):
                pass
            self.tool_id = None


# ---------------------------------------------------------------------------
# Failure rendering -- never swallow anything
# ---------------------------------------------------------------------------
def display_path(path):
    """Render *path* relative to the repo root with forward slashes."""
    if not path:
        return "<unknown>"
    path = os.path.abspath(path)
    try:
        relative = os.path.relpath(path, REPO_ROOT)
    except (ValueError, OSError):
        return path.replace("\\", "/")
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path.replace("\\", "/")
    return relative.replace("\\", "/")


def read_source_line(path, lineno):
    """Best-effort read of one source line (tracebacks here omit source text)."""
    key = (path, lineno)
    cached = _SOURCE_LINE_CACHE.get(key)
    if cached is not None:
        return cached
    line = None
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            for index, text in enumerate(handle, start=1):
                if index == lineno:
                    line = text.strip()
                    break
    except OSError:
        line = None
    _SOURCE_LINE_CACHE[key] = line
    return line


def _format_exception_text(exc, tb):
    """Format the full exception, compatible with Python 3.10+ and older."""
    try:
        return "".join(traceback.format_exception(exc))
    except TypeError:
        return "".join(traceback.format_exception(type(exc), exc, tb))


def format_failure(exc, suite_path):
    """Render a complete, self-contained failure report for *exc*.

    Uses ``traceback.extract_tb`` to find the deepest executed frame (the
    failing file:line), prints that frame's source statement, and then prints
    the full traceback so the exception is never swallowed.
    """
    rendered = []
    rendered.append("FAILURE DETAIL: %s" % display_path(suite_path))
    rendered.append("  exception : %s: %s" % (type(exc).__name__, exc))

    tb = getattr(exc, "__traceback__", None)
    frames = traceback.extract_tb(tb) if tb is not None else []

    if isinstance(exc, SyntaxError):
        filename = getattr(exc, "filename", None) or suite_path
        lineno = getattr(exc, "lineno", 0) or 0
        rendered.append(
            "  failing at: %s:%s" % (display_path(filename), lineno)
        )
        text = getattr(exc, "text", None)
        if text:
            rendered.append("  statement : %s" % text.strip())
        else:
            statement = read_source_line(filename, lineno)
            if statement:
                rendered.append("  statement : %s" % statement)
    elif frames:
        deepest = frames[-1]
        rendered.append(
            "  failing at: %s:%d (in %s)"
            % (display_path(deepest.filename), deepest.lineno, deepest.name)
        )
        statement = deepest.line or read_source_line(
            deepest.filename, deepest.lineno
        )
        if statement:
            rendered.append("  statement : %s" % statement.strip())

        # If the deepest frame is a helper, the call site inside the suite is
        # usually the line a reader wants first; print it too when it differs.
        for frame in reversed(frames[:-1]):
            if os.path.abspath(frame.filename) == os.path.abspath(suite_path):
                call_statement = frame.line or read_source_line(
                    frame.filename, frame.lineno
                )
                rendered.append(
                    "  suite call: %s:%d (in %s)"
                    % (display_path(frame.filename), frame.lineno, frame.name)
                )
                if call_statement:
                    rendered.append("              %s" % call_statement.strip())
                break

    if isinstance(exc, SystemExit):
        rendered.append(
            "  note      : suite raised SystemExit(%r); a non-zero status is a"
            " FAILURE (a suite cannot fake success by exiting)" % (exc.code,)
        )

    rendered.append("")
    rendered.append("  full traceback (never swallowed):")
    formatted = _format_exception_text(exc, tb)
    for line in formatted.rstrip("\n").splitlines():
        rendered.append("    " + line)
    return "\n".join(rendered)


def format_read_failure(suite_path, exc):
    return "\n".join(
        [
            "FAILURE DETAIL: %s" % display_path(suite_path),
            "  exception : could not read suite source: %r" % (exc,),
            "  full traceback (never swallowed):",
            traceback.format_exc(),
        ]
    )


def self_check_optimization():
    """Prove, in memory, that optimize=0 keeps an assert and optimize=2 drops it.

    Returns ``(ok, lines)`` where *ok* is False only when optimize=0 failed to
    keep the assertion (in which case the whole run's evidence is void).
    """
    probe = (
        "def _runner_probe():\n"
        "    assert 1 + 1 == 2, 'self-check positive assertion'\n"
        "    assert False, 'self-check failure assertion'\n"
        "\n"
        "_runner_probe()\n"
    )
    lines = []
    optimize0_ok = False
    try:
        exec(
            compile(probe, "<runner-self-check-optimize0>", "exec", optimize=0),
            {"__name__": "__main__"},
        )
    except AssertionError as exc:
        optimize0_ok = "self-check failure assertion" in str(exc)
        lines.append(
            "  self-check : optimize=0 probe raised AssertionError %r -> asserts"
            " are LIVE in recompiled suites" % (str(exc),)
        )
    else:
        lines.append(
            "  self-check : ERROR -- optimize=0 probe did NOT raise; recompiled"
            " suites would be vacuous"
        )

    optimized_stripped = False
    try:
        exec(
            compile(probe, "<runner-self-check-optimize2>", "exec", optimize=2),
            {"__name__": "__main__"},
        )
    except AssertionError:
        optimized_stripped = False
        lines.append(
            "  self-check : WARNING -- optimize=2 probe raised; this interpreter"
            " does not strip asserts as expected"
        )
    else:
        optimized_stripped = True
        lines.append(
            "  self-check : optimize=2 probe did NOT raise -> Calibre's native"
            " -O2 mode would be VACUOUS (hazard confirmed)"
        )
    return (optimize0_ok and optimized_stripped), lines


# ---------------------------------------------------------------------------
# Per-suite process-state isolation
# ---------------------------------------------------------------------------
def snapshot_state():
    """Capture the process state a suite must not be allowed to leak."""
    return {
        "path": list(sys.path),
        "modules": dict(sys.modules),
        "cwd": os.getcwd(),
        "environ": dict(os.environ),
        "argv": list(sys.argv),
        "recursion_limit": sys.getrecursionlimit(),
    }


def restore_state(state):
    """Restore the process state captured by :func:`snapshot_state`."""
    # Threads and traces first: a leaked trace hook would observe the runner.
    try:
        sys.settrace(None)
    except Exception:
        pass
    try:
        sys.setprofile(None)
    except Exception:
        pass

    try:
        sys.path[:] = state["path"]
    except (AttributeError, TypeError):
        sys.path = list(state["path"])

    baseline = state["modules"]
    current = sys.modules
    # Two passes: deleting a module can trigger a late import that re-populates
    # sys.modules (e.g. a module finalizer), so sweep again before restoring.
    for _ in range(3):
        extra = [name for name in list(current.keys()) if name not in baseline]
        if not extra:
            break
        for name in extra:
            try:
                del current[name]
            except KeyError:
                pass
    for name in baseline:
        if name not in current:
            current[name] = baseline[name]
        elif current[name] is not baseline[name]:
            # A suite may have monkeypatched sys.modules (e.g. fake fastapi).
            current[name] = baseline[name]

    try:
        os.chdir(state["cwd"])
    except OSError:
        pass

    try:
        os.environ.clear()
        os.environ.update(state["environ"])
    except (AttributeError, TypeError):
        pass

    try:
        sys.argv[:] = state["argv"]
    except (AttributeError, TypeError):
        sys.argv = list(state["argv"])
    try:
        sys.setrecursionlimit(state["recursion_limit"])
    except (ValueError, RecursionError):
        pass


def _prepend_suite_paths():
    """Put repo root / backend / tests first on sys.path, without duplicates."""
    current = list(sys.path)
    ordered = [REPO_ROOT, BACKEND_DIR, TESTS_DIR]
    seen = set()
    merged = []
    for entry in ordered + current:
        key = os.path.normcase(os.path.abspath(entry)) if entry else entry
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    sys.path[:] = merged


# ---------------------------------------------------------------------------
# Suite execution
# ---------------------------------------------------------------------------
class SuiteResult:
    """Everything the summary needs to know about one suite."""

    def __init__(self, path):
        self.path = path
        self.ok = False
        self.exit_code = 1
        self.asserts_compiled = 0
        self.asserts_observed = 0
        self.asserts_untraceable = 0
        self.duration = 0.0
        self.failure_kind = None
        self.error = None
        self.count_mode = "unavailable"
        self.count_error = None
        self.captured_stdout = ""
        self.captured_stderr = ""
        self.run_mode = "optimize=0"


def _safe_getvalue(buffer):
    """Read a StringIO without assuming a suite left it open and unmodified."""
    try:
        return buffer.getvalue()
    except (ValueError, OSError, AttributeError):
        return ""


def _capture_stream(text, limit=MAX_CAPTURE_BYTES):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... captured output truncated by runner ...]\n"


def _print_captured(result):
    if result.captured_stdout:
        print("  --- captured stdout for %s ---" % display_path(result.path))
        print(_capture_stream(result.captured_stdout), end="")
        if not result.captured_stdout.endswith("\n"):
            print()
    if result.captured_stderr:
        print("  --- captured stderr for %s ---" % display_path(result.path))
        print(_capture_stream(result.captured_stderr), end="")
        if not result.captured_stderr.endswith("\n"):
            print()


def run_suite(path, counter):
    """Compile *path* with optimize=0 and execute it as a fresh ``__main__``."""
    result = SuiteResult(path)
    started = time.time()

    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            source = handle.read()
    except OSError as exc:
        result.failure_kind = "read"
        result.error = format_read_failure(path, exc)
        result.duration = time.time() - started
        return result

    # Static evidence: how many asserts exist in the source text.
    try:
        compiled, per_line, untraceable = collect_assert_stats(source, path)
    except SyntaxError:
        # Let compile() below produce the canonical SyntaxError report.
        compiled, per_line, untraceable = 0, {}, 0
    result.asserts_compiled = compiled
    result.asserts_untraceable = untraceable

    # Environment probes: a suite that reads ``__debug__`` (and has no bare
    # asserts to keep live) asserts Calibre's native -O2 environment.  An
    # optimize=0 recompile necessarily makes ``__debug__`` True, so such a
    # suite can only pass when executed at the interpreter's native level.
    # Because it contains zero bare asserts, running it natively loses no
    # checks at all -- and the choice is reported loudly below.
    native_probe = False
    if compiled == 0:
        try:
            native_probe = references_debug_constant(source, path)
        except SyntaxError:
            native_probe = False
    result.run_mode = "native" if native_probe else "optimize=0"
    optimize_level = -1 if native_probe else 0

    # The load-bearing step: recompile the source with optimisation stripped
    # (except for an assert-free environment probe, explained above).
    try:
        code = compile(source, path, "exec", optimize=optimize_level)
    except SyntaxError as exc:
        result.failure_kind = "syntax"
        result.error = format_failure(exc, path)
        result.duration = time.time() - started
        return result

    if native_probe:
        print(
            "  [NOTE] ENVIRONMENT-PROBE SUITE: %s references `__debug__` and"
            " contains 0 bare asserts." % display_path(path)
        )
        print(
            "         optimize=0 recompilation would make `__debug__` True and"
            " contradict the suite's own"
        )
        print(
            "         calibration check, so it is executed natively at"
            " optimize=%d instead." % sys.flags.optimize
        )
        print(
            "         No evidence is lost: there are no bare asserts to strip,"
            " and its explicit"
        )
        print("         raise-based checks run identically.")

    namespace = types.ModuleType("__main__")
    namespace.__file__ = path
    namespace.__cached__ = None
    module_dict = namespace.__dict__

    state = snapshot_state()
    sys.argv = [path]
    _prepend_suite_paths()

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr

    try:
        saved_stdout.flush()
    except (AttributeError, ValueError):
        pass
    try:
        saved_stderr.flush()
    except (AttributeError, ValueError):
        pass

    sys.stdout = captured_out
    sys.stderr = captured_err
    try:
        try:
            counter.start(code, per_line)
        except BaseException as exc:  # counting must never block the suite
            result.count_error = "%s: %s" % (type(exc).__name__, exc)

        try:
            exec(code, module_dict)
            result.ok = True
        except SystemExit as exc:
            code_value = exc.code
            if code_value is None:
                code_value = 0
            result.exit_code = code_value if isinstance(code_value, int) else 1
            if result.exit_code == 0:
                result.ok = True
            else:
                result.ok = False
                result.failure_kind = "exit"
                result.error = format_failure(exc, path)
        except AssertionError as exc:
            result.ok = False
            result.failure_kind = "assertion"
            result.exit_code = 1
            result.error = format_failure(exc, path)
        except KeyboardInterrupt as exc:
            result.ok = False
            result.failure_kind = "interrupt"
            result.exit_code = 1
            result.error = format_failure(exc, path)
        except BaseException as exc:
            result.ok = False
            result.failure_kind = "exception"
            result.exit_code = 1
            result.error = format_failure(exc, path)
    finally:
        try:
            counter.stop()
        except BaseException as exc:
            if result.count_error is None:
                result.count_error = "stop: %s: %s" % (type(exc).__name__, exc)
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        restore_state(state)
        try:
            saved_stdout.flush()
        except (AttributeError, ValueError):
            pass
        result.asserts_observed = counter.executed
        result.count_mode = counter.mode
        result.captured_stdout = _safe_getvalue(captured_out)
        result.captured_stderr = _safe_getvalue(captured_err)
        result.duration = time.time() - started

    if result.ok:
        result.exit_code = 0
        result.failure_kind = None
        result.error = None

    # ---- loud warning: bare asserts + direct -e means a vacuous run ------
    if result.asserts_compiled:
        print(
            "  [WARN] BARE ASSERTS: %d assert statement(s) in %s."
            % (result.asserts_compiled, display_path(path))
        )
        print(
            "         A direct `calibre-debug -e` run executes this suite with"
            " optimize=2,"
        )
        print(
            "         which compiles those asserts AWAY: it would print PASS"
            " while checking"
        )
        print(
            "         nothing (vacuous). This runner recompiles at optimize=0,"
            " so the"
        )
        print(
            "         assertions are live and the PASS/FAIL below is meaningful."
        )
    if result.asserts_untraceable:
        print(
            "  [WARN] %d assert statement(s) in %s do not begin their own line"
            " (e.g. `if x: assert y`);"
            % (result.asserts_untraceable, display_path(path))
        )
        print(
            "         line tracing cannot prove they ran, so they are excluded"
            " from the observed count."
        )
    if result.ok and per_line and result.asserts_observed == 0:
        print(
            "  [WARN] %d traceable assert line(s) compiled but ZERO executions"
            " observed for %s;"
            % (len(per_line), display_path(path))
        )
        print(
            "         the asserts may be unreachable, or instrumentation failed"
            " (mode=%s)." % (result.count_mode,)
        )
    if result.count_error:
        print(
            "  [WARN] assert instrumentation error for %s: %s"
            % (display_path(path), result.count_error)
        )

    if not result.ok:
        if result.failure_kind == "assertion":
            print(
                "  ^ an executed assert FAILED (asserts were live: optimize=0)"
            )
    return result


# ---------------------------------------------------------------------------
# Discovery / CLI
# ---------------------------------------------------------------------------
def discover_suites():
    """Return every regular ``tests/test_*.py`` file, deterministically sorted."""
    suites = []
    try:
        names = os.listdir(TESTS_DIR)
    except OSError:
        return suites
    for name in names:
        if not name.startswith(SUITE_PREFIX) or not name.endswith(SUITE_SUFFIX):
            continue
        path = os.path.join(TESTS_DIR, name)
        if os.path.isfile(path):
            suites.append(os.path.abspath(path))
    suites.sort(key=lambda item: item.lower())
    return suites


def resolve_suite_argument(argument):
    """Resolve an explicit suite path against cwd, repo root, and tests dir."""
    candidates = []
    if os.path.isabs(argument):
        candidates.append(argument)
    else:
        candidates.append(os.path.abspath(argument))
        candidates.append(os.path.join(REPO_ROOT, argument))
        candidates.append(os.path.join(TESTS_DIR, argument))
    for candidate in candidates:
        if os.path.isfile(candidate) and candidate.lower().endswith(SUITE_SUFFIX):
            return os.path.abspath(candidate)
    return None


def matches_filters(path, filters):
    haystack = (display_path(path) + " " + os.path.basename(path)).lower()
    return any(needle in haystack for needle in filters)


def _looks_like_path(argument):
    return (
        argument.lower().endswith(SUITE_SUFFIX)
        or "/" in argument
        or "\\" in argument
    )


def parse_arguments(argv):
    """Return ``(suites, verbose, show_list, error_message)``."""
    verbose = False
    show_list = False
    filters = []
    explicit = []

    for argument in argv[1:]:
        if argument in ("--help", "-h"):
            return None, verbose, show_list, "help"
        if argument in ("--verbose", "-v"):
            verbose = True
        elif argument in ("--list", "-l"):
            show_list = True
        elif argument.startswith("-"):
            return None, verbose, show_list, "unknown option: %s" % argument
        elif _looks_like_path(argument):
            resolved = resolve_suite_argument(argument)
            if resolved is None:
                return (
                    None,
                    verbose,
                    show_list,
                    "no such suite file: %s" % argument,
                )
            if resolved not in explicit:
                explicit.append(resolved)
        else:
            filters.append(argument.lower())

    if explicit and filters:
        selected = list(explicit)
        for suite in discover_suites():
            if suite not in selected and matches_filters(suite, filters):
                selected.append(suite)
    elif filters:
        selected = [
            suite for suite in discover_suites() if matches_filters(suite, filters)
        ]
        if not selected:
            return (
                None,
                verbose,
                show_list,
                "no suite matched filter(s): %s"
                % ", ".join(repr(item) for item in filters),
            )
    elif explicit:
        selected = list(explicit)
    else:
        selected = discover_suites()

    selected = sorted(set(selected), key=lambda item: display_path(item).lower())
    if not selected:
        return None, verbose, show_list, "no suites found (tests/test_*.py)"
    return selected, verbose, show_list, None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_header(suites, counter, self_check_lines):
    print("=" * 108)
    print("NovelTrax assert-aware test runner")
    print("=" * 108)
    print("  interpreter        : Python %s (calibre-debug -e)" % sys.version.split()[0])
    print(
        "  sys.flags.optimize : %r  <- Calibre's embedded interpreter"
        % (sys.flags.optimize,)
    )
    print("  __debug__          : %r" % (__debug__,))
    if __debug__:
        print("  native asserts     : enabled in this process; every suite is")
        print("                       STILL recompiled with optimize=0 for evidence")
    else:
        print(
            "  native asserts     : DISABLED (optimize=%d strips bare asserts)"
            % sys.flags.optimize
        )
        print(
            "                       -> every suite below is recompiled with"
            " optimize=0, except"
        )
        print(
            "                          zero-bare-assert `__debug__` environment"
            " probes (reported per suite)"
        )
    print("  suites selected    : %d" % len(suites))
    print("  repo root          : %s" % REPO_ROOT)
    if counter.mode == "sys.monitoring":
        print(
            "  execution tracing  : sys.monitoring LINE events (local to suite"
            " code), tool id %s" % counter.tool_id
        )
    elif counter.mode == "sys.settrace":
        print(
            "  execution tracing  : sys.settrace line tracer (sys.monitoring"
            " unavailable)"
        )
    else:
        print("  execution tracing  : UNAVAILABLE -- observed counts are unknown")
    for line in self_check_lines:
        print(line)
    print("=" * 108)


def print_suite_table(results):
    name_width = max(
        [len(display_path(result.path)) for result in results]
        + [len("suite"), len("GRAND TOTAL")]
    )
    header = "  %-*s  %6s  %17s  %17s  %10s" % (
        name_width,
        "suite",
        "exit",
        "asserts compiled",
        "asserts observed",
        "duration",
    )
    print()
    print("=" * 108)
    print("SUMMARY")
    print("=" * 108)
    print(header)
    print("  " + "-" * (len(header) - 2))
    total_compiled = 0
    total_observed = 0
    total_duration = 0.0
    for result in results:
        total_compiled += result.asserts_compiled
        total_observed += result.asserts_observed
        total_duration += result.duration
        print(
            "  %-*s  %6d  %17d  %17d  %9.2fs"
            % (
                name_width,
                display_path(result.path),
                result.exit_code,
                result.asserts_compiled,
                result.asserts_observed,
                result.duration,
            )
        )
    print("  " + "-" * (len(header) - 2))
    print(
        "  %-*s  %6s  %17d  %17d  %9.2fs"
        % (
            name_width,
            "GRAND TOTAL",
            "",
            total_compiled,
            total_observed,
            total_duration,
        )
    )
    return total_compiled, total_observed, total_duration


def print_totals(results, total_compiled, total_observed, wall_time, count_mode):
    passed = [result for result in results if result.ok]
    failed = [result for result in results if not result.ok]
    assert_suites = [result for result in results if result.asserts_compiled]
    untraceable = sum(result.asserts_untraceable for result in results)

    print()
    print("  suites selected  : %d" % len(results))
    print("  suites passed    : %d" % len(passed))
    print("  suites failed    : %d" % len(failed))
    print(
        "  asserts compiled : %d  (ast.Assert nodes, compiled with optimize=0)"
        % total_compiled
    )
    print(
        "  asserts observed : %d  (real interpreter line events; mode=%s)"
        % (total_observed, count_mode)
    )
    if untraceable:
        print(
            "  untraceable      : %d assert(s) not at line start (excluded from"
            " observed count)" % untraceable
        )
    print("  wall time        : %.2fs" % wall_time)
    native_results = [
        result for result in results if result.run_mode != "optimize=0"
    ]
    if native_results:
        print(
            "  native-mode runs : %d  (zero-bare-assert environment probes;"
            " documented above)" % len(native_results)
        )
        for result in native_results:
            print(
                "           %-60s mode=%s, %d bare asserts"
                % (
                    display_path(result.path),
                    result.run_mode,
                    result.asserts_compiled,
                )
            )
    if assert_suites:
        print()
        print(
            "  [WARN] %d selected suite(s) contain bare asserts that a direct"
            " `calibre-debug -e` run"
            % len(assert_suites)
        )
        print(
            "         would compile away. This runner's optimize=0 recompile is"
            " what makes them real:"
        )
        for result in assert_suites:
            print(
                "           %-60s %d compiled / %d observed"
                % (
                    display_path(result.path),
                    result.asserts_compiled,
                    result.asserts_observed,
                )
            )
    print()
    print("=" * 108)
    if failed:
        print(
            "RESULT: FAIL -- %d of %d suite(s) failed with assertions compiled"
            " live (optimize=0)." % (len(failed), len(results))
        )
        for result in failed:
            print(
                "         FAILED: %s (%s)"
                % (display_path(result.path), result.failure_kind or "failure")
            )
        print("=" * 108)
        return 1
    print(
        "RESULT: PASS -- every selected suite passed; bare asserts were compiled"
        " live at optimize=0"
    )
    print(
        "         (assert-free `__debug__` environment probes ran natively and"
        " are reported above)."
    )
    print("=" * 108)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv):
    # Requirement: the interpreter's optimisation level is printed on EVERY
    # run, including --list / --help / usage errors.
    print(
        "run_tests: sys.flags.optimize=%r  __debug__=%r"
        % (sys.flags.optimize, __debug__)
    )
    suites, verbose, show_list, error = parse_arguments(argv)
    if error == "help":
        print(__doc__)
        return 0
    if error:
        print("run_tests: %s" % error, file=sys.stderr)
        print(
            "run_tests: usage: calibre-debug.exe -e tests\\run_tests.py"
            " [substring ...] [--verbose] [--list]",
            file=sys.stderr,
        )
        return 2
    if not suites:
        print("run_tests: no suites found", file=sys.stderr)
        return 2

    if show_list:
        for suite in suites:
            print(display_path(suite))
        return 0

    optimize_ok, self_check_lines = self_check_optimization()
    if not optimize_ok:
        for line in self_check_lines:
            print(line, file=sys.stderr)
        print(
            "run_tests: ABORT -- optimization self-check failed; assert evidence"
            " would be untrustworthy.",
            file=sys.stderr,
        )
        return 1

    counter = AssertCounter()
    print_header(suites, counter, self_check_lines)

    results = []
    started_all = time.time()
    interrupted = False
    try:
        total = len(suites)
        for index, suite in enumerate(suites, start=1):
            print()
            print("-" * 108)
            print("[%d/%d] RUN  %s" % (index, total, display_path(suite)))
            print("-" * 108)

            try:
                result = run_suite(suite, counter)
            except BaseException as exc:
                # Last-resort isolation: a failure in the runner's own glue for
                # this suite must not abort the remaining suites.
                result = SuiteResult(suite)
                result.failure_kind = "runner"
                result.error = format_failure(exc, suite)
                result.duration = 0.0
                print(
                    "  [ERROR] runner-level failure while executing %s"
                    % display_path(suite),
                    file=sys.stderr,
                )
            results.append(result)

            if not result.ok:
                # Failure detail goes to stderr, and is repeated in the capture
                # block below so it cannot be lost in a redirected run.
                print(result.error, file=sys.stderr)
                sys.stderr.flush()
            if verbose or not result.ok:
                _print_captured(result)

            status = "PASS" if result.ok else "FAIL"
            print(
                "[%d/%d] %s  %s  exit=%d  compiled=%d  observed=%d  mode=%s"
                "  %.2fs"
                % (
                    index,
                    total,
                    status,
                    display_path(suite),
                    result.exit_code,
                    result.asserts_compiled,
                    result.asserts_observed,
                    result.run_mode,
                    result.duration,
                )
            )
    except KeyboardInterrupt:
        interrupted = True
        print(
            "\nrun_tests: interrupted by the user; partial results only",
            file=sys.stderr,
        )
    finally:
        counter.release()

    wall_time = time.time() - started_all
    total_compiled, total_observed, _ = print_suite_table(results)
    exit_code = print_totals(
        results,
        total_compiled,
        total_observed,
        wall_time,
        counter.mode,
    )
    if interrupted:
        exit_code = 130
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except KeyboardInterrupt:
        print("run_tests: interrupted", file=sys.stderr)
        raise SystemExit(130)
