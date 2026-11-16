#!/bin/sh
# Start squid in the foreground. Its access log goes to the shared logs folder
# (logs/egress.log in the repo), so it survives restarts of the box.
set -eu

mkdir -p /var/log/squid /run/squid
touch /var/log/squid/cache.log /box-logs/egress.log
chown -R squid:squid /var/log/squid /run/squid
chown squid:squid /box-logs/egress.log

exec squid -N -f /etc/squid/squid.conf
