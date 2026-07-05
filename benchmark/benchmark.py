#!/usr/bin/env python3
"""Benchmark SQLFluff and Sqruff on real upstream SQL suites."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import jc

ROOT = Path(__file__).resolve().parents[1]
TESTSUIT = ROOT / "benchmark" / "testsuit"
WORKSPACE = ROOT / "benchmark" / "workspace"
DISABLED_SUITES = frozenset({"sakila"})
TOOLS = ("sqlfluff", "sqruff")
ACTIONS = ("lint", "format")
TIMEOUT_SEC = 300
ACCEPTED_EXIT_CODES = {0, 1}
SIGNAL_EXIT_OFFSET = 128


@dataclass(frozen=True)
class Suite:
    name: str
    dialect: str
    path: Path
    files: int
    bytes: int


@dataclass(frozen=True)
class Measurement:
    elapsed: float
    max_rss_kib: int
    status: str
    failed: bool


@dataclass(frozen=True)
class BenchmarkResult:
    suite: str
    dialect: str
    tool: str
    action: str
    files: int
    bytes: int
    time: float
    max_rss_kib: int
    status: str
    failed: bool
    command: tuple[str, ...]


def main() -> int:
    suites = discover_suites(TESTSUIT)
    print_suites(suites)
    results = run_benchmark(suites)
    print_results(results)

    return 0


def discover_suites(testsuit_root: Path) -> list[Suite]:
    """Build a suite from each testsuit/<dialect>/<repo> directory of SQL."""
    suites = []
    for dialect_dir in sorted(testsuit_root.iterdir()):
        dialect = dialect_dir.name
        if dialect is None or not dialect_dir.is_dir():
            continue
        for repo_dir in sorted(dialect_dir.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name in DISABLED_SUITES:
                continue
            files = sorted(repo_dir.glob("**/*.sql"))
            if not files:
                continue
            suites.append(
                Suite(
                    name=f"{dialect_dir.name}/{repo_dir.name}",
                    dialect=dialect_dir.name,
                    path=repo_dir,
                    files=len(files),
                    bytes=sum(file.stat().st_size for file in files),
                ),
            )

    if not suites:
        raise SystemExit(f"no suites found under {testsuit_root}")
    return suites


def command(action: str, tool: str, dialect: str, path: Path) -> list[str]:
    subcommand = "fix" if action == "format" else "lint"
    if tool == "sqlfluff":
        base = ["uv", "run", "sqlfluff", subcommand, "--dialect", dialect]
        if action == "lint":
            base += ["--format", "none"]
        base += ["--disable-progress-bar", "--ignore-local-config", str(path)]
        return base

    return [
        "uv",
        "run",
        "sqruff",
        subcommand,
        "--dialect",
        dialect,
        "--format",
        "none",
        str(path),
    ]


def run_benchmark(
    suites: list[Suite],
) -> list[BenchmarkResult]:
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)

    for suite in suites:
        for tool in TOOLS:
            shutil.copytree(suite.path, WORKSPACE / tool / suite.name)

    rows: list[BenchmarkResult] = []
    for action in ACTIONS:
        for suite in suites:
            for tool in TOOLS:
                workdir = WORKSPACE / tool / suite.name
                cmd = command(action, tool, suite.dialect, workdir)

                measurement = timed(cmd, TIMEOUT_SEC)
                print(
                    f"{suite.name:<17} {tool:<8} {action:<6}: "
                    f"{measurement.elapsed:.3f}s {measurement.status}",
                )

                rows.append(
                    BenchmarkResult(
                        suite=suite.name,
                        dialect=suite.dialect,
                        tool=tool,
                        action=action,
                        files=suite.files,
                        bytes=suite.bytes,
                        time=measurement.elapsed,
                        max_rss_kib=measurement.max_rss_kib,
                        status=measurement.status,
                        failed=measurement.failed,
                        command=tuple(cmd),
                    ),
                )
    return rows


def timed(cmd: list[str], timeout: float) -> Measurement:
    with tempfile.NamedTemporaryFile("r", suffix=".time") as report:
        wrapped = ["/usr/bin/time", "--verbose", "--output", report.name, *cmd]
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                wrapped,
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return Measurement(
                elapsed=time.perf_counter() - started,
                max_rss_kib=0,
                status="timeout",
                failed=True,
            )
        except OSError as error:
            return Measurement(
                elapsed=time.perf_counter() - started,
                max_rss_kib=0,
                status=f"error:{error.strerror}",
                failed=True,
            )

        # GNU time prepends a non-indented "Command exited ..." line on a
        # non-zero exit (a lint finding); jc only parses the tab-indented body.
        body = "".join(line for line in report if line.startswith("\t"))
        parsed = cast("dict[str, Any]", jc.parse("time", body))

    return Measurement(
        elapsed=parsed["elapsed_time_total_seconds"],
        max_rss_kib=parsed["maximum_resident_set_size"],
        status=status_for(code),
        failed=code not in ACCEPTED_EXIT_CODES,
    )


def status_for(code: int) -> str:
    if code < 0:
        return signal_status(-code)
    if code > SIGNAL_EXIT_OFFSET:
        return signal_status(code - SIGNAL_EXIT_OFFSET)
    return f"exit:{code}"


def signal_status(number: int) -> str:
    try:
        name = signal.Signals(number).name.removeprefix("SIG")
    except ValueError:
        name = str(number)
    return f"signal:{name}"


def print_suites(suites: list[Suite]) -> None:
    rows = [
        [
            suite.name,
            suite.dialect,
            str(suite.files),
        ]
        for suite in suites
    ]
    print()
    table(["suite", "dialect", "files"], rows)
    print()


def print_results(rows: list[BenchmarkResult]) -> None:
    baselines = {
        (row.suite, row.action): row.time
        for row in rows
        if row.tool == "sqlfluff" and not row.failed
    }
    mem_baselines = {
        (row.suite, row.action): row.max_rss_kib
        for row in rows
        if row.tool == "sqlfluff" and not row.failed
    }
    display = []
    for row in rows:
        baseline = baselines.get((row.suite, row.action))
        mem_baseline = mem_baselines.get((row.suite, row.action))
        if row.failed:
            speedup = "-"
            mem_ratio = "-"
        else:
            speedup = f"{baseline / row.time:.2f}x" if baseline and row.time else "-"
            mem_ratio = (
                f"{mem_baseline / row.max_rss_kib:.2f}x"
                if mem_baseline and row.max_rss_kib
                else "-"
            )
        display.append(
            [
                row.suite,
                row.tool,
                row.action,
                str(row.files),
                f"{row.max_rss_kib / 1024:.1f} MiB",
                mem_ratio,
                f"{row.time:.3f}s",
                speedup,
                row.status,
            ],
        )

    print()
    table(
        [
            "suite",
            "tool",
            "action",
            "files",
            "max Rss",
            "memory vs sqlfluff",
            "time",
            "time vs sqlfluff",
            "status",
        ],
        display,
    )


def table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
    )
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


if __name__ == "__main__":
    sys.exit(main())
