"""Record decisions about senders, and route senders to folders.

    python scripts/decide.py list
    python scripts/decide.py set someone@example.com unsubscribe
    python scripts/decide.py set news@example.com block
    python scripts/decide.py route notifications@github.com "Inbox/GitHub"
    python scripts/decide.py protect no-reply@accounts.google.com

Nothing here touches the mailbox — decisions take effect on the next
`cleanup.py --apply` / `rules_sync.py --apply`.
"""

from __future__ import annotations

import argparse
import sys

import config

CHOICES = ("pending", "unsubscribe", "block", "keep")


def _decisions() -> dict:
    return config.load_json(config.DECISIONS_PATH, {"decisions": []})


def _save(payload: dict) -> None:
    payload["updated_at"] = config.iso(config.utcnow())
    config.save_json(config.DECISIONS_PATH, payload)


def cmd_list(_args) -> int:
    payload = _decisions()
    rows = sorted(payload.get("decisions", []), key=lambda d: -(d.get("score") or 0))
    if not rows:
        print("No decisions yet — run analyze.py first.")
        return 0
    print(f"{'decision':<12} {'score':>6} {'msgs':>5}  address")
    for row in rows:
        print(
            f"{row.get('decision', 'pending'):<12} {row.get('score', 0):>6} "
            f"{row.get('count', 0):>5}  {row['address']}"
        )
    return 0


def cmd_set(args) -> int:
    payload = _decisions()
    address = args.address.lower()
    for row in payload.setdefault("decisions", []):
        if row["address"].lower() == address:
            row["decision"] = args.decision
            row["decided_at"] = config.iso(config.utcnow())
            break
    else:
        payload["decisions"].append(
            {
                "address": address,
                "decision": args.decision,
                "decided_at": config.iso(config.utcnow()),
            }
        )
    _save(payload)
    config.audit("decision", address=address, decision=args.decision)
    print(f"{address} -> {args.decision}")
    if args.decision == "unsubscribe":
        row = next((r for r in payload["decisions"] if r["address"].lower() == address), {})
        for key, label in (("unsubscribe_url", "link"), ("unsubscribe_mailto", "mailto")):
            if row.get(key):
                print(f"  opt-out {label}: {row[key]}")
        print("  (open it yourself - this skill never follows unsubscribe links)")
    return 0


def cmd_route(args) -> int:
    profile = config.load_profile()
    routes = profile["cleanup"].setdefault("routes", [])
    address = args.address.lower()
    is_domain = not args.address.startswith("@") and "@" not in args.address

    for route in routes:
        if route.get("folder") == args.folder:
            bucket = route.setdefault("domains" if is_domain else "senders", [])
            if address.lstrip("@") not in bucket:
                bucket.append(address.lstrip("@"))
            route.setdefault("mark_read", args.mark_read)
            break
    else:
        routes.append(
            {
                "folder": args.folder,
                "senders": [] if is_domain else [address],
                "domains": [address.lstrip("@")] if is_domain else [],
                "subject_patterns": [],
                "mark_read": args.mark_read,
            }
        )

    config.save_profile(profile)
    # A routed sender shouldn't keep showing up as an unsubscribe suggestion.
    payload = _decisions()
    for row in payload.get("decisions", []):
        if row["address"].lower() == address:
            row["decision"] = "keep"
            row["decided_at"] = config.iso(config.utcnow())
    _save(payload)
    config.audit("route", address=address, folder=args.folder)
    print(f"{address} -> folder '{args.folder}' (still subscribed)")
    return 0


def cmd_protect(args) -> int:
    profile = config.load_profile()
    address = args.address.lower()
    key = "protected_domains" if "@" not in address else "protected_senders"
    bucket = profile["cleanup"].setdefault(key, [])
    if address.lstrip("@") not in bucket:
        bucket.append(address.lstrip("@"))
    config.save_profile(profile)

    payload = _decisions()
    for row in payload.get("decisions", []):
        if row["address"].lower() == address:
            row["decision"] = "keep"
            row["decided_at"] = config.iso(config.utcnow())
    _save(payload)
    config.audit("protect", address=address)
    print(f"{address} -> protected (never filed, never suggested)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage sender decisions.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show all recorded decisions").set_defaults(func=cmd_list)

    p_set = sub.add_parser("set", help="record a decision for a sender")
    p_set.add_argument("address")
    p_set.add_argument("decision", choices=CHOICES)
    p_set.set_defaults(func=cmd_set)

    p_route = sub.add_parser("route", help="file a sender into a folder, stay subscribed")
    p_route.add_argument("address", help="email address or bare domain")
    p_route.add_argument("folder", help="folder name or path, e.g. 'Inbox/GitHub'")
    p_route.add_argument("--mark-read", action="store_true")
    p_route.set_defaults(func=cmd_route)

    p_protect = sub.add_parser("protect", help="exempt a sender from all actions")
    p_protect.add_argument("address")
    p_protect.set_defaults(func=cmd_protect)

    args = parser.parse_args()
    try:
        return args.func(args)
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
