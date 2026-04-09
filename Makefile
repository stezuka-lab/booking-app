# Windows では make が無い場合があります。そのときは START.md の手順か run_dev.bat を使ってください。

.PHONY: install test run docker migrate stamp-baseline

install:
	python -m pip install -r requirements.txt

test:
	python -m pytest -q

run:
	python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

docker:
	docker compose up --build

migrate:
	alembic upgrade head

stamp-baseline:
	alembic stamp 0001_baseline
