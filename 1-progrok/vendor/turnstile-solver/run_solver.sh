#!/bin/bash
cd /workspace/progrok/turnstile-solver
exec /workspace/progrok/.venv/bin/python3 api_solver.py --browser_type camoufox --thread 1 --host 0.0.0.0 --port 5072 --proxy
