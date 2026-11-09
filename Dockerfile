FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build tools are only needed if a wheel is unavailable for the platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY avenza ./avenza
COPY avenza_ai ./avenza_ai
COPY data ./data
COPY web ./web
COPY scripts ./scripts
COPY run.py ./

RUN useradd --create-home --uid 10001 avenza \
    && chown -R avenza:avenza /app
USER avenza

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"

# create_app reads configuration from the environment.
CMD ["uvicorn", "avenza.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
