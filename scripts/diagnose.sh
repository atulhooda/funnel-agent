#!/usr/bin/env bash
# Is the app down, or is it just my DNS?
#
# Written after an outage that wasn't one: a laptop's router-supplied resolver
# started returning REFUSED for the whole up.railway.app zone while the service
# kept serving traffic normally. From the browser the two look identical — a page
# that won't load — so this checks them separately and says which it is.
#
# Usage:  ./scripts/diagnose.sh [host]
set -u

HOST="${1:-funnel-agent-production-669a.up.railway.app}"
PUBLIC_DNS="1.1.1.1"

bold=$'\033[1m'; red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; off=$'\033[0m'
ok(){ printf "  %s✓%s %s\n" "$green" "$off" "$1"; }
bad(){ printf "  %s✗%s %s\n" "$red" "$off" "$1"; }
warn(){ printf "  %s!%s %s\n" "$yellow" "$off" "$1"; }

printf "%schecking %s%s\n\n" "$bold" "$HOST" "$off"

# 1. Can THIS machine resolve it (what the browser actually does)?
sys_ip=$(dscacheutil -q host -a name "$HOST" 2>/dev/null | awk '/^ip_address/{print $2; exit}')
[ -z "$sys_ip" ] && sys_ip=$(getent hosts "$HOST" 2>/dev/null | awk '{print $1; exit}')

# 2. Can a public resolver? Isolates "my network" from "the name is really gone".
pub_ip=$(dig +short +time=3 "@$PUBLIC_DNS" "$HOST" 2>/dev/null | grep -E '^[0-9]' | head -1)

# 3. Is the server actually serving? Bypass DNS entirely and set the Host header.
code=""
if [ -n "$pub_ip" ]; then
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 \
         --resolve "$HOST:443:$pub_ip" "https://$HOST/health" 2>/dev/null)
fi

[ -n "$sys_ip" ] && ok "your DNS resolves it        ($sys_ip)" || bad "your DNS CANNOT resolve it"
[ -n "$pub_ip" ] && ok "public DNS resolves it      ($pub_ip)" || bad "public DNS cannot resolve it either"
[ "$code" = "200" ] && ok "server responds             (HTTP $code)" \
                    || bad "server did not respond      (HTTP ${code:-no answer})"

echo
if [ "$code" = "200" ] && [ -z "$sys_ip" ]; then
  printf "%sVERDICT: your DNS is the problem — the server is fine.%s\n" "$red" "$off"
  echo "  Your resolvers right now:"
  scutil --dns 2>/dev/null | grep -E "nameserver\[" | sort -u | sed 's/^/    /'
  echo
  echo "  Pin a resolver that works, so this stops depending on the network you join:"
  echo "    sudo networksetup -setdnsservers Wi-Fi 1.1.1.1 1.0.0.1 2606:4700:4700::1111 2606:4700:4700::1001"
  echo "    sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder"
  exit 1
elif [ "$code" = "200" ]; then
  printf "%sVERDICT: everything is up.%s\n" "$green" "$off"
  exit 0
elif [ -z "$pub_ip" ]; then
  printf "%sVERDICT: the hostname does not resolve anywhere — check the domain in Railway.%s\n" "$red" "$off"
  exit 1
else
  printf "%sVERDICT: DNS is fine but the server isn't answering — this one is real.%s\n" "$red" "$off"
  echo "  Check Railway: deploy logs, crash loop, or a failed release."
  exit 1
fi
