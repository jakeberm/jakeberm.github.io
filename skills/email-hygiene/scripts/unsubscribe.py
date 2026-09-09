"""Actually unsubscribe from senders you've decided to leave.

Every other module in this skill only rearranges your own mailbox. This one
reaches out to the sender, so it is deliberately narrow about how it does that
and it still defaults to a dry run.

Three mechanisms, in order of preference:

  1. **One-click (RFC 8058)** - the sender advertises `List-Unsubscribe-Post:
     List-Unsubscribe=One-Click` alongside an HTTPS `List-Unsubscribe` URL. The
     spec requires that URL to be safe to POST to without confirmation, and
     forbids it from carrying side effects beyond unsubscribing. This is the
     only case where we contact a web endpoint.
  2. **mailto** - send a plain unsubscribe mail from your own mailbox.
  3. **manual** - anything else (a bare HTTPS link with no one-click support)
     is printed for you to click. We never fetch or follow those, because a
     GET on an arbitrary link can do anything, including confirming an address
     to a spammer.

A sender the deep scan flagged as suspected spam never gets an automatic POST,
even when it advertises one-click. The headers are supplied by the sender, so a
spammer can simply assert RFC 8058 compliance to get a live-address
confirmation out of us. Legitimate senders route opt-outs through ESPs on
unrelated domains, so the opt-out hostname can't be used to tell the two apart -
the sender's own reputation is the usable signal.

Results are recorded so a second run doesn't re-send anything.
"""

from __future__ import annotations

import argparse
import re
import sys

import requests

import config
from graph_client import GraphClient, GraphError

TIMEOUT = 20
UA = "email-hygiene/1.0 (personal mailbox cleanup)"
ONE_CLICK_BODY = {"List-Unsubscribe": "One-Click"}
RESULTS_PATH = config.APP_DIR / "unsubscribes.json"

URL_RE = re.compile(r"<\s*(https?://[^>]+)\s*>", re.I)
MAILTO_RE = re.compile(r"<\s*mailto:([^>]+)\s*>", re.I)


def _header(headers: list[dict], name: str) -> str:
    name = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == name:
            return (h.get("value") or "").strip()
    return ""


def flagged_spam_senders() -> set[str]:
    """Addresses the last deep scan scored as suspected spam."""
    scan = config.load_json(config.DEEP_SCAN_PATH, None)
    if not scan:
        return set()
    return {s["address"].lower() for s in scan.get("suspected_spam", [])}


def discover(client: GraphClient, address: str,
             spam: set[str] | None = None) -> dict:
    """Work out how (or whether) we can unsubscribe from one sender."""
    try:
        message = client.newest_message_from(address)
    except GraphError as exc:
        return {"address": address, "method": "none", "detail": f"lookup failed: {exc}"}
    if not message:
        return {"address": address, "method": "none", "detail": "no message found"}

    try:
        headers = client.get_message_headers(message["id"])
    except GraphError as exc:
        return {"address": address, "method": "none", "detail": f"header read failed: {exc}"}

    raw = _header(headers, "List-Unsubscribe")
    post = _header(headers, "List-Unsubscribe-Post")
    if not raw:
        return {"address": address, "method": "none", "detail": "no List-Unsubscribe header"}

    urls = URL_RE.findall(raw)
    mailtos = MAILTO_RE.findall(raw)
    https = next((u for u in urls if u.lower().startswith("https://")), None)

    # Every route below tells the sender the address is real. That is a fair
    # trade for a list you actually joined, and a bad one for a spammer who is
    # fishing for confirmation, so flagged senders get no automatic contact at
    # all - block them instead.
    if spam and address.lower() in spam:
        detail = "sender flagged as spam - not contacting; block instead"
        if https or urls:
            return {"address": address, "method": "manual", "url": https or urls[0],
                    "detail": detail}
        return {"address": address, "method": "none", "detail": detail}

    # One-click is only valid over HTTPS; the RFC requires TLS.
    if post.lower().replace(" ", "") == "list-unsubscribe=one-click" and https:
        return {"address": address, "method": "one-click", "url": https,
                "detail": "RFC 8058 one-click POST"}
    if mailtos:
        return {"address": address, "method": "mailto", "mailto": mailtos[0],
                "detail": "unsubscribe by email"}
    if urls:
        return {"address": address, "method": "manual", "url": urls[0],
                "detail": "link only - open it yourself"}
    return {"address": address, "method": "none", "detail": "unparseable header"}


def do_one_click(url: str) -> tuple[bool, str]:
    try:
        resp = requests.post(
            url,
            data=ONE_CLICK_BODY,
            timeout=TIMEOUT,
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return False, f"request failed: {exc}"
    ok = 200 <= resp.status_code < 300
    return ok, f"HTTP {resp.status_code}"


def do_mailto(client: GraphClient, mailto: str) -> tuple[bool, str]:
    # A mailto: target may carry its own subject, which senders often rely on.
    target, _, query = mailto.partition("?")
    subject = "unsubscribe"
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key.lower() == "subject" and value:
            subject = requests.utils.unquote(value)
    try:
        client.send_mail(target.strip(), subject, "<p>Please unsubscribe this address.</p>")
    except GraphError as exc:
        return False, f"send failed: {exc}"
    return True, f"sent to {target.strip()}"


def targets(explicit: list[str] | None) -> list[str]:
    if explicit:
        return [a.lower() for a in explicit]
    payload = config.load_json(config.DECISIONS_PATH, {"decisions": []})
    return [
        d["address"].lower()
        for d in payload.get("decisions", [])
        if (d.get("decision") or "") == "unsubscribe"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unsubscribe from senders decided 'unsubscribe' (dry run by default)."
    )
    parser.add_argument("--apply", action="store_true", help="actually unsubscribe")
    parser.add_argument("--senders", nargs="+", help="limit to these addresses")
    parser.add_argument("--senders-file", help="file with one address per line")
    parser.add_argument("--retry", action="store_true",
                        help="re-attempt senders already recorded as done")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    explicit = list(args.senders or [])
    if args.senders_file:
        with open(args.senders_file, encoding="utf-8") as fh:
            explicit += [l.strip() for l in fh if l.strip() and not l.startswith("#")]

    try:
        profile = config.load_profile()
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2

    addresses = targets(explicit or None)
    if not addresses:
        print("nothing decided 'unsubscribe'")
        return 0

    done = config.load_json(RESULTS_PATH, {"senders": {}})
    if not args.retry:
        skipped = [a for a in addresses if done["senders"].get(a, {}).get("ok")]
        addresses = [a for a in addresses if a not in skipped]
        if skipped:
            print(f"skipping {len(skipped)} already unsubscribed (use --retry to force)\n")
    if not addresses:
        print("all target senders already unsubscribed")
        return 0

    client = GraphClient(profile)
    print(f"discovering opt-out method for {len(addresses)} sender(s)...\n")

    spam = flagged_spam_senders()
    plans = [discover(client, a, spam) for a in addresses]
    buckets: dict[str, list] = {"one-click": [], "mailto": [], "manual": [], "none": []}
    for p in plans:
        buckets[p["method"]].append(p)

    for method in ("one-click", "mailto", "manual", "none"):
        items = buckets[method]
        if not items:
            continue
        print(f"== {method}  ({len(items)})")
        for p in items:
            extra = p.get("url") or p.get("mailto") or p["detail"]
            print(f"   {p['address']}\n     {extra}")
        print()

    automatic = buckets["one-click"] + buckets["mailto"]
    if not args.apply:
        print(f"dry run - nothing sent. {len(automatic)} can be done automatically; "
              f"{len(buckets['manual'])} need a manual click.")
        print("re-run with --apply to execute.")
        return 0

    if not automatic:
        print("nothing can be unsubscribed automatically.")
        return 0

    if not args.non_interactive:
        answer = input(f"unsubscribe {len(automatic)} sender(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted")
            return 1

    ok_count = fail_count = 0
    for p in automatic:
        if p["method"] == "one-click":
            ok, detail = do_one_click(p["url"])
        else:
            ok, detail = do_mailto(client, p["mailto"])
        done["senders"][p["address"]] = {
            "ok": ok,
            "method": p["method"],
            "detail": detail,
            "at": config.iso(config.utcnow()),
        }
        config.audit("unsubscribe", address=p["address"], method=p["method"],
                     ok=ok, detail=detail)
        status = "ok " if ok else "FAIL"
        print(f"  {status}  {p['address']}  ({detail})")
        ok_count += ok
        fail_count += not ok

    config.save_json(RESULTS_PATH, done)
    print(f"\nunsubscribed {ok_count}, failed {fail_count}")

    if buckets["manual"]:
        print(f"\n{len(buckets['manual'])} still need a manual click:")
        for p in buckets["manual"]:
            print(f"  {p['address']}\n    {p['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
