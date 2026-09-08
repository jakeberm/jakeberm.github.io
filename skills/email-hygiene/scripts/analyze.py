"""Scan the mailbox, aggregate per-sender behaviour, and score unsubscribe candidates.

Writes ~/.email-hygiene/analysis.json and seeds ~/.email-hygiene/decisions.json.
Read-only against the mailbox — this module never modifies mail.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import defaultdict
from datetime import timedelta

import config
from graph_client import GraphClient, GraphError

SELECT = (
    "id",
    "receivedDateTime",
    "isRead",
    "subject",
    "from",
    "toRecipients",
)

UNSUB_HEADERS = {"list-unsubscribe", "list-unsubscribe-post", "list-id"}
BULK_HEADERS = {"precedence", "x-campaign-id", "x-mailer", "feedback-id"}
MAILTO_RE = re.compile(r"<mailto:([^>]+)>", re.I)
HTTP_RE = re.compile(r"<(https?://[^>]+)>", re.I)


def _sender(message: dict) -> tuple[str, str]:
    addr = ((message.get("from") or {}).get("emailAddress") or {})
    email = (addr.get("address") or "").strip().lower()
    name = (addr.get("name") or "").strip()
    return email, name


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def collect(client: GraphClient, profile: dict) -> dict[str, dict]:
    """Aggregate message stats keyed by sender address."""
    settings = profile["analysis"]
    since = config.iso(config.utcnow() - timedelta(days=settings["lookback_days"]))
    budget = settings["max_messages"]

    stats: dict[str, dict] = defaultdict(
        lambda: {
            "name": "",
            "count": 0,
            "unread": 0,
            "subjects": set(),
            "first_seen": None,
            "last_seen": None,
            "sample_message_id": None,
            "folders": set(),
        }
    )

    for folder in settings["scan_folders"]:
        if budget <= 0:
            break
        try:
            messages = client.iter_messages(folder, since, SELECT, limit=budget)
            for message in messages:
                email, name = _sender(message)
                if not email:
                    continue
                entry = stats[email]
                entry["name"] = entry["name"] or name
                entry["count"] += 1
                if not message.get("isRead", True):
                    entry["unread"] += 1
                subject = (message.get("subject") or "").strip()
                if subject and len(entry["subjects"]) < 8:
                    entry["subjects"].add(subject[:120])
                received = message.get("receivedDateTime")
                if received:
                    if entry["first_seen"] is None or received < entry["first_seen"]:
                        entry["first_seen"] = received
                    if entry["last_seen"] is None or received > entry["last_seen"]:
                        entry["last_seen"] = received
                        entry["sample_message_id"] = message["id"]
                entry["folders"].add(folder)
                budget -= 1
                if budget <= 0:
                    break
        except GraphError as exc:
            print(f"  ! skipped folder '{folder}': {exc}", file=sys.stderr)

    return stats


def probe_unsubscribe(client: GraphClient, message_id: str) -> dict:
    """Inspect one sample message's headers for bulk-mail / unsubscribe markers."""
    result = {"has_unsubscribe": False, "bulk": False, "mailto": None, "url": None, "list_id": None}
    if not message_id:
        return result
    try:
        headers = client.get_message_headers(message_id)
    except GraphError:
        return result

    for header in headers:
        name = (header.get("name") or "").lower()
        value = header.get("value") or ""
        if name in BULK_HEADERS:
            result["bulk"] = True
        if name not in UNSUB_HEADERS:
            continue
        if name == "list-id":
            result["list_id"] = value[:120]
            result["bulk"] = True
            continue
        result["has_unsubscribe"] = True
        result["bulk"] = True
        if not result["mailto"]:
            match = MAILTO_RE.search(value)
            if match:
                result["mailto"] = match.group(1)
        if not result["url"]:
            match = HTTP_RE.search(value)
            if match:
                result["url"] = match.group(1)
    return result


def score(entry: dict, weeks: float) -> float:
    """0-100 confidence that unsubscribing is the right call."""
    volume = entry["count"]
    per_week = volume / max(weeks, 1.0)
    ignored = 1.0 - entry["read_rate"]

    volume_points = min(35.0, 12.0 * math.log2(1 + per_week))
    ignore_points = 40.0 * ignored
    bulk_points = 15.0 if entry["has_unsubscribe"] else (7.0 if entry["bulk"] else 0.0)
    repetition = 10.0 * (1.0 - min(1.0, entry["distinct_subjects"] / max(volume, 1)))
    return round(min(100.0, volume_points + ignore_points + bulk_points + repetition), 1)


def analyze(interactive: bool = True) -> dict:
    profile = config.load_profile()
    client = GraphClient(profile, interactive=interactive)
    settings = profile["analysis"]
    cleanup = profile["cleanup"]

    protected_senders = {s.lower() for s in cleanup.get("protected_senders", [])}
    protected_domains = {d.lower().lstrip("@") for d in cleanup.get("protected_domains", [])}

    print(f"Scanning {settings['scan_folders']} over {settings['lookback_days']} days...")
    raw = collect(client, profile)
    print(f"  {sum(e['count'] for e in raw.values())} messages from {len(raw)} senders")

    weeks = settings["lookback_days"] / 7.0
    min_count = settings["min_messages_for_candidate"]
    max_read = settings["max_read_rate_for_candidate"]

    senders = []
    for email, entry in raw.items():
        count = entry["count"]
        read_rate = round((count - entry["unread"]) / count, 3) if count else 0.0
        record = {
            "address": email,
            "domain": _domain(email),
            "name": entry["name"],
            "count": count,
            "unread": entry["unread"],
            "read_rate": read_rate,
            "per_week": round(count / max(weeks, 1.0), 2),
            "distinct_subjects": len(entry["subjects"]),
            "sample_subjects": sorted(entry["subjects"])[:5],
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
            "folders": sorted(entry["folders"]),
            "sample_message_id": entry["sample_message_id"],
            "protected": email in protected_senders or _domain(email) in protected_domains,
        }
        senders.append(record)

    senders.sort(key=lambda r: r["count"], reverse=True)

    shortlist = [
        r
        for r in senders
        if not r["protected"] and r["count"] >= min_count and r["read_rate"] <= max_read
    ]
    print(f"  probing headers for {len(shortlist)} candidate senders...")

    candidates = []
    for record in shortlist:
        probe = probe_unsubscribe(client, record["sample_message_id"])
        record.update(probe)
        record["score"] = score(record, weeks)
        if record["score"] >= 40:
            candidates.append(record)

    for record in senders:
        record.pop("sample_message_id", None)
        record.setdefault("score", None)

    candidates.sort(key=lambda r: r["score"], reverse=True)

    # A sender you have already ruled on should not keep showing up as a new
    # suggestion every run - park it in previously_decided instead.
    prior_decisions = {
        d["address"]: d.get("decision", "pending")
        for d in config.load_json(config.DECISIONS_PATH, {}).get("decisions", [])
    }
    pending, decided = [], []
    for record in candidates:
        choice = prior_decisions.get(record["address"], "pending")
        if choice in ("pending", None, ""):
            pending.append(record)
        else:
            record["decision"] = choice
            decided.append(record)

    analysis = {
        "generated_at": config.iso(config.utcnow()),
        "account": profile["account"]["email"],
        "lookback_days": settings["lookback_days"],
        "total_messages": sum(r["count"] for r in senders),
        "total_senders": len(senders),
        "top_senders": senders[:60],
        "unsubscribe_candidates": [
            {
                k: v
                for k, v in record.items()
                if k not in ("sample_message_id", "protected")
            }
            for record in pending
        ],
        "previously_decided": [
            {
                "address": record["address"],
                "name": record["name"],
                "count": record["count"],
                "score": record["score"],
                "decision": record["decision"],
            }
            for record in decided
        ],
    }
    config.save_json(config.ANALYSIS_PATH, analysis)
    seed_decisions(analysis)
    print(
        f"  {len(pending)} new unsubscribe candidates "
        f"({len(decided)} already decided) -> {config.ANALYSIS_PATH}"
    )
    return analysis


def seed_decisions(analysis: dict) -> dict:
    """Create/refresh decisions.json, preserving any choice already made."""
    existing = {d["address"]: d for d in config.load_json(config.DECISIONS_PATH, {}).get("decisions", [])}
    decisions = []
    for candidate in analysis["unsubscribe_candidates"]:
        prior = existing.get(candidate["address"], {})
        decisions.append(
            {
                "address": candidate["address"],
                "name": candidate["name"],
                "count": candidate["count"],
                "read_rate": candidate["read_rate"],
                "score": candidate["score"],
                "unsubscribe_url": candidate.get("url"),
                "unsubscribe_mailto": candidate.get("mailto"),
                # pending | unsubscribe | block | keep
                "decision": prior.get("decision", "pending"),
                "decided_at": prior.get("decided_at"),
            }
        )
    for address, prior in existing.items():
        if address not in {d["address"] for d in decisions}:
            decisions.append(prior)

    payload = {"updated_at": config.iso(config.utcnow()), "decisions": decisions}
    config.save_json(config.DECISIONS_PATH, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze mailbox sender patterns.")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    try:
        analysis = analyze(interactive=not args.non_interactive)
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"\nTop repeat senders:\n"
        + "\n".join(
            f"  {r['count']:>4}x  {r['read_rate']*100:>5.1f}% read  {r['address']}"
            for r in analysis["top_senders"][:15]
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
