#!/bin/bash
# Installed at /usr/local/sbin/ontokit-firewall.sh and systemd-enabled on the app host.
# This exists because bare /etc/iptables/rules.v* files had no restore mechanism
# when inspected live in U7. A whole-table iptables-restore is deliberately
# avoided because it can restore stale Docker chains.
# OntoKit DEV ingress lockdown (F9 + U7): app ports reachable only from the
# hetzner-dev proxy (v4) and never via v6 (proxy is v4-only). Idempotent.
#
# F9-v6 live evidence: this proxy is IPv4-only, and Docker's published IPv6
# traffic on this box terminates at userland docker-proxy sockets. Those sockets
# are filtered by INPUT; external IPv6 connections were refused with these
# rules installed. If Docker native IPv6 forwarding is ever enabled with
# `"ip6tables": true` in daemon.json, INPUT will no longer cover published
# ports and a DOCKER-USER-equivalent IPv6 forwarding rule set will be required.
set -euo pipefail

# The systemd unit requires /etc/ontokit/firewall.env. Direct invocations must
# export both addresses. Validate both before checking or changing any rules.
validate_ipv4() {
  local name=$1 value=${!1:-} octet
  local -a octets
  if [[ ! $value =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    printf '%s must be a canonical dotted-decimal IPv4 address\n' "$name" >&2
    return 1
  fi
  IFS=. read -r -a octets <<< "$value"
  for octet in "${octets[@]}"; do
    if [[ ${#octet} -gt 1 && $octet == 0* ]] || (( 10#$octet > 255 )); then
      printf '%s must be a canonical dotted-decimal IPv4 address\n' "$name" >&2
      return 1
    fi
  done
}

validate_ipv4 ONTOKIT_PROXY_IPV4
validate_ipv4 ONTOKIT_HOST_IPV4

# A failure in one family must not leave the other family or later ports open.
# Keep attempting every deny rule, then report any insertion failure to systemd.
status=0
for p in 3000 8000 8080 8081; do
  iptables -C DOCKER-USER ! -s "$ONTOKIT_PROXY_IPV4/32" -p tcp --dport "$p" -m conntrack --ctorigdst "$ONTOKIT_HOST_IPV4" -j DROP 2>/dev/null || \
  iptables -I DOCKER-USER 1 ! -s "$ONTOKIT_PROXY_IPV4/32" -p tcp --dport "$p" -m conntrack --ctorigdst "$ONTOKIT_HOST_IPV4" -j DROP || status=1
  ip6tables -C INPUT ! -i lo -p tcp --dport "$p" -j DROP 2>/dev/null || \
  ip6tables -I INPUT 1 ! -i lo -p tcp --dport "$p" -j DROP || status=1
done
exit "$status"
