"""First-run setup: create ~/.email-hygiene/profile.json from the shipped template."""

from __future__ import annotations

import argparse
import sys

import config


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the email-hygiene profile.")
    parser.add_argument("--email", required=True, help="your Outlook.com address")
    parser.add_argument("--client-id", default="", help="Entra app client ID (optional)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing profile")
    args = parser.parse_args()

    if config.PROFILE_PATH.exists() and not args.force:
        print(f"Profile already exists at {config.PROFILE_PATH} (use --force to overwrite).")
        return 1

    profile = config.load_template()
    profile["account"]["email"] = args.email
    profile["account"]["client_id"] = args.client_id or config.DEFAULT_CLIENT_ID
    config.save_profile(profile)

    print(f"Wrote {config.PROFILE_PATH}")
    print("Next:  python scripts/auth.py login")
    return 0


if __name__ == "__main__":
    sys.exit(main())
