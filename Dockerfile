FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=UTC
WORKDIR /srv

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY tests ./tests
COPY scripts ./scripts
RUN pip install --upgrade pip && pip install ".[dev]" && useradd -u 10050 -m pe && chown -R pe /srv
USER pe

CMD ["python", "-m", "app.jobs.worker"]
