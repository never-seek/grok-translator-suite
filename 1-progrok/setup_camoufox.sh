#!/bin/bash
set -e
echo 'Extracting camoufox...'
mkdir -p /root/.cache/camoufox
unzip -q -o /tmp/camoufox.zip -d /root/.cache/camoufox
echo '{\ version\: \152.0.4\, \release\: \beta.30\}' > /root/.cache/camoufox/version.json
chmod +x /root/.cache/camoufox/camoufox-bin
echo 'Testing binary version...'
/root/.cache/camoufox/camoufox-bin --version || true
echo 'Testing camoufox python module...'
/workspace/progrok/.venv/bin/python3 -c 'import camoufox; print(\Camoufox OK\)'
