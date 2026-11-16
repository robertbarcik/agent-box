#!/bin/sh
# Start squid in the foreground and mirror its access log to the container's stdout,
# so `docker compose logs egress` shows every allowed and denied request.
set -eu

mkdir -p /var/log/squid /run/squid
touch /var/log/squid/access.log /var/log/squid/cache.log
chown -R squid:squid /var/log/squid /run/squid

tail -n 0 -F /var/log/squid/access.log &

exec squid -N -f /etc/squid/squid.conf
