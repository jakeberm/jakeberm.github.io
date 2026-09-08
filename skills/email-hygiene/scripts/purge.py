"""Delete aged mail from senders you have already rejected.

This is the one module in the skill that removes mail, so it is the most
conservative. A message is only eligible when ALL of these hold:

  * its sender has a recorded decision of `unsubscribe` or `block`, or the
    sender is on the noise lists, or the message is sitting in Junk
  * the message is older than the grace period (default 30 days), so a recent
    unsubscribe-confirmation is never swept away
  * the sender is not protected, and has no `keep` / `route` / `protect` decision

By default "delete" means **move to Deleted Items**, which is recoverable from
the mailbox for 30 days. Permanent removal requires the explicit `--purge` flag
on top of `--apply`, and is never used by the scheduled job.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import timedelta

import config
from analyze import _domain, _sender
from graph_client import GraphClient, GraphError

SELECT = ("id", "receivedDateTime", "isRead", "subject", "from", "parentFolderId")

# Junk is included because mail that lands there is spam by the server's own
# judgement, which is a stronger signal than anything this skill computes.
DEFAULT_FOLDERS = ("inbox", "junkemail", "archive")
REJECTED = {"unsubscribe", "block"}
SPARED = {"keep", "route", "protect"}


def _decisions() -> dict[str, str]:
    return {
        d["address"].lower(): (d.get("decision") or "pending")
        for d in config.load_json(config.DECISIONS_PATH, {}).get("decisions", [])
    }


def plan(client: GraphClient, profile: dict, older_than_days: int, folders,
         only_senders: set[str] | None = None, limit: int | None = None) -> list[dict]:
    cleanup = profile["cleanup"]
    protected_senders = {s.lower() for s in cleanup.get("protected_senders", [])}
    protected_domains = {d.lower() for d in cleanup.get("protected_domains", [])}
    noise_senders = {s.lower() for s in cleanup.get("noise_senders", [])}
    noise_domains = {d.lower() for d in cleanup.get("noise_domains", [])}
    routed = {str(r.get("sender", "")).lower() for r in cleanup.get("routes", [])}
    decisions = _decisions()
    cap = limit or cleanup["max_actions_per_run"]

    cutoff = config.iso(config.utcnow() - timedelta(days=older_than_days))
    # Look back well past the cutoff so old mail is actually reachable.
    since = config.iso(config.utcnow() - timedelta(days=max(older_than_days * 6, 365)))

    junk_id = client.resolve_folder_id("junkemail")
    actions: list[dict] = []

    for folder in folders:
        try:
            messages = client.iter_messages(folder, since, SELECT, limit=cap * 4)
        except GraphError as exc:
            print(f"  ! skipped folder '{folder}': {exc}", file=sys.stderr)
            continue

        for message in messages:
            received = message.get("receivedDateTime") or ""
            if received >= cutoff:
                continue  # too recent - inside the grace period

            email, _ = _sender(message)
            if not email:
                continue
            # An explicit sender list means "only these", and it overrides the
            # usual noise/junk eligibility so the sweep stays predictable.
            if only_senders is not None and email not in only_senders:
                continue
            domain = _domain(email)
            decision = decisions.get(email, "pending")

            if decision in SPARED:
                continue
            if email in protected_senders or domain in protected_domains:
                continue
            if email in routed:
                continue

            if decision in REJECTED:
                reason = f"decided:{decision}"
            elif email in noise_senders or domain in noise_domains:
                reason = "noise-list"
            elif junk_id and message.get("parentFolderId") == junk_id:
                reason = "in-junk"
            else:
                continue

            actions.append(
                {
                    "id": message["id"],
                    "address": email,
                    "subject": (message.get("subject") or "")[:100],
                    "received": received,
                    "folder": folder,
                    "reason": reason,
                }
            )

    actions.sort(key=lambda a: a["received"])
    return actions[:cap]


def summarize(actions: list[dict]) -> None:
    if not actions:
        print("  nothing eligible for deletion")
        return
    by_sender: dict[str, list] = defaultdict(list)
    for a in actions:
        by_sender[a["address"]].append(a)

    print(f"  {len(actions)} message(s) from {len(by_sender)} sender(s) eligible:")
    print(f"    {'msgs':>4}  {'oldest':10}  {'newest':10}  {'reason':14}  sender")
    for address, items in sorted(by_sender.items(), key=lambda kv: -len(kv[1])):
        print(
            f"    {len(items):>4}  {items[0]['received'][:10]:10}  "
            f"{items[-1]['received'][:10]:10}  {items[0]['reason']:14}  {address}"
        )


def execute(client: GraphClient, actions: list[dict], purge: bool) -> dict:
    counts = {"deleted": 0, "errors": 0}
    target = None if purge else client.resolve_folder_id("deleteditems")

    for action in actions:
        try:
            if purge:
                client.delete_message(action["id"])
            else:
                client.move_message(action["id"], target)
            counts["deleted"] += 1
            config.audit(
                "purge" if purge else "delete",
                address=action["address"],
                subject=action["subject"],
                received=action["received"],
                reason=action["reason"],
            )
        except GraphError as exc:
            counts["errors"] += 1
            print(f"  ! {action['address']}: {exc}", file=sys.stderr)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete aged mail from rejected senders (dry run by default)."
    )
    parser.add_argument("--apply", action="store_true", help="actually delete")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="permanently remove instead of moving to Deleted Items (requires --apply)",
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=30,
        help="grace period in days; newer mail is never touched (default 30)",
    )
    parser.add_argument("--folders", nargs="+", default=list(DEFAULT_FOLDERS))
    parser.add_argument(
        "--senders",
        nargs="+",
        help="restrict the sweep to these exact addresses",
    )
    parser.add_argument(
        "--senders-file",
        help="file with one sender address per line (avoids shell length limits)",
    )
    parser.add_argument("--limit", type=int, help="override max_actions_per_run")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    if args.purge and not args.apply:
        print("--purge requires --apply", file=sys.stderr)
        return 2

    only_senders = None
    if args.senders or args.senders_file:
        only_senders = {s.strip().lower() for s in (args.senders or []) if s.strip()}
        if args.senders_file:
            with open(args.senders_file, encoding="utf-8") as fh:
                only_senders |= {
                    line.strip().lower()
                    for line in fh
                    if line.strip() and not line.startswith("#")
                }

    try:
        profile = config.load_profile()
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2

    client = GraphClient(profile)
    mode = "PURGE (permanent)" if args.purge else "delete -> Deleted Items (recoverable)"
    scope = f" | {len(only_senders)} sender(s)" if only_senders else ""
    print(f"purge plan: older than {args.older_than}d | mode: {mode}{scope}")

    try:
        actions = plan(client, profile, args.older_than, args.folders,
                       only_senders, args.limit)
    except GraphError as exc:
        print(f"graph error: {exc}", file=sys.stderr)
        return 1

    summarize(actions)

    if not args.apply:
        print("\n  dry run - nothing deleted. re-run with --apply to execute.")
        return 0
    if not actions:
        return 0

    if not args.non_interactive:
        verb = "PERMANENTLY DELETE" if args.purge else "move to Deleted Items"
        answer = input(f"\n{verb} {len(actions)} message(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted")
            return 1

    counts = execute(client, actions, args.purge)
    print(f"  deleted {counts['deleted']}, errors {counts['errors']}")
    config.audit("purge_run", purge=args.purge, older_than=args.older_than, **counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
