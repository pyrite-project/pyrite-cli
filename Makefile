.PHONY: install dev test lint typecheck clean build

SHELL := /bin/bash

install:
	pip install -e .

dev:
	pip install -e ".[dev]"
	pip install pytest mypy ruff pre-commit

test:
	pytest test/ -v --tb=short

test-all:
	pytest test/ tests/ -v --tb=short

lint:
	ruff check cli/ test/ tests/
	ruff format --check cli/ test/ tests/

format:
	ruff format cli/ test/ tests/
	ruff check --fix cli/ test/ tests/

typecheck:
	mypy cli/

precommit:
	pre-commit install
	pre-commit run --all-files

clean:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

build: clean
	pip install build
	python -m build

.PHONY: help
help:
	@echo "Targets:"
	@echo "  install    - Install pyrite-cli in editable mode"
	@echo "  dev        - Install dev dependencies"
	@echo "  test       - Run unit tests"
	@echo "  test-all   - Run all tests (unit + integration)"
	@echo "  lint       - Run ruff linter + formatter check"
	@echo "  format     - Auto-format code with ruff"
	@echo "  typecheck  - Run mypy type checker"
	@echo "  precommit  - Install & run pre-commit hooks"
	@echo "  clean      - Remove build artifacts"
	@echo "  build      - Build distribution packages"
