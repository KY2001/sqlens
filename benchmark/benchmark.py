#!/usr/bin/env python3
"""Benchmark SQLFluff and Sqruff on real upstream SQL suites."""

from __future__ import annotations

import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "benchmark" / ".cache"
TOOLS = ("sqlfluff", "sqruff")
DIALECTS = ("postgres", "mysql", "sqlite")
RUNS = 1
WARMUPS = 0
TIMEOUT = 300.0
LIMIT_FILES: int | None = None
REFRESH_CACHE = False
SIGNAL_EXIT_OFFSET = 128


@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    ref: str
    sparse_paths: tuple[str, ...]


@dataclass(frozen=True)
class Suite:
    name: str
    repo: str
    path: str
    pattern: str
    dialect: str


@dataclass(frozen=True)
class StagedSuite:
    suite: Suite
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


REPOS = {
    "sqlfluff": Repo(
        name="sqlfluff",
        url="https://github.com/sqlfluff/sqlfluff.git",
        ref="main",
        sparse_paths=tuple(f"test/fixtures/dialects/{dialect}" for dialect in DIALECTS),
    ),
    "sqruff": Repo(
        name="sqruff",
        url="https://github.com/quarylabs/sqruff.git",
        ref="main",
        sparse_paths=tuple(
            f"crates/lib-dialects/test/fixtures/dialects/{dialect}"
            for dialect in DIALECTS
        ),
    ),
    "postgres": Repo(
        name="postgres",
        url="https://github.com/postgres/postgres.git",
        ref="master",
        sparse_paths=("src/test/regress/sql",),
    ),
    "mysql": Repo(
        name="mysql",
        url="https://github.com/mysql/mysql-server.git",
        ref="trunk",
        sparse_paths=("mysql-test/t",),
    ),
    "sqlite": Repo(
        name="sqlite",
        url="https://github.com/sqlite/sqlite.git",
        ref="master",
        sparse_paths=("test",),
    ),
    "sakila": Repo(
        name="sakila",
        url="https://github.com/jOOQ/sakila.git",
        ref="main",
        sparse_paths=("mysql-sakila-db", "postgres-sakila-db", "sqlite-sakila-db"),
    ),
}

SUITES = (
    Suite(
        "sqlfluff-postgres",
        "sqlfluff",
        "test/fixtures/dialects/postgres",
        "*.sql",
        "postgres",
    ),
    Suite(
        "sqlfluff-mysql",
        "sqlfluff",
        "test/fixtures/dialects/mysql",
        "*.sql",
        "mysql",
    ),
    Suite(
        "sqlfluff-sqlite",
        "sqlfluff",
        "test/fixtures/dialects/sqlite",
        "*.sql",
        "sqlite",
    ),
    Suite(
        "sqruff-postgres",
        "sqruff",
        "crates/lib-dialects/test/fixtures/dialects/postgres",
        "**/*.sql",
        "postgres",
    ),
    Suite(
        "sqruff-mysql",
        "sqruff",
        "crates/lib-dialects/test/fixtures/dialects/mysql",
        "**/*.sql",
        "mysql",
    ),
    Suite(
        "sqruff-sqlite",
        "sqruff",
        "crates/lib-dialects/test/fixtures/dialects/sqlite",
        "**/*.sql",
        "sqlite",
    ),
    Suite("postgres-regress", "postgres", "src/test/regress/sql", "*.sql", "postgres"),
    Suite("mysql-tests", "mysql", "mysql-test/t", "*.test", "mysql"),
    Suite("sqlite-tests", "sqlite", "test", "*.test", "sqlite"),
    Suite("sakila-mysql", "sakila", "mysql-sakila-db", "*.sql", "mysql"),
    Suite("sakila-postgres", "sakila", "postgres-sakila-db", "*.sql", "postgres"),
    Suite("sakila-sqlite", "sakila", "sqlite-sakila-db", "*.sql", "sqlite"),
)


def main() -> int:
    fetch_missing_repos(CACHE, refresh=REFRESH_CACHE)
    suites = stage_suites(CACHE, LIMIT_FILES)
    print_suites(suites)
    results = run_benchmark(suites)
    print_results(results)

    return 0


def fetch_missing_repos(cache_dir: Path, *, refresh: bool) -> None:
    repos = {suite.repo for suite in SUITES}
    for name in sorted(repos):
        repo = REPOS[name]
        target = cache_dir / "repos" / repo.name

        if refresh and target.exists():
            shutil.rmtree(target)
        if repo_ready(target, repo):
            continue

        if target.exists():
            shutil.rmtree(target)

        print(f"fetch {repo.name} ({repo.ref})")
        target.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--quiet", str(target)])
        run(["git", "-C", str(target), "remote", "add", "origin", repo.url])
        run(["git", "-C", str(target), "sparse-checkout", "init", "--cone"])
        run(["git", "-C", str(target), "sparse-checkout", "set", *repo.sparse_paths])
        run(
            [
                "git",
                "-C",
                str(target),
                "fetch",
                "--quiet",
                "--depth",
                "1",
                "origin",
                repo.ref,
            ],
        )
        run(["git", "-C", str(target), "checkout", "--quiet", "--detach", "FETCH_HEAD"])


def repo_ready(path: Path, repo: Repo) -> bool:
    return (path / ".git").exists() and all(
        (path / item).exists() for item in repo.sparse_paths
    )


def stage_suites(cache_dir: Path, limit: int | None) -> list[StagedSuite]:
    stage_root = cache_dir / "stage"
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True)

    staged = []
    for suite in SUITES:
        repo_root = cache_dir / "repos" / suite.repo
        source_root = repo_root / suite.path
        files = sorted(source_root.glob(suite.pattern))
        if limit is not None:
            files = files[:limit]
        if not files:
            message = f"no files matched {source_root / suite.pattern}"
            raise SystemExit(message)

        target_root = stage_root / suite.name
        target_root.mkdir()
        for source in files:
            target = target_root / source.relative_to(source_root).with_suffix(".sql")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        staged.append(
            StagedSuite(
                suite=suite,
                path=target_root,
                files=len(files),
                bytes=sum(file.stat().st_size for file in files),
            ),
        )
    return staged


def command(tool: str, suite: StagedSuite) -> list[str]:
    if tool == "sqlfluff":
        return [
            "uv",
            "run",
            "sqlfluff",
            "lint",
            "--dialect",
            suite.suite.dialect,
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
        suite.suite.dialect,
        "--format",
        "none",
        str(suite.path),
    ]


def run_benchmark(
    suites: list[StagedSuite],
) -> list[BenchmarkResult]:
    rows: list[BenchmarkResult] = []
    for suite in suites:
        for tool in TOOLS:
            cmd = command(tool, suite)
            for index in range(WARMUPS):
                measurement = timed(cmd, TIMEOUT)
                print(
                    f"{suite.suite.name:<17} {tool:<8} warmup "
                    f"{index + 1}/{WARMUPS}: {measurement.elapsed:.3f}s "
                    f"{measurement.status}",
                )

            timings: list[float] = []
            statuses: list[str] = []
            failed = False
            for index in range(RUNS):
                measurement = timed(cmd, TIMEOUT)
                timings.append(measurement.elapsed)
                statuses.append(measurement.status)
                failed = failed or measurement.failed
                print(
                    f"{suite.suite.name:<17} {tool:<8} run "
                    f"{index + 1}/{RUNS}: {measurement.elapsed:.3f}s "
                    f"{measurement.status}",
                )

            rows.append(
                BenchmarkResult(
                    suite=suite.suite.name,
                    dialect=suite.suite.dialect,
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

    return Measurement(time.perf_counter() - started, status_for(code), code != 0)


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


def print_suites(suites: list[StagedSuite]) -> None:
    rows = [
        [
            suite.suite.name,
            suite.suite.dialect,
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
        speedup = (
            f"{baseline / row.median:.2f}x"
            if baseline is not None and row.median and not row.failed
            else ""
        )
        throughput = f"{row.files / row.median:.1f}" if not row.failed else ""
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

    failed = [row for row in rows if row.failed]
    if failed:
        print()
        print("failed rows are timed but excluded from throughput and speedup")


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


def run(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        command_text = " ".join(cmd)
        message = f"command failed: {command_text}\n{output}"
        raise SystemExit(message)


if __name__ == "__main__":
    sys.exit(main())
