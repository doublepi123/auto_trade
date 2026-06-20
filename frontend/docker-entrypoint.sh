#!/bin/sh
set -eu

NGINX_CONFIG_PATH="/etc/nginx/conf.d/default.conf"
if [ "$(printf '%s' "${AUTO_TRADE_API_KEY:-}" | wc -l)" -ne 0 ]; then
  printf '%s\n' 'AUTO_TRADE_API_KEY must not contain newline characters' >&2
  exit 1
fi

nginx_escaped=$(printf '%s' "${AUTO_TRADE_API_KEY:-}" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\$/\\$/g')
sed_escaped=$(printf '%s' "$nginx_escaped" | sed 's/[\\&|]/\\&/g')
sed -i "s|__AUTO_TRADE_PROXY_API_KEY__|$sed_escaped|g" "$NGINX_CONFIG_PATH"

exec nginx -g 'daemon off;'
