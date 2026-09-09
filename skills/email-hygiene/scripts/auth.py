"""MSAL device-code authentication for a personal Microsoft (Outlook.com) account.

Tokens are cached at ~/.email-hygiene/token_cache.bin so scheduled runs stay silent.
"""

from __future__ import annotations

import os
import sys

import msal

import config


class AuthError(RuntimeError):
    pass


def _build_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if config.TOKEN_CACHE_PATH.exists():
        cache.deserialize(config.TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    return cache


def _persist_cache(cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return
    config.ensure_dirs()
    config.TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")
    try:
        os.chmod(config.TOKEN_CACHE_PATH, 0o600)
    except OSError:
        pass


REGISTRATION_HELP = """
Consumer sign-in was rejected for this client ID.

Register your own app (2 minutes, free, no Azure subscription required):
  1. https://entra.microsoft.com  ->  Applications  ->  App registrations  ->  New registration
  2. Name: email-hygiene
  3. Supported account types: "Personal Microsoft accounts only"
  4. Redirect URI: leave empty
  5. Register, then open Authentication -> "Allow public client flows" = Yes -> Save
  6. Copy the Application (client) ID into ~/.email-hygiene/profile.json as account.client_id
""".strip()


def acquire_token(profile: dict, interactive: bool = True) -> str:
    """Return a Graph access token, refreshing silently when possible."""
    account_cfg = profile["account"]
    cache = _build_cache()
    app = msal.PublicClientApplication(
        client_id=account_cfg["client_id"],
        authority=account_cfg["authority"],
        token_cache=cache,
    )

    result = None
    accounts = app.get_accounts(username=account_cfg["email"]) or app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(config.SCOPES, account=accounts[0])

    if not result:
        if not interactive:
            raise AuthError(
                "No cached credentials and running non-interactively. "
                "Run 'python scripts/auth.py login' once to sign in."
            )
        flow = app.initiate_device_flow(scopes=config.SCOPES)
        if "user_code" not in flow:
            error = flow.get("error_description") or flow.get("error") or str(flow)
            raise AuthError(f"Could not start device code flow: {error}\n\n{REGISTRATION_HELP}")
        print("\n" + flow["message"] + "\n", flush=True)
        result = app.acquire_token_by_device_flow(flow)

    _persist_cache(cache)

    if "access_token" not in result:
        error = result.get("error_description") or result.get("error") or str(result)
        raise AuthError(f"Authentication failed: {error}")
    return result["access_token"]


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "login"
    profile = config.load_profile()

    if command == "logout":
        if config.TOKEN_CACHE_PATH.exists():
            config.TOKEN_CACHE_PATH.unlink()
            print("Token cache cleared.")
        else:
            print("No token cache to clear.")
        return 0

    if command == "status":
        try:
            acquire_token(profile, interactive=False)
        except AuthError as exc:
            print(f"Not signed in: {exc}")
            return 1
        print(f"Signed in as {profile['account']['email']} (cached token valid).")
        return 0

    acquire_token(profile, interactive=True)
    print(f"Signed in as {profile['account']['email']}. Token cached at {config.TOKEN_CACHE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
