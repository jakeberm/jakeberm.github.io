"""Minimal Microsoft Graph REST client: paging, throttling backoff, mail + rules helpers."""

from __future__ import annotations

import time
from typing import Iterable, Iterator
from urllib.parse import quote

import requests

import config
from auth import acquire_token

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_RETRIES = 5
WELL_KNOWN_FOLDERS = {
    "inbox",
    "archive",
    "drafts",
    "sentitems",
    "deleteditems",
    "junkemail",
    "clutter",
    "msgfolderroot",
}


class GraphError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Graph {status}: {message}")
        self.status = status


class GraphClient:
    def __init__(self, profile: dict, interactive: bool = True):
        self.profile = profile
        self._token = acquire_token(profile, interactive=interactive)
        self._session = requests.Session()
        self._folder_cache: dict[str, str] = {}

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = f"{GRAPH}{url}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        headers.setdefault("Accept", "application/json")

        for attempt in range(MAX_RETRIES):
            response = self._session.request(method, url, headers=headers, timeout=60, **kwargs)
            if response.status_code in (429, 503, 504):
                wait = int(response.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            if response.status_code == 401 and attempt == 0:
                self._token = acquire_token(self.profile, interactive=False)
                headers["Authorization"] = f"Bearer {self._token}"
                continue
            if response.status_code >= 400:
                raise GraphError(response.status_code, response.text[:600])
            return response
        raise GraphError(response.status_code, "retries exhausted")

    def get(self, url: str, params: dict | None = None) -> dict:
        return self._request("GET", url, params=params).json()

    def post(self, url: str, payload: dict) -> dict:
        response = self._request("POST", url, json=payload)
        return response.json() if response.content else {}

    def patch(self, url: str, payload: dict) -> dict:
        response = self._request("PATCH", url, json=payload)
        return response.json() if response.content else {}

    def delete(self, url: str) -> None:
        self._request("DELETE", url)

    def paged(self, url: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        """Yield items across @odata.nextLink pages, stopping at `limit`."""
        seen = 0
        page = self.get(url, params=params)
        while True:
            for item in page.get("value", []):
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return
            page = self.get(next_link)

    # ------------------------------------------------------------------- mail

    def me(self) -> dict:
        return self.get("/me", params={"$select": "displayName,mail,userPrincipalName"})

    def list_folders(self) -> list[dict]:
        return list(
            self.paged(
                "/me/mailFolders",
                params={"$top": 100, "$select": "id,displayName,parentFolderId"},
            )
        )

    def resolve_folder_id(self, name: str) -> str | None:
        """Map a well-known name or display name to a folder id."""
        key = name.lower()
        if key in WELL_KNOWN_FOLDERS:
            return key
        if key in self._folder_cache:
            return self._folder_cache[key]
        for folder in self.list_folders():
            self._folder_cache[folder["displayName"].lower()] = folder["id"]
        return self._folder_cache.get(key)

    def list_child_folders(self, parent_id: str) -> list[dict]:
        return list(
            self.paged(
                f"/me/mailFolders/{quote(parent_id)}/childFolders",
                params={"$top": 100, "$select": "id,displayName"},
            )
        )

    def _split_path(self, path: str) -> tuple[str, list[str]]:
        """Split 'Inbox/GitHub/PRs' into (root_folder_id, ['GitHub', 'PRs'])."""
        parts = [p.strip() for p in path.replace("\\", "/").split("/") if p.strip()]
        root = "msgfolderroot"
        if parts and parts[0].lower().replace(" ", "") in WELL_KNOWN_FOLDERS:
            root = parts[0].lower().replace(" ", "")
            parts = parts[1:]
        return root, parts

    def _find_child(self, parent_id: str, name: str) -> str | None:
        key = f"{parent_id}/{name.lower()}"
        if key in self._folder_cache:
            return self._folder_cache[key]
        for folder in self.list_child_folders(parent_id):
            self._folder_cache[f"{parent_id}/{folder['displayName'].lower()}"] = folder["id"]
        return self._folder_cache.get(key)

    def resolve_folder_path(self, path: str) -> str | None:
        """Resolve a possibly-nested folder path without creating anything."""
        parent, parts = self._split_path(path)
        if not parts:
            return parent
        # A bare name may also be an existing top-level folder.
        if len(parts) == 1:
            direct = self.resolve_folder_id(parts[0])
            if direct:
                return direct
        for part in parts:
            found = self._find_child(parent, part)
            if not found:
                return None
            parent = found
        return parent

    def ensure_folder(self, path: str) -> str:
        """Return the id of a folder path like 'Inbox/GitHub', creating levels as needed."""
        parent, parts = self._split_path(path)
        if not parts:
            return parent
        if len(parts) == 1:
            direct = self.resolve_folder_id(parts[0])
            if direct:
                return direct
        for part in parts:
            found = self._find_child(parent, part)
            if not found:
                created = self.post(
                    f"/me/mailFolders/{quote(parent)}/childFolders",
                    {"displayName": part},
                )
                self._folder_cache[f"{parent}/{part.lower()}"] = created["id"]
                found = created["id"]
            parent = found
        return parent

    def iter_messages(
        self,
        folder: str,
        since_iso: str,
        select: Iterable[str],
        limit: int | None = None,
    ) -> Iterator[dict]:
        folder_id = self.resolve_folder_id(folder)
        if not folder_id:
            return iter(())
        path = f"/me/mailFolders/{quote(folder_id)}/messages"
        params = {
            "$top": 100,
            "$select": ",".join(select),
            "$filter": f"receivedDateTime ge {since_iso}",
            "$orderby": "receivedDateTime desc",
        }
        return self.paged(path, params=params, limit=limit)

    def get_message_headers(self, message_id: str) -> list[dict]:
        data = self.get(
            f"/me/messages/{quote(message_id)}",
            params={"$select": "internetMessageHeaders"},
        )
        return data.get("internetMessageHeaders") or []

    def move_message(self, message_id: str, destination_id: str) -> dict:
        return self.post(
            f"/me/messages/{quote(message_id)}/move",
            {"destinationId": destination_id},
        )

    def mark_read(self, message_id: str, is_read: bool = True) -> dict:
        return self.patch(f"/me/messages/{quote(message_id)}", {"isRead": is_read})

    def delete_message(self, message_id: str) -> None:
        """Permanently remove a message. Prefer move_message to Deleted Items."""
        self.delete(f"/me/messages/{quote(message_id)}")

    def send_mail(self, to_address: str, subject: str, html_body: str) -> None:
        self.post(
            "/me/sendMail",
            {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [{"emailAddress": {"address": to_address}}],
                },
                "saveToSentItems": False,
            },
        )

    # ------------------------------------------------------------------ rules

    def list_rules(self) -> list[dict]:
        return list(self.paged("/me/mailFolders/inbox/messageRules"))

    def create_rule(self, rule: dict) -> dict:
        return self.post("/me/mailFolders/inbox/messageRules", rule)

    def update_rule(self, rule_id: str, rule: dict) -> dict:
        return self.patch(f"/me/mailFolders/inbox/messageRules/{quote(rule_id)}", rule)

    def delete_rule(self, rule_id: str) -> None:
        self.delete(f"/me/mailFolders/inbox/messageRules/{quote(rule_id)}")


def connect(interactive: bool = True) -> tuple[GraphClient, dict]:
    profile = config.load_profile()
    return GraphClient(profile, interactive=interactive), profile
