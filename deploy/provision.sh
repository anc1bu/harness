#!/bin/bash
# Run once on VPS as root to set up the Flask backend.
set -euo pipefail

cd /var/www/harness

# Python venv + deps
apt-get update -qq
apt-get install -y python3-venv python3-pip
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

# Generate secret if missing
if [ ! -f .env ]; then
    echo "HARNESS_SECRET=$(openssl rand -hex 32)" > .env
    chmod 600 .env
fi

# Data dir
mkdir -p data/reference data/transactional
chmod 700 data

# systemd service
cp deploy/harness.service /etc/systemd/system/harness.service
systemctl daemon-reload
systemctl enable harness
systemctl restart harness

# nginx config — always sync and reload
cp deploy/nginx.conf /etc/nginx/sites-available/harness.sapcons.nl
# Remove any OTHER nginx configs that claim server_name harness.sapcons.nl
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
    [ -e "$f" ] || continue
    [ "$(readlink -f "$f")" = "/etc/nginx/sites-available/harness.sapcons.nl" ] && continue
    if grep -q "harness.sapcons.nl" "$f" 2>/dev/null; then
        echo "Disabling conflicting nginx config: $f"
        rm -f "$f"
    fi
done
ln -sf /etc/nginx/sites-available/harness.sapcons.nl /etc/nginx/sites-enabled/harness.sapcons.nl
nginx -t && systemctl reload nginx

echo "✓ Harness provisioned"
systemctl status harness --no-pager | head -5
