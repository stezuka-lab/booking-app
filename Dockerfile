# 本番・検証用。開発時は run_dev.bat / uvicorn でも可。
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

RUN mkdir -p data

EXPOSE 8000

CMD ["python", "-m", "app.serve"]
