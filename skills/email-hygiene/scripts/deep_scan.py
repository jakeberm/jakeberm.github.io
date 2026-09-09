"""Long-window, no-floor sweep for subscription lists the normal scan misses.

analyze.py is tuned for the recurring cron pass: 90 days, Inbox-ish folders, and a
minimum message count before a sender is worth probing. That deliberately misses
low-volume drip lists -- a retailer mailing you four times a quarter never clears
the floor, so it never gets header-probed and never appears as a candidate.

This module is the occasional deep audit instead:
  * long lookback (default 365 days)
  * no minimum message count
  * includes Archive / Deleted Items / Junk
  * probes headers for EVERY sender, not just a shortlist
  * reports the address each list actually has on file, which reveals mail
    arriving via a forward from another account

Read-only. This module never modifies mail and never follows an opt-out link.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import config
from analyze import _domain, _sender, probe_unsubscribe
from graph_client import GraphClient, GraphError

SELECT = (
    "id",
    "receivedDateTime",
    "isRead",
    "subject",
    "from",
    "toRecipients",
)

DEFAULT_FOLDERS = ("inbox", "archive", "deleteditems", "junkemail")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# ESPs put their own opt-out mailbox in List-Unsubscribe. Those are routing
# endpoints, not the subscriber's identity, so they must not be mistaken for
# "this list is under a different address".
OPTOUT_MARKERS = ("unsub", "optout", "opt-out", "leave", "bounce", "remove", "spamproc")
TLD_TAIL_RE = re.compile(r"\.(com|net|org|edu|gov|io|co|uk|de|se)$", re.I)


def _is_plausible_subscriber(address: str) -> bool:
    """True only for something that looks like a real personal mailbox."""
    local, _, domain = address.partition("@")
    if not local or not domain:
        return False
    # Opaque ESP tokens are long and meaningless.
    if len(local) > 40:
        return False
    haystack = f"{local}.{domain}".lower()
    if any(marker in haystack for marker in OPTOUT_MARKERS):
        return False
    # e.g. "example.com@abc12.esp-mail.net" - the local part is itself a domain.
    if TLD_TAIL_RE.search(local):
        return False
    return True


# Spam fingerprints. These only ever FLAG a sender for review -- nothing is
# deleted on this evidence. The user promotes a flagged sender with
# `decide.py set <addr> block`, and only then does purge.py act on it.
GIBBERISH_LOCAL_RE = re.compile(r"^[a-z]+\.[a-z]{3,4}$")          # contact.wuk@
RANDOM_TOKEN_RE = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}")        # udqzf, qwfnffabcjemfq
# No \b anchor: the tell is a capital mid-word, as in "FreeToys" / "LoseWeight".
RUNTOGETHER_RE = re.compile(r"[a-z]{2,}[A-Z][a-z]{2,}")

# Weighted so a single weak signal cannot flag a legitimate sender. Mail the
# server already binned, or subjects with spam-typical run-together words, count
# double; "never opened" and "no opt-out header" are common on harmless low
# volume mail and count single.
SIGNAL_WEIGHTS = {
    "server-marked-junk": 2,
    "run-together-words": 2,
    "throwaway-domain": 2,
    "random-sender": 1,
    "never-opened": 1,
    "no-optout-header": 1,
}
SPAM_THRESHOLD = 4


def spam_signals(record: dict, throwaway_domains: set[str]) -> list[str]:
    """Cheap, explainable spam indicators for a sender we have already probed."""
    reasons = []
    local = record["address"].partition("@")[0]
    subjects = record.get("sample_subjects") or []

    if record["count"] and record["read_rate"] == 0.0:
        reasons.append("never-opened")
    if not record.get("has_unsubscribe"):
        reasons.append("no-optout-header")
    if GIBBERISH_LOCAL_RE.match(local) or RANDOM_TOKEN_RE.search(local):
        reasons.append("random-sender")
    if any(RUNTOGETHER_RE.search(s) for s in subjects):
        reasons.append("run-together-words")
    if "junkemail" in (record.get("folders") or []):
        reasons.append("server-marked-junk")
    if record["domain"] in throwaway_domains:
        reasons.append("throwaway-domain")
    return reasons


def spam_score(reasons) -> int:
    return sum(SIGNAL_WEIGHTS.get(r, 0) for r in reasons)


def find_throwaway_domains(senders) -> set[str]:
    """Domains fronting several random-looking senders are rented spam infrastructure.

    A real business mails you from one or two stable addresses. Three different
    gibberish local parts on one domain is a spam pattern, not a company.
    """
    by_domain: dict[str, set[str]] = {}
    for s in senders:
        local = s["address"].partition("@")[0]
        if GIBBERISH_LOCAL_RE.match(local) or RANDOM_TOKEN_RE.search(local):
            by_domain.setdefault(s["domain"], set()).add(local)
    return {domain for domain, locals_ in by_domain.items() if len(locals_) >= 2}





def subscribed_address(url: str | None, mailto: str | None) -> str | None:
    """Recover the address a bulk list has on file from its opt-out target.

    Most ESPs (Klaviyo, Mailchimp, SendGrid, ...) embed the subscriber address as a
    query parameter on the List-Unsubscribe URL. When that address differs from the
    mailbox we are scanning, the mail is reaching us via a forward -- which matters
    a lot, because filtering it here would never take us off the list.
    """
    for candidate in (url, mailto):
        if not candidate:
            continue
        text = unquote(candidate)
        try:
            query = urlsplit(text).query
        except ValueError:
            query = ""
        for values in parse_qs(query).values():
            for value in values:
                value = value.strip().lower()
                if EMAIL_RE.fullmatch(value) and _is_plausible_subscriber(value):
                    return value
        match = EMAIL_RE.search(text)
        if match and _is_plausible_subscriber(match.group(0).lower()):
            return match.group(0).lower()
    return None


def collect(client: GraphClient, folders, since_iso: str, budget: int) -> dict[str, dict]:
    stats: dict[str, dict] = defaultdict(
        lambda: {
            "name": "",
            "count": 0,
            "unread": 0,
            "subjects": set(),
            "recipients": set(),
            "first_seen": None,
            "last_seen": None,
            "sample_message_id": None,
            "folders": set(),
        }
    )

    for folder in folders:
        if budget <= 0:
            break
        print(f"  scanning {folder}...", flush=True)
        try:
            for message in client.iter_messages(folder, since_iso, SELECT, limit=budget):
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
                for recipient in message.get("toRecipients") or []:
                    addr = ((recipient.get("emailAddress") or {}).get("address") or "").lower()
                    if addr:
                        entry["recipients"].add(addr)
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


def mailbox_addresses(profile: dict) -> set[str]:
    """Every address that delivers directly into this mailbox (primary + aliases)."""
    account = profile["account"]["email"].lower()
    aliases = {a.lower() for a in profile["account"].get("aliases", []) if a}
    return {account} | aliases


def run(
    client: GraphClient,
    profile: dict,
    lookback_days: int,
    folders,
    max_messages: int,
    max_probe: int,
) -> dict:
    account = profile["account"]["email"].lower()
    mine = mailbox_addresses(profile)
    protected_senders = {s.lower() for s in profile["cleanup"].get("protected_senders", [])}
    protected_domains = {d.lower() for d in profile["cleanup"].get("protected_domains", [])}
    decided = {
        d["address"].lower(): d.get("decision", "pending")
        for d in config.load_json(config.DECISIONS_PATH, {}).get("decisions", [])
    }

    since = config.iso(config.utcnow() - timedelta(days=lookback_days))
    print(f"deep scan: {lookback_days}d lookback across {', '.join(folders)}")
    stats = collect(client, folders, since, max_messages)
    print(f"  {sum(v['count'] for v in stats.values())} messages from {len(stats)} senders")

    # Probe newest-first so a truncated run still covers the senders that matter most.
    order = sorted(stats.items(), key=lambda kv: kv[1]["last_seen"] or "", reverse=True)
    probe_targets = [kv for kv in order if kv[0] != account][:max_probe]
    print(f"  probing headers for {len(probe_targets)} senders...", flush=True)

    senders = []
    for email, entry in probe_targets:
        probe = probe_unsubscribe(client, entry["sample_message_id"])
        on_file = subscribed_address(probe.get("url"), probe.get("mailto"))
        source = "unsubscribe-link" if on_file else None
        external = sorted(r for r in entry["recipients"] if r not in mine)
        # An alias delivers straight into this mailbox, so it is ours to act on;
        # only a genuinely different account means the opt-out must happen elsewhere.
        alias_hits = sorted(r for r in entry["recipients"] if r in mine and r != account)
        if not on_file and len(external) == 1:
            on_file = external[0]
            source = "to-header"
        if not on_file and alias_hits:
            on_file = alias_hits[0]
            source = "to-header"

        if on_file and on_file not in mine:
            delivery = "forward"
        elif on_file and on_file != account:
            delivery = "alias"
        elif on_file or not alias_hits:
            delivery = "direct" if on_file else "unknown"
        else:
            delivery = "alias"
        count = entry["count"]
        senders.append(
            {
                "address": email,
                "domain": _domain(email),
                "name": entry["name"],
                "count": count,
                "unread": entry["unread"],
                "read_rate": round((count - entry["unread"]) / count, 3) if count else 0.0,
                "first_seen": entry["first_seen"],
                "last_seen": entry["last_seen"],
                "folders": sorted(entry["folders"]),
                "sample_subjects": sorted(entry["subjects"])[:5],
                "has_unsubscribe": probe.get("has_unsubscribe", False),
                "bulk": probe.get("bulk", False),
                "unsubscribe_url": probe.get("url"),
                "unsubscribe_mailto": probe.get("mailto"),
                "list_id": probe.get("list_id"),
                "subscribed_as": on_file,
                "subscribed_as_source": source,
                "delivery": delivery,
                "recipients": external,
                "via_forward": delivery == "forward",
                "decision": decided.get(email, "pending"),
                "protected": email in protected_senders or config.domain_matches(_domain(email), protected_domains),
            }
        )

    subscriptions = [s for s in senders if s["has_unsubscribe"] or s["bulk"]]
    subscriptions.sort(key=lambda s: (s["last_seen"] or ""), reverse=True)

    # Spam is scored over ALL probed senders, not just ones with list headers -
    # the worst offenders deliberately omit List-Unsubscribe.
    throwaway = find_throwaway_domains(senders)
    for s in senders:
        s["spam_signals"] = spam_signals(s, throwaway)
        s["spam_score"] = spam_score(s["spam_signals"])
    suspected_spam = [
        s
        for s in senders
        if s["spam_score"] >= SPAM_THRESHOLD
        and not s["protected"]
        and s["decision"] == "pending"
    ]
    suspected_spam.sort(key=lambda s: (-s["spam_score"], -s["count"]))

    result = {
        "generated_at": config.iso(config.utcnow()),
        "account": account,
        "lookback_days": lookback_days,
        "folders": list(folders),
        "total_senders": len(stats),
        "probed": len(senders),
        "subscriptions": subscriptions,
        "forwarded_subscriptions": [s for s in subscriptions if s["via_forward"]],
        "alias_subscriptions": [s for s in subscriptions if s["delivery"] == "alias"],
        "suspected_spam": suspected_spam,
        "undecided_subscriptions": [
            s for s in subscriptions if s["decision"] == "pending" and not s["protected"]
        ],
    }
    config.save_json(config.DEEP_SCAN_PATH, result)
    return result


def summarize(result: dict) -> None:
    subs = result["subscriptions"]
    new = result["undecided_subscriptions"]
    fwd = result["forwarded_subscriptions"]

    print()
    print(f"{len(subs)} bulk/subscription senders found ({len(new)} not yet decided)")

    if fwd:
        by_address: dict[str, list] = {}
        for s in fwd:
            by_address.setdefault(s["subscribed_as"], []).append(s)
        print()
        print(f"!! {len(fwd)} list(s) across {len(by_address)} other address(es) reach this "
              f"mailbox indirectly.")
        print("   Filtering them here hides the mail but does NOT unsubscribe you -")
        print("   the opt-out has to happen for the address the list actually holds.")
        for address, items in sorted(by_address.items(), key=lambda kv: -len(kv[1])):
            print(f"\n   {address}  ({len(items)} lists)")
            for s in sorted(items, key=lambda s: -s["count"])[:12]:
                print(f"     {s['count']:>3} msgs  {s['address']}")
            if len(items) > 12:
                print(f"     ... and {len(items) - 12} more")

    if new:
        print()
        print("Not yet decided:")
        print(f"  {'msgs':>4}  {'read':>5}  {'last seen':10}  {'opt-out':7}  address")
        for s in new:
            print(
                f"  {s['count']:>4}  {s['read_rate']:>5.2f}  "
                f"{(s['last_seen'] or '')[:10]:10}  "
                f"{'yes' if s['has_unsubscribe'] else 'no':7}  {s['address']}"
            )

    spam = result.get("suspected_spam") or []
    if spam:
        print()
        print(f"Suspected spam ({len(spam)}) - flagged only, nothing deleted on this evidence:")
        for s in spam[:20]:
            print(f"  score {s['spam_score']}  {s['count']:>3} msgs  {s['address']}")
            print(f"        {', '.join(s['spam_signals'])}")
        if len(spam) > 20:
            print(f"  ... and {len(spam) - 20} more")

    print()
    print(f"full detail -> {config.DEEP_SCAN_PATH}")

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deep, long-window subscription audit (read-only)."
    )
    parser.add_argument("--days", type=int, default=365, help="lookback window (default 365)")
    parser.add_argument(
        "--folders",
        nargs="+",
        default=list(DEFAULT_FOLDERS),
        help="folders to scan (default: inbox archive deleteditems junkemail)",
    )
    parser.add_argument("--max-messages", type=int, default=5000)
    parser.add_argument("--max-probe", type=int, default=250, help="cap on header probes")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    try:
        profile = config.load_profile()
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2

    client = GraphClient(profile)
    try:
        result = run(
            client,
            profile,
            lookback_days=args.days,
            folders=args.folders,
            max_messages=args.max_messages,
            max_probe=args.max_probe,
        )
    except GraphError as exc:
        print(f"graph error: {exc}", file=sys.stderr)
        return 1

    summarize(result)
    config.audit(
        "deep_scan",
        lookback_days=args.days,
        senders=result["total_senders"],
        subscriptions=len(result["subscriptions"]),
        forwarded=len(result["forwarded_subscriptions"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
