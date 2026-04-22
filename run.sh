#!/bin/bash
# Eastern Aero Parts — Auto Quote System
# Run this script to start the web app.

cd "$(dirname "$0")"

echo ""
echo "  ✈  Eastern Aero Parts — Auto Quote System"
echo "  ─────────────────────────────────────────"

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
  echo "  Installing dependencies..."
  pip3 install -r requirements.txt --quiet
fi

echo "  Starting server at http://localhost:5000"
echo "  Press Ctrl+C to stop."
echo ""

python3 app.py
