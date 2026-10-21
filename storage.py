"""Where bots, groups, and messages are kept: a local JSON file or a Supabase account."""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode, urlsplit

from core import UserError, _NoRedirect, _error_message, _tls_context


PAGE_SIZE = 1000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
RECENT_IMAGES = 24
UPSERT = {"Prefer": "resolution=merge-duplicates,return=minimal"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def load_config(root: Path) -> dict | None:
    """Supabase settings from the environment or a git-ignored .env file; None means local mode."""
    values: dict[str, str] = {}
    env_file = root / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.strip().partition("=")
            if separator and not name.startswith("#"):
                values[name.strip()] = value.strip().strip("\"'")

    def setting(*names: str) -> str:
        for name in names:
            value = os.environ.get(name) or values.get(name, "")
            if value:
                return value
        return ""

    url = setting("SUPABASE_URL").rstrip("/")
    key = setting("SUPABASE_ANON_KEY", "SUPABASE_PUBLISHABLE_KEY")
    if not url and not key:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or not key:
        raise ValueError("Set SUPABASE_URL to your https://<project>.supabase.co URL and SUPABASE_ANON_KEY to its publishable key.")
    if key.startswith("sb_secret_") or '"service_role"' in _jwt_payload(key):
        raise ValueError("SUPABASE_ANON_KEY is a secret service key. Use the publishable (anon) key; the secret key bypasses every security rule.")
    return {"url": url, "key": key}


def _jwt_payload(token: str) -> str:
    try:
        part = token.split(".")[1]
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8")
    except (IndexError, ValueError, UnicodeDecodeError):
        return ""


class SupabaseError(UserError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _supabase_message(raw: bytes) -> str:
    # Auth, PostgREST, and Storage each name their error text differently.
    try:
        detail = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "Request failed"
    if isinstance(detail, dict):
        for name in ("msg", "error_description", "message"):
            if isinstance(detail.get(name), str) and detail[name]:
                return detail[name]
    return _error_message(raw)


def _http(method: str, url: str, headers: dict[str, str], data: bytes | None = None, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_tls_context()))
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise SupabaseError(error.code, _supabase_message(error.read(65536))[:300]) from None
    except (urllib.error.URLError, TimeoutError):
        raise SupabaseError(0, "Could not reach Supabase. Check your internet connection.") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise SupabaseError(0, "The Supabase response is too large.")
    return body


class SupabaseAuth:
    """Email and password sign-in through Supabase Auth's REST API."""

    def __init__(self, config: dict):
        self.config = config

    def _token(self, grant: str, body: dict) -> dict:
        raw = _http("POST", f"{self.config['url']}/auth/v1/token?grant_type={grant}",
                    {"apikey": self.config["key"], "Content-Type": "application/json"}, json.dumps(body).encode("utf-8"))
        try:
            data = json.loads(raw)
            return {
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "expires_at": time.time() + float(data.get("expires_in", 3600)),
                "user_id": data["user"]["id"],
                "email": data["user"].get("email", ""),
            }
        except (ValueError, KeyError, TypeError):
            raise SupabaseError(0, "Supabase returned an unexpected sign-in response.") from None

    def sign_in(self, email: str, password: str) -> dict:
        return self._token("password", {"email": email, "password": password})

    def refresh(self, session: dict) -> dict:
        return self._token("refresh_token", {"refresh_token": session["refresh_token"]})

    def sign_out(self, session: dict) -> None:
        try:
            _http("POST", f"{self.config['url']}/auth/v1/logout",
                  {"apikey": self.config["key"], "Authorization": f"Bearer {session['access_token']}"}, b"", timeout=10)
        except SupabaseError:
            pass  # The local session is discarded either way.


class LocalStore:
    """The original single JSON file; every change rewrites it."""

    kind = "local"
    error = ""

    def __init__(self, save: Callable[[], None], images_dir: Path):
        self._save = save
        self.images_dir = images_dir

    def put_bot(self, bot: dict) -> None:
        self._save()

    def delete_bot(self, bot_id: str) -> None:
        self._save()

    def put_group(self, group: dict) -> None:
        self._save()

    def add_message(self, group_id: str, message: dict) -> None:
        self._save()

    def put_grants(self, grants: list[str]) -> None:
        self._save()

    def save_image(self, data: bytes, kind: str) -> str:
        self.images_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{kind}"
        path = self.images_dir / name
        path.write_bytes(data)
        path.chmod(0o600)
        return name

    def load_image(self, name: str) -> bytes:
        return (self.images_dir / name).read_bytes()

    def close(self) -> None:
        pass


class SupabaseStore:
    """One signed-in account. Reads happen at sign-in; writes go through one ordered background queue."""

    kind = "supabase"

    def __init__(self, config: dict, session: dict, grants_file: Path):
        self.config = config
        self.session = session
        self.grants_file = grants_file
        self.error = ""
        self._session_lock = threading.Lock()
        self._writes: queue.Queue = queue.Queue()
        # Images stay in memory until their upload finishes, so the chat can show them at once.
        self._recent: OrderedDict[str, bytes] = OrderedDict()
        self._recent_lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    @property
    def user_id(self) -> str:
        return self.session["user_id"]

    @property
    def email(self) -> str:
        return self.session["email"]

    def _access_token(self) -> str:
        with self._session_lock:
            if self.session["expires_at"] - 60 < time.time():
                self.session = SupabaseAuth(self.config).refresh(self.session)
            return self.session["access_token"]

    def _request(self, method: str, path: str, *, params: dict | None = None, body: object = None,
                 data: bytes | None = None, headers: dict | None = None) -> bytes:
        url = self.config["url"] + path + ("?" + urlencode(params) if params else "")
        all_headers = {"apikey": self.config["key"], "Authorization": f"Bearer {self._access_token()}", **(headers or {})}
        if body is not None:
            all_headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return _http(method, url, all_headers, data)

    def _rows(self, table: str, select: str, order: str) -> list[dict]:
        rows: list[dict] = []
        while True:
            raw = self._request("GET", f"/rest/v1/{table}", params={"select": select, "order": order, "limit": PAGE_SIZE, "offset": len(rows)})
            page = json.loads(raw)
            if not isinstance(page, list):
                raise SupabaseError(0, f"Supabase returned an unexpected {table} response.")
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows

    def load(self) -> dict:
        """The account's data in the same shape as the local settings file."""
        try:
            bots = [{**row["data"], "id": row["id"]} for row in self._rows("bots", "id,data", "created_at.asc,id.asc") if isinstance(row.get("data"), dict)]
            groups = self._rows("groups", "id,name,mode,master_id,quality_criteria,bot_ids,last_workflow", "created_at.asc,id.asc")
            messages = self._rows("messages", "id,group_id,sender,bot_id,text,image,files,created_at", "seq.asc")
        except SupabaseError as error:
            if error.status == 404 or "does not exist" in str(error) or "schema cache" in str(error):
                raise SupabaseError(error.status, "The Supabase tables are missing. Run supabase/schema.sql in the Supabase SQL editor first.") from None
            raise
        by_group: dict[str, list[dict]] = {}
        for message in messages:
            item = {key: value for key, value in message.items() if key != "group_id" and value is not None}
            by_group.setdefault(message["group_id"], []).append(item)
        for group in groups:
            group["messages"] = by_group.get(group["id"], [])
            if group.get("last_workflow") is None:
                group.pop("last_workflow", None)
        return {"bots": bots, "groups": groups, "grants": self._read_grants()}

    def _read_grants(self) -> list:
        try:
            grants = json.loads(self.grants_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return grants if isinstance(grants, list) else []

    def _queue(self, method: str, path: str, **options: object) -> None:
        self._writes.put((method, path, options))

    def put_bot(self, bot: dict) -> None:
        row = {"owner_id": self.user_id, "id": bot["id"], "data": {key: value for key, value in bot.items() if key != "id"}}
        self._queue("POST", "/rest/v1/bots", params={"on_conflict": "owner_id,id"}, body=row, headers=UPSERT)

    def delete_bot(self, bot_id: str) -> None:
        self._queue("DELETE", "/rest/v1/bots", params={"id": f"eq.{bot_id}"})

    def put_group(self, group: dict) -> None:
        row = {"owner_id": self.user_id, "id": group["id"], "name": group["name"], "mode": group.get("mode", "conversation"),
               "master_id": group.get("master_id", ""), "quality_criteria": group.get("quality_criteria", ""),
               "bot_ids": group["bot_ids"], "last_workflow": group.get("last_workflow")}
        self._queue("POST", "/rest/v1/groups", params={"on_conflict": "owner_id,id"}, body=row, headers=UPSERT)

    def delete_group(self, group_id: str) -> None:
        self._queue("DELETE", "/rest/v1/groups", params={"id": f"eq.{group_id}"})

    def add_message(self, group_id: str, message: dict) -> None:
        row = {"owner_id": self.user_id, "group_id": group_id, "id": message["id"], "sender": message["sender"],
               "bot_id": message.get("bot_id", ""), "text": message["text"], "files": message.get("files", [])}
        if message.get("image"):
            row["image"] = message["image"]
        if message.get("created_at"):
            row["created_at"] = message["created_at"]
        self._queue("POST", "/rest/v1/messages", params={"on_conflict": "owner_id,id"}, body=row, headers=UPSERT)

    def put_grants(self, grants: list[str]) -> None:
        # Grants are paths on this computer, so they stay local even when signed in.
        self.grants_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.grants_file.with_suffix(".tmp")
        temp.write_text(json.dumps(grants, indent=2) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.grants_file)

    def _image_path(self, name: str) -> str:
        return f"/storage/v1/object/images/{quote(self.user_id)}/{quote(name)}"

    def upload_image(self, name: str, data: bytes) -> None:
        with self._recent_lock:
            self._recent[name] = data
            while len(self._recent) > RECENT_IMAGES:
                self._recent.popitem(last=False)
        self._queue("POST", self._image_path(name), data=data,
                    headers={"Content-Type": IMAGE_TYPES[name.rsplit(".", 1)[1]], "x-upsert": "true"})

    def save_image(self, data: bytes, kind: str) -> str:
        name = f"{uuid.uuid4().hex}.{kind}"
        self.upload_image(name, data)
        return name

    def load_image(self, name: str) -> bytes:
        with self._recent_lock:
            if name in self._recent:
                return self._recent[name]
        try:
            return self._request("GET", self._image_path(name).replace("/object/", "/object/authenticated/", 1))
        except SupabaseError as error:
            raise OSError(str(error)) from None

    def _drain(self) -> None:
        while True:
            item = self._writes.get()
            try:
                if item is None:
                    return
                method, path, options = item
                for attempt in range(4):
                    try:
                        self._request(method, path, **options)
                        self.error = ""
                        break
                    except SupabaseError as error:
                        retryable = error.status in {0, 401, 408, 429} or error.status >= 500
                        if not retryable or attempt == 3:
                            self.error = f"Could not save a change to Supabase: {error}"
                            break
                        time.sleep(2 ** attempt)
            finally:
                self._writes.task_done()

    def flush(self, timeout: float = 15) -> bool:
        deadline = time.monotonic() + timeout
        while self._writes.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self._writes.unfinished_tasks

    def close(self) -> None:
        self.flush()
        self._writes.put(None)
