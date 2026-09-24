FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=UTC
WORKDIR /srv

# Dependencies first (cached layer - code changes do not re-download ~400 MB)
COPY pyproject.toml ./
RUN pip install --upgrade pip && python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['dev']))" > /tmp/req.txt \
    && pip install -r /tmp/req.txt && useradd -u 10050 -m pe

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY tests ./tests
COPY scripts ./scripts
RUN pip install --no-deps . && chown -R pe /srv
USER pe

CMD ["python", "-m", "app.jobs.worker"]
