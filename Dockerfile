FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    DEFAULT_MODE=paper

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Optional: bake a default config if user did not mount one
RUN if [ ! -f config.yaml ]; then cp config.example.yaml config.yaml; fi

# Paper mode only — live stays locked in code
CMD ["python", "-m", "src.main", "--mode", "paper", "--config", "config.yaml"]
