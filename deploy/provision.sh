#!/bin/bash
set -e

APPDIR=/var/www/harness

# Install/update Python dependencies
"$APPDIR/venv/bin/pip" install -q -r "$APPDIR/requirements.txt"

# Restart service to pick up new code
systemctl restart harness

echo "Deploy complete."
