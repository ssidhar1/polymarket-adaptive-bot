FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    DEFAULT_MODE=paper \
    ASTF_DATA_DIR=/app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN if [ ! -f config.yaml ]; then cp config.example.yaml config.yaml; fi \
    && mkdir -p /app/data

# Paper mode only — live stays locked in code
CMD ["python", "-m", "src.main", "--mode", "paper", "--config", "config.yaml"]

# Process is a long-running worker; fail if main.py vanished (bad image)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import src.main" || exit 1
