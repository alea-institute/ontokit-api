#!/bin/bash
# Installed at /usr/local/sbin/ontokit-firewall.sh and systemd-enabled on CPX41.
# This exists because bare /etc/iptables/rules.v* files had no restore mechanism
# when inspected live in U7. A whole-table iptables-restore is deliberately
# avoided because it can restore stale Docker chains.
# OntoKit DEV ingress lockdown (F9 + U7): app ports reachable only from the
# hetzner-dev proxy (v4) and never via v6 (proxy is v4-only). Idempotent.
PROXY=204.168.246.227; SELF=178.156.208.239
for p in 3000 8000 8080 8081; do
  iptables -C DOCKER-USER ! -s $PROXY/32 -p tcp --dport $p -m conntrack --ctorigdst $SELF -j DROP 2>/dev/null || \
  iptables -I DOCKER-USER 1 ! -s $PROXY/32 -p tcp --dport $p -m conntrack --ctorigdst $SELF -j DROP
  ip6tables -C INPUT ! -i lo -p tcp --dport $p -j DROP 2>/dev/null || \
  ip6tables -I INPUT 1 ! -i lo -p tcp --dport $p -j DROP
done
