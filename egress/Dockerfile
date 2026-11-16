FROM alpine:3.20
RUN apk add --no-cache squid
COPY squid.conf /etc/squid/squid.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
# The repo's egress/ folder is mounted at /box-egress at run time, so allowlist.txt
# can change without a rebuild.
ENTRYPOINT ["/entrypoint.sh"]
