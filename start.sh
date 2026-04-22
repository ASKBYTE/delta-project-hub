#!/bin/bash
echo "Starting Delta Project Hub..."
cd "$(dirname "$0")"
pip install flask flask-cors werkzeug --break-system-packages -q 2>/dev/null
python3 app.py
