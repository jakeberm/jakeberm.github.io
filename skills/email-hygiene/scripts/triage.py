"""Propose a decision for every undecided sender, so bulk review is possible.

Walking a hundred senders one at a time is not realistic. This classifies each
undecided sender from the deep scan into a proposed decision with a stated
reason, groups them, and lets you accept a whole group at once.

Nothing here decides anything on its own -- it writes proposals, and only
`--accept <group>` records them. Deletion is never proposed; the strongest
proposal is `block`, which purge.py acts on later only for aged mail.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict

import config
import decide

# Ordered: the first matching rule wins, so security beats receipts, which beats
# marketing. Each entry is (group, decision, folder, matcher).
SECURITY_RE = re.compile(
    r"\b(sign[- ]?in|log[- ]?in|verification|verify|security|password|passcode|"
    r"one[- ]time|2fa|authenticat|suspicious|unusual activity|recovery)\b",
    re.I,
)
RECEIPT_RE = re.compile(
    r"\b(order|receipt|invoice|shipped|shipping|delivered|tracking|confirmation|"
    r"confirmed|statement|payment|refund|booking|reservation|itinerary|ticket)\b",
    re.I,
)
PROMO_RE = re.compile(
    r"(\d+% off|\boff\b|\bsale\b|\bdeal|\bsave\b|last chance|ends (today|tomorrow|soon)|"
    r"limited time|exclusive|new arrivals|shop now|don't miss|black friday)",
    re.I,
)
KIDS_RE = re.compile(r"(school|camp|pta|kids|girlsrock|scout|parish|youth|classroom)", re.I)
FINANCE_RE = re.compile(
    r"(bank|chase|equifax|fidelity|navyfederal|creditcard|americanexpress|amex|"
    r"capitalone|mortgage|loan|401k|tax|irs|ssa\.gov)",
    re.I,
)
PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "live.com", "outlook.com", "yahoo.com",
    "comcast.net", "me.com", "icloud.com", "aol.com",
}


def _text(sender: dict) -> str:
    return " ".join(sender.get("sample_subjects") or [])


def classify(sender: dict) -> tuple[str, str, str | None, str]:
    """Return (group, decision, folder, reason) for one sender."""
    address = sender["address"]
    local = address.partition("@")[0].lower()
    domain = sender["domain"].lower()
    subjects = _text(sender)
    spam = sender.get("spam_score", 0)

    if domain in PERSONAL_DOMAINS and not sender.get("has_unsubscribe"):
        return ("personal", "keep", None,
                "individual sender, not a mailing list")

    if spam >= 4:
        return ("spam", "block", None,
                f"spam score {spam}: {', '.join(sender.get('spam_signals', []))}")

    if SECURITY_RE.search(subjects) or "security" in local or "accounts" in local:
        return ("security", "protect", None,
                "sign-in codes / security alerts - must stay visible")

    if FINANCE_RE.search(domain) or FINANCE_RE.search(subjects):
        return ("finance", "route", "Inbox/Finance",
                "financial statements and account notices")

    if KIDS_RE.search(domain) or KIDS_RE.search(subjects):
        return ("kids", "route", "Inbox/Kids", "school / kids activity mail")

    if RECEIPT_RE.search(subjects) and not PROMO_RE.search(subjects):
        return ("receipts", "route", "Inbox/Receipts",
                "order and shipping confirmations - transactional")

    if PROMO_RE.search(subjects) or sender.get("has_unsubscribe"):
        if sender.get("has_unsubscribe"):
            return ("marketing", "unsubscribe", None,
                    "promotional, and a working opt-out link exists")
        return ("marketing_nooptout", "block", None,
                "promotional with no opt-out header - filter instead")

    return ("unclear", "pending", None, "not enough signal - review manually")


def build(limit_group: str | None = None) -> dict[str, list]:
    scan = config.load_json(config.DEEP_SCAN_PATH, None)
    if not scan:
        raise SystemExit(f"No deep scan yet. Run: python scripts/deep_scan.py --days 365")

    # The scan is a snapshot, so anything decided since then must be filtered
    # out here or it comes back as undecided on every run.
    decided = {
        d["address"].lower()
        for d in config.load_json(config.DECISIONS_PATH, {}).get("decisions", [])
        if (d.get("decision") or "pending") != "pending"
    }
    profile = config.load_profile()
    cleanup = profile.get("cleanup", {})
    for key in ("protected_senders", "protected_domains"):
        decided |= {s.lower() for s in cleanup.get(key, [])}
    for route in cleanup.get("routes", []):
        decided |= {s.lower() for s in route.get("senders", [])}

    seen, pool = set(), []
    for bucket in ("undecided_subscriptions", "suspected_spam"):
        for sender in scan.get(bucket, []):
            address = sender["address"].lower()
            if address in seen or address in decided:
                continue
            seen.add(address)
            pool.append(sender)

    groups: dict[str, list] = defaultdict(list)
    for sender in pool:
        group, decision, folder, reason = classify(sender)
        if limit_group and group != limit_group:
            continue
        groups[group].append(
            {**sender, "proposed": decision, "folder": folder, "reason": reason}
        )
    for items in groups.values():
        items.sort(key=lambda s: -s["count"])
    return groups


GROUP_ORDER = [
    "spam", "marketing_nooptout", "marketing", "receipts",
    "finance", "kids", "security", "personal", "unclear",
]


def show(groups: dict[str, list], verbose: bool) -> None:
    total = sum(len(v) for v in groups.values())
    print(f"{total} undecided sender(s) in {len(groups)} group(s)\n")
    for group in GROUP_ORDER:
        items = groups.get(group)
        if not items:
            continue
        proposed = items[0]["proposed"]
        folder = items[0]["folder"]
        target = f" -> {folder}" if folder else ""
        msgs = sum(i["count"] for i in items)
        print(f"== {group}  ({len(items)} senders, {msgs} msgs)  "
              f"proposed: {proposed}{target}")
        print(f"   {items[0]['reason']}")
        shown = items if verbose else items[:8]
        for s in shown:
            print(f"     {s['count']:>3} msgs  read {s['read_rate']:.2f}  {s['address']}")
        if len(items) > len(shown):
            print(f"     ... and {len(items) - len(shown)} more")
        print()
    print("accept a group:  python scripts/triage.py --accept <group>")


def accept(groups: dict[str, list], group: str) -> int:
    items = groups.get(group)
    if not items:
        print(f"no senders in group '{group}'", file=sys.stderr)
        return 1

    # Load once, mutate everything, save once -- 121 individual saves would be
    # both slow and a chance to end up half-applied.
    profile = config.load_profile()
    payload = config.load_json(config.DECISIONS_PATH, {"decisions": []})

    applied, links = 0, []
    for s in items:
        proposed = s["proposed"]
        if proposed == "pending":
            continue
        if proposed == "route":
            decide.apply_route(profile, payload, s["address"], s["folder"])
        elif proposed == "protect":
            decide.apply_protect(profile, payload, s["address"])
        else:
            decide.apply_decision(payload, s["address"], proposed)
        if proposed == "unsubscribe":
            url = s.get("unsubscribe_url") or s.get("unsubscribe_mailto")
            if url:
                links.append((s["address"], url))
        applied += 1

    config.save_profile(profile)
    payload["updated_at"] = config.iso(config.utcnow())
    config.save_json(config.DECISIONS_PATH, payload)

    print(f"recorded {applied} decision(s) for group '{group}'")
    if links:
        print(f"\n{len(links)} opt-out link(s) -- open these yourself, "
              f"this skill never follows them:")
        for address, url in links:
            print(f"  {address}\n    {url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-triage undecided senders.")
    parser.add_argument("--group", help="only show one group")
    parser.add_argument("--accept", help="record the proposed decision for a whole group")
    parser.add_argument("--verbose", action="store_true", help="list every sender")
    args = parser.parse_args()

    groups = build(args.group)
    if args.accept:
        return accept(build(None), args.accept)
    show(groups, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
