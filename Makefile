.PHONY: help install install-airflow test test-cov lint format typecheck dag-check airflow-up airflow-clean check clean

# Local Airflow state lives inside the repo so it doesn't pollute ~/airflow.
AIRFLOW_HOME_LOCAL := $(CURDIR)/.airflow

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
	@echo "  airflow-up      run 'airflow standalone' against airflow/dags (UI on :8080)"
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

# Spin up a local Airflow (scheduler + triggerer + webserver) using SQLite.
# Auto-creates an admin user on first start and prints the password to the
# console and to $(AIRFLOW_HOME_LOCAL)/standalone_admin_password.txt.
# UI: http://localhost:8080
#
# OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES is required on macOS: gunicorn forks
# webserver workers, and any Objective-C framework already loaded in the parent
# (Foundation, SystemConfiguration, Security/Keychain, ...) will SIGSEGV the
# child unless this flag is set. Without it the webserver enters an infinite
# worker-crash loop and never serves a request.
# NO_PROXY=* avoids the same crash being re-triggered by macOS proxy lookups.
airflow-up:
	@mkdir -p $(AIRFLOW_HOME_LOCAL)
	AIRFLOW_HOME=$(AIRFLOW_HOME_LOCAL) \
	AIRFLOW__CORE__DAGS_FOLDER=$(CURDIR)/airflow/dags \
	AIRFLOW__CORE__LOAD_EXAMPLES=False \
	AIRFLOW__CORE__EXECUTOR=SequentialExecutor \
	OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
	NO_PROXY='*' \
	poetry run airflow standalone

airflow-clean:
	rm -rf $(AIRFLOW_HOME_LOCAL)

check: lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
