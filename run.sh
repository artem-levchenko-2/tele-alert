#!/usr/bin/env bash
set -euo pipefail

echo "[tele-alert] starting forwarder..."
exec python3 -u /opt/forwarder.py
