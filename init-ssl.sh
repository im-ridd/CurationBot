#!/bin/bash
set -e

DOMAIN="auto.cur8.fun"
EMAIL="${1:?Usage: ./init-ssl.sh your@email.com}"

echo "==> Starting nginx for ACME challenge..."
docker compose up -d nginx

echo "==> Requesting certificate for $DOMAIN..."
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN"

echo "==> Switching to SSL nginx config..."
cp nginx/default.ssl.conf nginx/default.conf

echo "==> Restarting nginx with SSL..."
docker compose restart nginx

echo "==> Done! https://$DOMAIN should be live."
