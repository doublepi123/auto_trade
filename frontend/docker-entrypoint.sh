#!/bin/sh
set -eu

CONFIG_PATH="/usr/share/nginx/html/runtime-config.js"
if [ -n "${AUTO_TRADE_API_KEY:-}" ]; then
  escaped=$(printf '%s' "$AUTO_TRADE_API_KEY" | sed "s/\\\\/\\\\\\\\/g; s/'/\\\\'/g")
  printf "window.__AUTO_TRADE_API_KEY__ = '%s';\n" "$escaped" > "$CONFIG_PATH"
else
  printf "window.__AUTO_TRADE_API_KEY__ = '';\n" > "$CONFIG_PATH"
fi

exec nginx -g 'daemon off;'
