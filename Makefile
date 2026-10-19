.PHONY: install check test lint types lock
install:
	uv venv --python 3.12 && uv pip install -e '.[dev]'
test:
	pytest -q
lint:
	ruff check src tests
types:
	mypy
check: lint types test
lock:
	uv lock
