.PHONY: test test-fast lint

test:
	python -m pytest

test-fast:
	python -m pytest tests/unit -q -x

lint:
	python -m ruff check .
	python -m mypy
