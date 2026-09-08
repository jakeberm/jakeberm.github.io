"""Apply mailbox cleanup actions. Dry-run by default; --apply performs changes.

Never permanently deletes: messages are only moved between folders and marked read.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta

import config
from graph_client import GraphClient, GraphError

SELECT = ("id", "receivedDateTime", "isRead", "subject", "from")


def _sender_address(message: dict) -> str:
    return (((message.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def _compile(patterns: list[str]) -> list[re.Pattern]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.I))
        except re.error as exc:
            print(f"  ! bad subject pattern {pattern!r}: {exc}", file=sys.stderr)
    return compiled


def blocked_addresses() -> set[str]:
    """Addresses the user explicitly marked 'block' in decisions.json."""
    payload = config.load_json(config.DECISIONS_PATH, {})
    return {
        d["address"].lower()
        for d in payload.get("decisions", [])
        if d.get("decision") == "block"
    }


def plan(client: GraphClient, profile: dict) -> list[dict]:
    """Build the list of intended actions without performing any of them."""
    cleanup = profile["cleanup"]
    if not cleanup.get("enabled", True):
        return []

    noise_senders = {s.lower() for s in cleanup.get("noise_senders", [])} | blocked_addresses()
    noise_domains = {d.lower().lstrip("@") for d in cleanup.get("noise_domains", [])}
    subject_patterns = _compile(cleanup.get("noise_subject_patterns", []))
    protected_senders = {s.lower() for s in cleanup.get("protected_senders", [])}
    protected_domains = {d.lower().lstrip("@") for d in cleanup.get("protected_domains", [])}

    lookback = profile["analysis"]["lookback_days"]
    since = config.iso(config.utcnow() - timedelta(days=lookback))
    cap = cleanup.get("max_actions_per_run", 300)

    actions: list[dict] = []
    for message in client.iter_messages("inbox", since, SELECT, limit=profile["analysis"]["max_messages"]):
        if len(actions) >= cap:
            break
        address = _sender_address(message)
        domain = _domain(address)
        if address in protected_senders or domain in protected_domains:
            continue

        subject = message.get("subject") or ""
        reason = None
        if address in noise_senders:
            reason = "noise sender"
        elif domain in noise_domains:
            reason = "noise domain"
        elif any(p.search(subject) for p in subject_patterns):
            reason = "noise subject"

        if reason:
            actions.append(
                {
                    "message_id": message["id"],
                    "address": address,
                    "subject": subject[:120],
                    "received": message.get("receivedDateTime"),
                    "action": "move_to_noise",
                    "reason": reason,
                    "mark_read": cleanup.get("mark_noise_as_read", True)
                    and not message.get("isRead", True),
                }
            )
            continue

        stale_days = cleanup.get("archive_read_newsletters_after_days", 0)
        if stale_days and message.get("isRead") and message.get("receivedDateTime"):
            cutoff = config.iso(config.utcnow() - timedelta(days=stale_days))
            if message["receivedDateTime"] < cutoff and address in noise_senders:
                actions.append(
                    {
                        "message_id": message["id"],
                        "address": address,
                        "subject": subject[:120],
                        "received": message.get("receivedDateTime"),
                        "action": "archive",
                        "reason": f"read and older than {stale_days}d",
                        "mark_read": False,
                    }
                )
    return actions


def execute(client: GraphClient, profile: dict, actions: list[dict], apply: bool) -> dict:
    summary = {"planned": len(actions), "moved": 0, "archived": 0, "marked_read": 0, "errors": 0}
    if not actions:
        return summary

    noise_folder_id = archive_folder_id = None
    if apply:
        noise_folder_id = client.ensure_folder(profile["folders"]["noise"])
        archive_folder_id = client.resolve_folder_id("archive") or noise_folder_id

    for action in actions:
        if not apply:
            continue
        destination = noise_folder_id if action["action"] == "move_to_noise" else archive_folder_id
        try:
            if action["mark_read"]:
                client.mark_read(action["message_id"])
                summary["marked_read"] += 1
            client.move_message(action["message_id"], destination)
            summary["moved" if action["action"] == "move_to_noise" else "archived"] += 1
            config.audit(
                "cleanup",
                action=action["action"],
                address=action["address"],
                reason=action["reason"],
                subject=action["subject"],
            )
        except GraphError as exc:
            summary["errors"] += 1
            print(f"  ! {action['address']}: {exc}", file=sys.stderr)
    return summary


def run(apply: bool = False, interactive: bool = True) -> dict:
    profile = config.load_profile()
    client = GraphClient(profile, interactive=interactive)
    actions = plan(client, profile)
    mode = "APPLY" if apply else "DRY RUN"
    print(f"Cleanup [{mode}]: {len(actions)} message(s) matched")
    for action in actions[:20]:
        print(f"  {action['action']:<14} {action['reason']:<14} {action['address']}  {action['subject'][:60]}")
    if len(actions) > 20:
        print(f"  ... and {len(actions) - 20} more")
    summary = execute(client, profile, actions, apply)
    summary["actions"] = actions
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up the inbox.")
    parser.add_argument("--apply", action="store_true", help="perform changes (default: dry run)")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    try:
        summary = run(apply=args.apply, interactive=not args.non_interactive)
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(
        f"\nmoved={summary['moved']} archived={summary['archived']} "
        f"marked_read={summary['marked_read']} errors={summary['errors']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
