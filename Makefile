# Task
.PHONY: init
init:
	uv sync

.PHONY: benchmark
benchmark:
	uv run benchmark/benchmark.py

.PHONY: format
format:
	uv run -m ruff format
	uv run -m ruff check --fix

.PHONY: lint
lint:
	uv run -m ruff check
	uv run -m ty check

.PHONY: all
all: init format lint
