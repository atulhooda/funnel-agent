#!/usr/bin/env python3
"""Check the WhatsApp Cloud API credentials before trusting them in production.

Four values have to line up — access token, phone number id, app secret, verify
token — and Graph reports almost every mistake as the same unhelpful "object
does not exist, cannot be loaded due to missing permissions". This separates
them, because a valid token whose System User has no WhatsApp asset assigned
looks identical to a wrong phone number id until you check both.

    ./scripts/check_meta.py
    META_WA_ACCESS_TOKEN=... META_WA_PHONE_NUMBER_ID=... ./scripts/check_meta.py

Reads .env by PARSING it, never by sourcing it: a shell `source` would execute
anything in there, and a scratch curl left in an env file is a live command.

Read-only — it never sends a message.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

GREEN, RED, YELLOW, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
ok = lambda m: print(f"  {GREEN}ok{OFF}   {m}")
bad = lambda m: print(f"  {RED}FAIL{OFF} {m}")
warn = lambda m: print(f"  {YELLOW}warn{OFF} {m}")


def load_env() -> None:
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().isidentifier():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def graph(path: str, token: str, **params) -> dict:
    api = os.environ.get("META_WA_API_VERSION", "v23.0")
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://graph.facebook.com/{api}/{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except Exception:
            return {"error": {"message": f"HTTP {exc.code}"}}
    except Exception as exc:
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def main() -> int:
    load_env()
    token = os.environ.get("META_WA_ACCESS_TOKEN", "")
    phone_id = os.environ.get("META_WA_PHONE_NUMBER_ID", "")
    if not token:
        bad("META_WA_ACCESS_TOKEN is not set")
        return 1
    failed = False

    print("== token ==")
    data = graph("debug_token", token, input_token=token).get("data", {})
    if not data.get("is_valid"):
        bad(f"token rejected: {(data.get('error') or {}).get('message', 'unknown')}")
        return 1
    ok(f"valid {data.get('type')} token, app {data.get('app_id')}")
    expires = data.get("expires_at")
    if expires:
        warn(f"EXPIRES at {expires} — use a System User token, not a temporary one")
    else:
        ok("never expires")
    scopes = {g["scope"] for g in data.get("granular_scopes", [])}
    for needed in ("whatsapp_business_messaging", "whatsapp_business_management"):
        if needed in scopes:
            ok(f"scope {needed}")
        else:
            bad(f"missing scope {needed}")
            failed = True

    # Scopes say what KINDS of thing the token may touch; asset assignment says
    # WHICH accounts. Both are required, and only the second is easy to forget.
    print("== assets the System User can reach ==")
    businesses = graph("me/businesses", token).get("data", [])
    if businesses:
        for b in businesses:
            ok(f"business {b['id']} {b.get('name', '')}")
    else:
        bad("no business assets assigned to this System User")
        print("       Business Settings -> Users -> System users -> Add assets ->")
        print("       WhatsApp accounts -> pick the WABA -> Full control, then")
        print("       GENERATE A NEW TOKEN (existing ones don't pick up new grants).")
        failed = True

    print("== phone number ==")
    waba = ""
    if not phone_id:
        warn("META_WA_PHONE_NUMBER_ID is not set")
    else:
        info = graph(phone_id, token,
                     fields="display_phone_number,verified_name,quality_rating,"
                            "whatsapp_business_account{id,name}")
        if "error" in info:
            bad(info["error"]["message"][:130])
            print("       Either the id is wrong (WhatsApp Manager -> API Setup shows")
            print("       it under the number) or the WABA is not assigned above.")
            failed = True
        else:
            ok(f"{info.get('display_phone_number')} \"{info.get('verified_name')}\" "
               f"quality={info.get('quality_rating')}")
            account = info.get("whatsapp_business_account") or {}
            waba = account.get("id", "")
            name = account.get("name", "")
            ok(f"belongs to WABA {waba} \"{name}\"")
            # Meta auto-creates a sandbox WABA whose number only reaches a
            # handful of allow-listed testers. Sending real leads from it looks
            # like success — Graph accepts the call — and delivers to nobody.
            if "test" in name.lower():
                bad("that is the TEST account: it only delivers to allow-listed "
                    "numbers. Use the production WABA's phone number id.")

    print("== templates ==")
    if not waba:
        warn("cannot list templates until the phone number resolves")
    else:
        result = graph(f"{waba}/message_templates", token,
                       fields="name,status,category,language", limit=50)
        if "error" in result:
            bad(result["error"]["message"][:130])
        else:
            for tpl in result.get("data", []):
                line = f"{tpl['name']:28} {tpl['category']:14} {tpl['status']}"
                ok(line) if tpl["status"] == "APPROVED" else warn(line)
            print("\n       Names and categories must match config/templates.yaml.")

    print("== app secret ==")
    if os.environ.get("META_WA_APP_SECRET"):
        ok("set — webhook signatures will verify")
    else:
        warn("unset: inbound webhooks are rejected, so STOP replies never arrive")

    print()
    print(f"{RED}Not ready.{OFF}" if failed else f"{GREEN}All checks passed.{OFF}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
