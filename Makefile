.PHONY: help up down seed pipeline api test clean

# Default python environment if not overridden
PYTHON ?= .venv/bin/python
UVICORN ?= .venv/bin/uvicorn
PYTEST ?= .venv/bin/pytest

help:
	@echo "CRM Sidecar Setup & Execution Commands"
	@echo "--------------------------------------"
	@echo "make up        - Start the PostgreSQL databases (Source + Sidecar) via Docker"
	@echo "make down      - Shut down Docker containers"
	@echo "make seed      - Reset and seed the Source DB with local test data (Task 0)"
	@echo "make pipeline  - Run the full sync, score, and AI summary pipeline (Tasks 1-3)"
	@echo "make api       - Start the FastAPI UI / Swagger dashboard (Task 4)"
	@echo "make test      - Run all Python unit tests"
	@echo "make clean     - Shut down Docker and remove local data volumes"

up:
	docker compose up -d
	@echo "Databases started! Waiting 5s for Postgres to accept connections..."
	@sleep 5

down:
	docker compose down

seed:
	$(PYTHON) seed/generate_seed.py

pipeline:
	$(PYTHON) scripts/run_pipeline.py

api:
	$(UVICORN) interface.main:app --reload --port 8000

test:
	$(PYTEST) tests/

clean:
	docker compose down -v
	rm -rf .pytest_cache
	find . -type d -name __pycache__ -exec rm -r {} +
