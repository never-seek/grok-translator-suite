#!/bin/bash
export HTTP_PROXY="http://127.0.0.1:7897"
export HTTPS_PROXY="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"
export https_proxy="http://127.0.0.1:7897"
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"
export PROGROK_SKIP_PROXY_PREFLIGHT="1"

cd /workspace/progrok/backend
exec /workspace/progrok/.venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 3080 --workers 1
