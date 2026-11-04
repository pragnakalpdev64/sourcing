# One image for both the API service and the worker job.
# The worker uses Browserbase (Playwright over CDP) or, with BROWSER_BACKEND=local,
# the headless Chromium installed below; both are in the image so the two can be
# compared on Cloud Run without a rebuild.
# Bookworm: a Debian release Playwright's --with-deps supports.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
# Headless shell only (no full Chrome UI) keeps the image smaller; readable by
# the non-root user created below.
RUN pip install -r requirements.txt \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY sourcing ./sourcing
COPY service ./service
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 app
USER app

ENV PORT=8080
# Service default. The job overrides this with: python -m service.job_main
CMD exec uvicorn service.api:app --host 0.0.0.0 --port ${PORT}
