.PHONY: test test-fast lint

test:
	python -m pytest

test-fast:
	python -m pytest tests/unit tests/property -q

lint:
	python -m ruff check src/ tests/
	python -m mypy

# `make migrate` lands with the first migration (M0, V0.4).
