#!/usr/bin/env python3
"""Benchmark SQLFluff and Sqruff on real upstream SQL suites."""

from __future__ import annotations

import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTSUIT = ROOT / "benchmark" / "testsuit"
TOOLS = ("sqlfluff", "sqruff")
RUNS = 1
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
    status: str
    failed: bool


@dataclass(frozen=True)
class BenchmarkResult:
    suite: str
    dialect: str
    tool: str
    files: int
    bytes: int
    median: float
    mean: float
    min_elapsed: float
    max_elapsed: float
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
            if not repo_dir.is_dir():
                continue
            files = sorted(repo_dir.glob("**/*.sql"))
            if not files:
                continue
            suites.append(
                Suite(
                    name=f"{repo_dir.name}-{dialect_dir.name}",
                    dialect=dialect_dir.name,
                    path=repo_dir,
                    files=len(files),
                    bytes=sum(file.stat().st_size for file in files),
                ),
            )

    if not suites:
        raise SystemExit(f"no suites found under {testsuit_root}")
    return suites


def command(tool: str, suite: Suite) -> list[str]:
    if tool == "sqlfluff":
        return [
            "uv",
            "run",
            "sqlfluff",
            "lint",
            "--dialect",
            suite.dialect,
            "--templater",
            "raw",
            "--format",
            "none",
            "--nofail",
            "--disable-progress-bar",
            "--ignore-local-config",
            "--ignore",
            "parsing,templating",
            str(suite.path),
        ]

    return [
        "uv",
        "run",
        "sqruff",
        "lint",
        "--dialect",
        suite.dialect,
        "--format",
        "none",
        str(suite.path),
    ]


def run_benchmark(
    suites: list[Suite],
) -> list[BenchmarkResult]:
    rows: list[BenchmarkResult] = []
    for suite in suites:
        for tool in TOOLS:
            cmd = command(tool, suite)

            timings: list[float] = []
            statuses: list[str] = []
            failed = False
            for index in range(RUNS):
                measurement = timed(cmd, TIMEOUT_SEC)
                timings.append(measurement.elapsed)
                statuses.append(measurement.status)
                failed = failed or measurement.failed
                print(
                    f"{suite.name:<17} {tool:<8} run "
                    f"{index + 1}/{RUNS}: {measurement.elapsed:.3f}s "
                    f"{measurement.status}",
                )

            rows.append(
                BenchmarkResult(
                    suite=suite.name,
                    dialect=suite.dialect,
                    tool=tool,
                    files=suite.files,
                    bytes=suite.bytes,
                    median=statistics.median(timings),
                    mean=statistics.fmean(timings),
                    min_elapsed=min(timings),
                    max_elapsed=max(timings),
                    status=",".join(sorted(set(statuses))),
                    failed=failed,
                    command=tuple(cmd),
                ),
            )
    return rows


def timed(cmd: list[str], timeout: float) -> Measurement:
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return Measurement(
            elapsed=time.perf_counter() - started,
            status="timeout",
            failed=True,
        )
    except OSError as error:
        return Measurement(
            elapsed=time.perf_counter() - started,
            status=f"error:{error.strerror}",
            failed=True,
        )

    return Measurement(
        elapsed=time.perf_counter() - started,
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
            f"{suite.bytes / 1024:.1f}",
        ]
        for suite in suites
    ]
    print()
    table(["suite", "dialect", "files", "KiB"], rows)
    print()


def print_results(rows: list[BenchmarkResult]) -> None:
    baselines = {
        row.suite: row.median
        for row in rows
        if row.tool == "sqlfluff" and not row.failed
    }
    display = []
    for row in rows:
        baseline = baselines.get(row.suite)
        if row.failed:
            throughput = "-"
            speedup = "-"
        else:
            throughput = f"{row.files / row.median:.1f}"
            speedup = (
                f"{baseline / row.median:.2f}x" if baseline and row.median else "-"
            )
        display.append(
            [
                row.suite,
                row.dialect,
                row.tool,
                str(row.files),
                f"{row.median:.3f}s",
                f"{row.mean:.3f}s",
                f"{row.min_elapsed:.3f}s",
                f"{row.max_elapsed:.3f}s",
                throughput,
                speedup,
                row.status,
            ],
        )

    print()
    table(
        [
            "suite",
            "dialect",
            "tool",
            "files",
            "median",
            "mean",
            "min",
            "max",
            "files/s",
            "vs sqlfluff",
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
