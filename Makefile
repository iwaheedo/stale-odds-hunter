.PHONY: setup run dashboard test lint format backtest replay clean

setup:
	python -m pip install -e ".[dev]"

run:
	python -m src.main run

dashboard:
	streamlit run src/dashboard/streamlit_app.py --server.port=8501

test:
	python -m pytest tests/ -v

lint:
	ruff check src/ tests/
	mypy src/

format:
	ruff format src/ tests/
	ruff check --fix src/ tests/

backtest:
	python -m src.main backtest

replay:
	python -m src.main replay

clean:
	rm -rf data/sqlite/*.db data/duckdb/*.duckdb
	find . -type d -name __pycache__ -exec rm -rf {} +
