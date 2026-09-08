"""Sync server-side Outlook message rules from the profile + blocked-sender decisions.

Server rules run even when the scheduled job doesn't, so blocked senders never hit the inbox.
Only rules whose displayName starts with the configured prefix are touched.
"""

from __future__ import annotations

import argparse
import sys

import config
from graph_client import GraphClient, GraphError


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def blocked_addresses() -> list[str]:
    payload = config.load_json(config.DECISIONS_PATH, {})
    return sorted(
        {
            d["address"].lower()
            for d in payload.get("decisions", [])
            if d.get("decision") in ("block", "unsubscribe")
        }
    )


def build_plan(profile: dict, folder_ids: dict[str, str]) -> list[dict]:
    """Return the desired set of [Hygiene] rules. folder_ids maps folder name -> id."""
    rules_cfg = profile["rules"]
    prefix = rules_cfg["prefix"]
    batch_size = rules_cfg.get("max_senders_per_rule", 40)
    noise_folder_id = folder_ids[profile["folders"]["noise"]]

    senders = sorted(
        {s.lower() for s in profile["cleanup"].get("noise_senders", [])}
        | set(blocked_addresses())
    )
    domains = sorted({d.lower().lstrip("@") for d in profile["cleanup"].get("noise_domains", [])})

    planned: list[dict] = []
    sequence = 10

    for route in profile["cleanup"].get("routes", []):
        folder = route.get("folder")
        targets = sorted(
            {s.lower() for s in route.get("senders", [])}
            | {d.lower().lstrip("@") for d in route.get("domains", [])}
        )
        if not folder or not targets:
            continue
        for index, batch in enumerate(_chunks(targets, batch_size), start=1):
            suffix = f" #{index}" if len(targets) > batch_size else ""
            planned.append(
                {
                    "displayName": f"{prefix}Route {folder}{suffix}",
                    "sequence": sequence,
                    "isEnabled": True,
                    "conditions": {"senderContains": batch},
                    "actions": {
                        "markAsRead": bool(route.get("mark_read", False)),
                        "moveToFolder": folder_ids[folder],
                        "stopProcessingRules": True,
                    },
                }
            )
            sequence += 1

    for index, batch in enumerate(_chunks(senders, batch_size), start=1):
        suffix = f" #{index}" if len(senders) > batch_size else ""
        planned.append(
            {
                "displayName": f"{prefix}Noise senders{suffix}",
                "sequence": sequence,
                "isEnabled": True,
                "conditions": {"senderContains": batch},
                "actions": {
                    "markAsRead": True,
                    "moveToFolder": noise_folder_id,
                    "stopProcessingRules": True,
                },
            }
        )
        sequence += 1

    if domains:
        planned.append(
            {
                "displayName": f"{prefix}Noise domains",
                "sequence": sequence,
                "isEnabled": True,
                "conditions": {"senderContains": domains},
                "actions": {
                    "markAsRead": True,
                    "moveToFolder": noise_folder_id,
                    "stopProcessingRules": True,
                },
            }
        )
    return planned


def _matches(existing: dict, planned: dict) -> bool:
    def norm(rule: dict) -> tuple:
        conditions = rule.get("conditions") or {}
        actions = rule.get("actions") or {}
        return (
            rule.get("isEnabled"),
            tuple(sorted(s.lower() for s in conditions.get("senderContains") or [])),
            actions.get("moveToFolder"),
            bool(actions.get("markAsRead")),
        )

    return norm(existing) == norm(planned)


def sync(apply: bool = False, interactive: bool = True) -> dict:
    profile = config.load_profile()
    if not profile["rules"].get("enabled", True):
        print("Rules sync disabled in profile.")
        return {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0}

    client = GraphClient(profile, interactive=interactive)
    prefix = profile["rules"]["prefix"]

    folder_names = [profile["folders"]["noise"]] + [
        r["folder"] for r in profile["cleanup"].get("routes", []) if r.get("folder")
    ]
    folder_ids: dict[str, str] = {}
    for name in folder_names:
        if apply:
            folder_ids[name] = client.ensure_folder(name)
        else:
            folder_ids[name] = client.resolve_folder_path(name) or f"<{name}>"

    planned = build_plan(profile, folder_ids)
    existing = [r for r in client.list_rules() if (r.get("displayName") or "").startswith(prefix)]
    by_name = {r["displayName"]: r for r in existing}

    summary = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    mode = "APPLY" if apply else "DRY RUN"
    print(f"Rules sync [{mode}]: {len(planned)} desired, {len(existing)} existing")

    for rule in planned:
        current = by_name.pop(rule["displayName"], None)
        if current is None:
            print(f"  + create  {rule['displayName']}  ({len(rule['conditions']['senderContains'])} senders)")
            if apply:
                client.create_rule(rule)
                config.audit("rule_create", name=rule["displayName"])
            summary["created"] += 1
        elif not _matches(current, rule):
            print(f"  ~ update  {rule['displayName']}")
            if apply:
                client.update_rule(current["id"], rule)
                config.audit("rule_update", name=rule["displayName"])
            summary["updated"] += 1
        else:
            summary["unchanged"] += 1

    for name, stale in by_name.items():
        print(f"  - delete  {name}")
        if apply:
            client.delete_rule(stale["id"])
            config.audit("rule_delete", name=name)
        summary["deleted"] += 1

    return summary


def list_rules(interactive: bool = True) -> int:
    profile = config.load_profile()
    client = GraphClient(profile, interactive=interactive)
    prefix = profile["rules"]["prefix"]
    for rule in client.list_rules():
        owned = "*" if (rule.get("displayName") or "").startswith(prefix) else " "
        state = "on " if rule.get("isEnabled") else "off"
        print(f"{owned} [{state}] {rule.get('sequence'):>3}  {rule.get('displayName')}")
    print("\n* = managed by this skill")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Outlook server-side rules.")
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "list"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "list":
            return list_rules(interactive=not args.non_interactive)
        summary = sync(apply=args.apply, interactive=not args.non_interactive)
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2
    except GraphError as exc:
        print(f"Graph error: {exc}", file=sys.stderr)
        return 1
    print(
        f"\ncreated={summary['created']} updated={summary['updated']} "
        f"deleted={summary['deleted']} unchanged={summary['unchanged']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
