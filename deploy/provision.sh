#!/bin/bash
set -e

APPDIR=/var/www/harness

# Install/update Python dependencies
"$APPDIR/venv/bin/pip" install -q -r "$APPDIR/requirements.txt"

# Ensure DB exists and schema is up to date
"$APPDIR/venv/bin/python3" -c "from server import init_db; init_db()"

# Restart service to pick up new code
systemctl restart harness

echo "Deploy complete."
