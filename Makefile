.PHONY: help install install-airflow test test-cov lint format typecheck dag-check airflow-up airflow-down airflow-clean check clean

# Local Airflow state lives inside the repo so it doesn't pollute ~/airflow.
AIRFLOW_HOME_LOCAL := $(CURDIR)/.airflow
# Override with `make airflow-up AIRFLOW_PORT=8081` if 8088 is also taken.
AIRFLOW_PORT ?= 8088

help:
	@echo "Common targets:"
	@echo "  install         poetry install (runtime + dev)"
	@echo "  install-airflow poetry install with the optional 'airflow' group"
	@echo "  test            run pytest (skips DAG-parse test if airflow not installed)"
	@echo "  test-cov        run pytest with coverage"
	@echo "  lint            ruff check"
	@echo "  format          black + ruff --fix"
	@echo "  typecheck       mypy on src/"
	@echo "  dag-check       parse the Airflow DAG (requires the airflow group)"
	@echo "  airflow-up      run a local Airflow (scheduler + triggerer + Flask webserver) on :$(AIRFLOW_PORT)"
	@echo "  airflow-down    kill anything listening on :$(AIRFLOW_PORT) and any stray airflow procs"
	@echo "  airflow-clean   remove the local .airflow/ state directory"
	@echo "  check           lint + typecheck + test"

install:
	poetry config virtualenvs.in-project true --local
	poetry install --with dev

install-airflow:
	poetry config virtualenvs.in-project true --local
	poetry install --with dev,airflow

test:
	poetry run pytest

test-cov:
	poetry run pytest --cov

lint:
	poetry run ruff check src tests

format:
	poetry run black src tests
	poetry run ruff check --fix src tests

typecheck:
	poetry run mypy

dag-check:
	poetry run python -c "from airflow.models import DagBag; \
db = DagBag(dag_folder='airflow/dags', include_examples=False); \
assert not db.import_errors, db.import_errors; \
print('DAGs parsed:', list(db.dags))"

# Spin up a local Airflow (scheduler + triggerer + webserver) for browsing the
# DAG. Implemented in scripts/airflow_local.sh because it has to orchestrate
# three subprocesses with a signal-trap cleanup, which is painful in Make.
#
# Why not 'airflow standalone'?  standalone uses gunicorn for the webserver,
# and gunicorn's fork()-based workers SIGSEGV in a tight loop on macOS / Apple
# Silicon (arm64 C-extension wheels + dyld interposers). The script uses
# 'airflow webserver --debug' instead, which runs Flask's dev server in a
# single process and never forks, sidestepping the entire problem.
#
# UI:    http://localhost:$(AIRFLOW_PORT)  (default 8088; override per call)
# Login: admin / admin (override with ADMIN_USER / ADMIN_PASS env vars)
airflow-up:
	@AIRFLOW_HOME_LOCAL=$(AIRFLOW_HOME_LOCAL) AIRFLOW_PORT=$(AIRFLOW_PORT) \
		bash $(CURDIR)/scripts/airflow_local.sh

# Best-effort teardown: kill anything bound to the Airflow port plus any stray
# scheduler/triggerer/standalone processes from a previous run that crashed
# without unwinding (e.g. orphaned gunicorn after a SIGSEGV loop).
airflow-down:
	-@lsof -nP -iTCP:$(AIRFLOW_PORT) -sTCP:LISTEN -t 2>/dev/null | xargs -r kill -9
	-@pkill -f "airflow standalone" 2>/dev/null || true
	-@pkill -f "airflow scheduler"  2>/dev/null || true
	-@pkill -f "airflow triggerer"  2>/dev/null || true
	-@pkill -f "airflow webserver"  2>/dev/null || true
	-@pkill -f "gunicorn.*airflow"  2>/dev/null || true
	@echo "Airflow processes stopped (port $(AIRFLOW_PORT) freed)."

airflow-clean: airflow-down
	rm -rf $(AIRFLOW_HOME_LOCAL)

check: lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
