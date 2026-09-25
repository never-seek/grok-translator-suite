FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY backend/ /app/backend/
COPY web/ /app/web/
COPY vendor/ /app/vendor/
COPY config.example.json /app/config/config.json

WORKDIR /app/backend

EXPOSE 3080

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3080"]
