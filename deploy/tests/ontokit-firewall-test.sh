#!/usr/bin/env bash
set -euo pipefail

readonly TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT="$TEST_DIR/../firewall/ontokit-firewall.sh"
TMP_ROOT=$(mktemp -d)
readonly TMP_ROOT
trap 'rm -rf -- "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/bin"
export CALL_LOG="$TMP_ROOT/calls" RULE_STATE="$TMP_ROOT/rules"
export PATH="$TMP_ROOT/bin:$PATH"

# Both firewall commands are mocked; tests never touch the machine's firewall.
cat >"$TMP_ROOT/bin/iptables" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
tool=${0##*/}
printf '%s %s\n' "$tool" "$*" >>"$CALL_LOG"
operation=$1
chain=$2
shift 2
if [[ $operation == -I ]]; then shift; fi
rule="$tool $chain $*"
case "$operation" in
    -C) grep -Fxq -- "$rule" "$RULE_STATE" ;;
    -I)
        [[ ${MOCK_INSERT_FAIL:-0} != 1 && ${MOCK_INSERT_FAIL_TOOL:-} != "$tool" ]] || exit 2
        printf '%s\n' "$rule" >>"$RULE_STATE"
        ;;
    *) exit 99 ;;
esac
EOF
chmod +x "$TMP_ROOT/bin/iptables"
cp "$TMP_ROOT/bin/iptables" "$TMP_ROOT/bin/ip6tables"
: >"$RULE_STATE"

fail() { printf 'not ok - %s\n' "$1" >&2; exit 1; }

reject_config() {
    : >"$CALL_LOG"
    if env -u ONTOKIT_PROXY_IPV4 -u ONTOKIT_HOST_IPV4 "$@" bash "$SCRIPT" >"$TMP_ROOT/output" 2>&1; then
        fail "invalid or missing configuration was accepted: $*"
    fi
    [[ ! -s $CALL_LOG ]] || fail 'configuration failure invoked firewall commands'
}

reject_config
reject_config ONTOKIT_PROXY_IPV4=192.0.2.10
reject_config ONTOKIT_HOST_IPV4=198.51.100.20
for bad in '' 256.1.1.1 1.2.3 1.2.3.4.5 1.2.3.-1 01.2.3.4 1.2.3.4/32 ::1 '1.2.3.4 -j ACCEPT' $'1.2.3.4\n'; do
    reject_config "ONTOKIT_PROXY_IPV4=$bad" ONTOKIT_HOST_IPV4=198.51.100.20
    reject_config ONTOKIT_PROXY_IPV4=192.0.2.10 "ONTOKIT_HOST_IPV4=$bad"
done
printf 'ok - invalid and missing configuration fails before firewall access\n'

export ONTOKIT_PROXY_IPV4=192.0.2.10 ONTOKIT_HOST_IPV4=198.51.100.20
: >"$CALL_LOG"
bash "$SCRIPT"
: >"$TMP_ROOT/expected"
for port in 3000 8000 8080 8081; do
    printf '%s\n' \
        "iptables -C DOCKER-USER ! -s 192.0.2.10/32 -p tcp --dport $port -m conntrack --ctorigdst 198.51.100.20 -j DROP" \
        "iptables -I DOCKER-USER 1 ! -s 192.0.2.10/32 -p tcp --dport $port -m conntrack --ctorigdst 198.51.100.20 -j DROP" \
        "ip6tables -C INPUT ! -i lo -p tcp --dport $port -j DROP" \
        "ip6tables -I INPUT 1 ! -i lo -p tcp --dport $port -j DROP" >>"$TMP_ROOT/expected"
done
diff -u "$TMP_ROOT/expected" "$CALL_LOG" || fail 'unexpected firewall rules'
printf 'ok - exact IPv4 conntrack and IPv6 INPUT rules installed\n'

cp "$RULE_STATE" "$TMP_ROOT/initial-rules"
: >"$CALL_LOG"
bash "$SCRIPT"
grep ' -C ' "$TMP_ROOT/expected" >"$TMP_ROOT/checks"
diff -u "$TMP_ROOT/checks" "$CALL_LOG" || fail 'existing rules were reinserted'
cmp "$TMP_ROOT/initial-rules" "$RULE_STATE" || fail 'existing rules changed'
printf 'ok - repeated execution checks existing rules without duplicate insertion\n'

for failed_tool in iptables ip6tables; do
    : >"$RULE_STATE"
    : >"$CALL_LOG"
    if MOCK_INSERT_FAIL_TOOL="$failed_tool" bash "$SCRIPT" >"$TMP_ROOT/output" 2>&1; then
        fail "$failed_tool insertion failure returned success"
    fi
    diff -u "$TMP_ROOT/expected" "$CALL_LOG" || fail "$failed_tool failure skipped remaining deny attempts"
    other_tool=iptables
    if [[ $failed_tool == iptables ]]; then other_tool=ip6tables; fi
    grep "^$other_tool " "$TMP_ROOT/initial-rules" >"$TMP_ROOT/surviving-rules"
    diff -u "$TMP_ROOT/surviving-rules" "$RULE_STATE" || fail "$failed_tool failure prevented other family protection"
    printf 'ok - %s insertion failures preserve other family and attempt every port\n' "$failed_tool"
done

: >"$RULE_STATE"
if MOCK_INSERT_FAIL=1 bash "$SCRIPT" >"$TMP_ROOT/output" 2>&1; then
    fail 'insertion failure returned success'
fi
printf 'ok - firewall insertion failures propagate\n'

grep -Fxq 'EnvironmentFile=/etc/ontokit/firewall.env' "$TEST_DIR/../firewall/ontokit-firewall.service" || fail 'systemd configuration file is not required'
printf 'ok - systemd requires the host configuration file\n'
