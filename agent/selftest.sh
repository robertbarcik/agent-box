#!/bin/bash
# Containment self-test. Runs INSIDE the agent container (bin/box test) and tries
# the things a misbehaving agent would try. Every line should say PASS.

pass=0; fail=0
check() {  # check "description" expected_exit command...
  local desc="$1" want="$2"; shift 2
  "$@" >/dev/null 2>&1; local got=$?
  if { [ "$want" = "ok" ] && [ $got -eq 0 ]; } || { [ "$want" = "blocked" ] && [ $got -ne 0 ]; }; then
    printf '  PASS  %s\n' "$desc"; pass=$((pass+1))
  else
    printf '  FAIL  %s\n' "$desc"; fail=$((fail+1))
  fi
}
http() { curl -s -o /dev/null -m 8 -w '%{http_code}' "$@"; }
is() { [ "$(http "${@:2}")" = "$1" ]; }

echo "Network"
check "no direct route to the internet"            blocked curl -s -m 6 --noproxy '*' https://example.com
check "no direct route by IP address either"       blocked curl -s -m 6 --noproxy '*' https://1.1.1.1
check "outside names do not resolve (no DNS leak)" blocked getent hosts example.com
check "proxy refuses a domain not on the allowlist" blocked curl -s -m 8 https://example.com
check "proxy refuses the host machine"             ok is 403 http://host.docker.internal/
check "proxy refuses a home-network address"       ok is 403 http://192.168.1.1/

echo "Model gateway"
check "gateway is up"                              ok is 200 --noproxy '*' http://gateway:8080/healthz
check "model catalogue is reachable"               ok is 200 --noproxy '*' http://gateway:8080/api/v1/models
check "account endpoints are refused"              ok is 403 --noproxy '*' http://gateway:8080/api/v1/credits
check "key endpoints are refused"                  ok is 403 --noproxy '*' http://gateway:8080/api/v1/keys

echo "Secrets"
check "the real API key is not in the environment" ok test "$OPENROUTER_API_KEY" = "held-by-the-gateway"
check "no host home directory is mounted"          blocked ls /Users /host
check "no Docker socket"                           blocked test -e /var/run/docker.sock
check "no SSH keys or cloud credentials"           blocked ls "$HOME/.ssh" "$HOME/.aws" "$HOME/.config/gh"

echo "Privileges"
check "not running as root"                        ok test "$(id -u)" != "0"
check "no sudo"                                    blocked sudo -n true
check "system files are read-only"                 blocked touch /usr/local/bin/x
check "cannot install system packages"             blocked apt-get install -y cowsay
check "workspace is writable"                      ok sh -c 'touch /workspace/.box-selftest && rm /workspace/.box-selftest'

echo
echo "$pass passed, $fail failed"
[ $fail -eq 0 ]
