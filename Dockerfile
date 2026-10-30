FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --create-home --uid 10001 appuser
USER 10001:10001

FROM base AS test
USER root
COPY requirements-dev.lock ./
RUN pip install --no-cache-dir -r requirements-dev.lock
ENV RUFF_CACHE_DIR=/tmp/ruff
COPY src/ ./src/
COPY pyproject.toml ./
COPY tests/ ./tests/
COPY samples/ ./samples/
USER 10001:10001
CMD ["sh", "-c", "ruff check src tests && ruff format --check src tests && python -m pytest -q -p no:cacheprovider"]

FROM base AS runtime
COPY src/ ./src/
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"
CMD ["gunicorn", "--workers=1", "--bind=0.0.0.0:8000", "--timeout=30", "--worker-tmp-dir=/tmp", "csv_cleaner:create_app()"]
