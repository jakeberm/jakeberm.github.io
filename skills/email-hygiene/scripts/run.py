"""Orchestrator — the entry point the scheduled task runs.

    python scripts/run.py                 # dry run, no mailbox changes
    python scripts/run.py --apply         # file noise, sync rules, email the report
    python scripts/run.py --apply --non-interactive   # what the scheduler uses
"""

from __future__ import annotations

import argparse
import sys
import traceback

import analyze
import cleanup
import config
import purge
import report
import rules_sync
from graph_client import GraphClient, GraphError


def run(apply: bool, interactive: bool, skip_email: bool = False) -> int:
    profile = config.load_profile()
    started = config.utcnow()
    print(f"=== email-hygiene {'APPLY' if apply else 'DRY RUN'} — {config.iso(started)} ===")

    analysis = analyze.analyze(interactive=interactive)

    # Purge runs before cleanup deliberately. Cleanup files noise into its own
    # folder, which purge doesn't scan, so the other order would quietly
    # protect the very mail we mean to age out.
    print()
    try:
        purge_summary = purge.run(apply=apply, interactive=interactive)
    except GraphError as exc:
        print(f"  ! purge failed: {exc}", file=sys.stderr)
        purge_summary = {"deleted": 0, "errors": 0, "error": str(exc)}

    print()
    cleanup_summary = cleanup.run(apply=apply, interactive=interactive)

    print()
    try:
        rules_summary = rules_sync.sync(apply=apply, interactive=interactive)
    except GraphError as exc:
        print(f"  ! rules sync failed: {exc}", file=sys.stderr)
        rules_summary = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "error": str(exc)}

    path, body = report.write_report(analysis, cleanup_summary, rules_summary)
    print(f"\nReport: {path}")
    print()
    print(report.render_text(analysis, cleanup_summary, rules_summary))

    if apply and not skip_email and profile["report"].get("email_digest", True):
        try:
            client = GraphClient(profile, interactive=interactive)
            subject = (
                f"{profile['report']['digest_subject']} — "
                f"{len(analysis['unsubscribe_candidates'])} unsubscribe suggestions"
            )
            client.send_mail(profile["account"]["email"], subject, body)
            print("\nDigest emailed.")
        except GraphError as exc:
            print(f"  ! digest email failed: {exc}", file=sys.stderr)

    state = config.load_state()
    state["last_run"] = config.iso(started)
    state["last_mode"] = "apply" if apply else "dry-run"
    state["last_candidates"] = len(analysis["unsubscribe_candidates"])
    config.save_state(state)
    config.audit(
        "run",
        mode=state["last_mode"],
        candidates=state["last_candidates"],
        filed=cleanup_summary.get("moved", 0) + cleanup_summary.get("archived", 0),
        deleted=purge_summary.get("deleted", 0),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full email hygiene cycle.")
    parser.add_argument("--apply", action="store_true", help="perform changes (default: dry run)")
    parser.add_argument("--non-interactive", action="store_true", help="fail instead of prompting to sign in")
    parser.add_argument("--no-email", action="store_true", help="skip the digest email")
    args = parser.parse_args()

    try:
        return run(apply=args.apply, interactive=not args.non_interactive, skip_email=args.no_email)
    except config.ProfileError as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        config.audit("run_failed", error=traceback.format_exc(limit=3))
        return 1


if __name__ == "__main__":
    sys.exit(main())
