.PHONY: check test lint fmt secrets gitleaks selftest

check:
	bash scripts/check.sh

test:
	python -m pytest

lint:
	python -m ruff check src tests && python -m ruff format --check src tests && python -m mypy

fmt:
	python -m ruff format src tests && python -m ruff check --fix src tests

secrets:
	bash scripts/secret_scan.sh --tree && bash scripts/gitleaks.sh history

selftest:
	python -m energy_analyzer selftest
