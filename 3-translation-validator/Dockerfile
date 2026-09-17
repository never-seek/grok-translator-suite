FROM python:3.12-slim

WORKDIR /app

COPY grok_audit_proxy.py /app/grok_audit_proxy.py

ENV PYTHONUNBUFFERED=1
ENV LISTEN_HOST=0.0.0.0
ENV PORT=3002
ENV UPSTREAM_HOST=127.0.0.1
ENV UPSTREAM_PORT=3001
ENV LOG_DIR=/app/logs
ENV CONFIG_FILE=/app/config/translation_profiles.json

EXPOSE 3002

CMD ["python3", "/app/grok_audit_proxy.py"]
