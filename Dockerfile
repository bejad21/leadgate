FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime packages only, pinned. Copied before the code so a code change reuses this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY engine/ engine/

# The engine needs no write access to its own files, so it runs as an unprivileged user.
RUN useradd --system --uid 10001 --user-group --no-create-home app
USER app

EXPOSE 8000

# python:slim has no curl, so the check uses the standard library.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
