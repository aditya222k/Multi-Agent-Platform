"""Where bots, groups, and messages are kept: a local SQLite file or a Supabase account."""

from __future__ import annotations

import base64
import json
import os
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from core import UserError, _NoRedirect, _error_message, _tls_context


PAGE_SIZE = 1000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
RECENT_IMAGES = 24
RECENT_RUNS = 30
UPSERT = {"Prefer": "resolution=merge-duplicates,return=minimal"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MESSAGE_COLUMNS = "id,sender,bot_id,text,image,files,created_at"


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


class SQLiteStore:
    """This computer's data in one SQLite file. Each change is one short transaction."""

    kind = "local"
    error = ""

    SCHEMA = """
        create table if not exists meta (key text primary key, value text not null);
        create table if not exists bots (id text primary key, data text not null);
        create table if not exists groups (id text primary key, data text not null);
        create table if not exists messages (seq integer primary key autoincrement, id text not null unique, group_id text not null, data text not null);
        create index if not exists messages_by_group on messages (group_id, seq);
        create table if not exists runs (id text primary key, created_at real not null default 0, data text not null);
        create table if not exists grants (path text primary key);
    """

    def __init__(self, path: Path, images_dir: Path):
        self.path = path
        self.images_dir = images_dir
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            # Owner-only from the start; SQLite gives its WAL files the same permissions.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("pragma journal_mode=wal")
        self._db.execute("pragma busy_timeout=5000")
        self._db.executescript(self.SCHEMA)

    def _write(self, statements: list[tuple[str, tuple]]) -> None:
        with self._lock:
            try:
                self._db.execute("begin immediate")
                try:
                    for sql, args in statements:
                        self._db.execute(sql, args)
                    self._db.execute("commit")
                except BaseException:
                    self._db.execute("rollback")
                    raise
            except sqlite3.Error as error:
                raise OSError(f"Local database write failed: {error}") from None

    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    @staticmethod
    def _bot_row(bot: dict) -> tuple:
        return bot["id"], json.dumps({key: value for key, value in bot.items() if key != "id"}, ensure_ascii=False)

    @staticmethod
    def _group_row(group: dict) -> tuple:
        return group["id"], json.dumps({key: value for key, value in group.items() if key not in {"messages", "message_count"}}, ensure_ascii=False)

    @staticmethod
    def _message_row(group_id: str, message: dict) -> tuple:
        return message["id"], group_id, json.dumps(message, ensure_ascii=False)

    def get_meta(self, key: str) -> str | None:
        rows = self._read("select value from meta where key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._write([("insert into meta (key, value) values (?, ?) on conflict (key) do update set value = excluded.value", (key, value))])

    def has_data(self) -> bool:
        return bool(self._read("select exists (select 1 from bots) or exists (select 1 from messages)")[0][0])

    def is_empty(self) -> bool:
        return not self._read("select exists (select 1 from bots) or exists (select 1 from groups) or exists (select 1 from messages)")[0][0]

    def load(self, window: int) -> dict:
        """Bots, groups with their latest messages and totals, grants, and recent runs."""
        bots = [{**json.loads(data), "id": bot_id} for bot_id, data in self._read("select id, data from bots order by rowid")]
        groups = []
        for group_id, data in self._read("select id, data from groups order by rowid"):
            recent = self._read("select data from messages where group_id = ? order by seq desc limit ?", (group_id, window))
            count = self._read("select count(*) from messages where group_id = ?", (group_id,))[0][0]
            groups.append({**json.loads(data), "id": group_id, "messages": [json.loads(row[0]) for row in reversed(recent)], "message_count": count})
        grants = [row[0] for row in self._read("select path from grants order by rowid")]
        runs = [json.loads(row[0]) for row in reversed(self._read("select data from runs order by created_at desc limit ?", (RECENT_RUNS,)))]
        return {"bots": bots, "groups": groups, "grants": grants, "runs": runs}

    def load_messages(self, group_id: str, before_id: str, limit: int) -> tuple[list[dict], bool]:
        """Up to limit messages older than before_id, oldest first, and whether even older ones exist."""
        seq = self._read("select seq from messages where id = ? and group_id = ?", (before_id, group_id))
        if not seq:
            return [], False
        rows = self._read("select data from messages where group_id = ? and seq < ? order by seq desc limit ?", (group_id, seq[0][0], limit + 1))
        return [json.loads(row[0]) for row in reversed(rows[:limit])], len(rows) > limit

    def all_messages(self, group_id: str) -> list[dict]:
        return [json.loads(row[0]) for row in self._read("select data from messages where group_id = ? order by seq", (group_id,))]

    def put_bot(self, bot: dict) -> None:
        self._write([("insert into bots (id, data) values (?, ?) on conflict (id) do update set data = excluded.data", self._bot_row(bot))])

    def delete_bot(self, bot_id: str) -> None:
        self._write([("delete from bots where id = ?", (bot_id,))])

    def put_group(self, group: dict) -> None:
        self._write([("insert into groups (id, data) values (?, ?) on conflict (id) do update set data = excluded.data", self._group_row(group))])

    def delete_group(self, group_id: str) -> None:
        self._write([("delete from messages where group_id = ?", (group_id,)), ("delete from groups where id = ?", (group_id,))])

    def add_message(self, group_id: str, message: dict) -> None:
        self._write([("insert or ignore into messages (id, group_id, data) values (?, ?, ?)", self._message_row(group_id, message))])

    def put_grants(self, grants: list[str]) -> None:
        self._write([("delete from grants", ())] + [("insert or ignore into grants (path) values (?)", (path,)) for path in grants])

    def put_run(self, run: dict) -> None:
        self._write([("insert into runs (id, created_at, data) values (?, ?, ?) on conflict (id) do update set data = excluded.data",
                      (run["id"], float(run.get("created_at") or 0), json.dumps(run, ensure_ascii=False)))])

    def save_all(self, bots: list[dict], groups: list[dict], grants: list[str], runs: list[dict]) -> None:
        """Write a whole workspace in one transaction, removing bots and groups it no longer has."""
        statements: list[tuple[str, tuple]] = []
        bot_ids, group_ids = [bot["id"] for bot in bots], [group["id"] for group in groups]
        statements.append((f"delete from bots where id not in ({','.join('?' * len(bot_ids))})", tuple(bot_ids)))
        statements.append((f"delete from messages where group_id not in ({','.join('?' * len(group_ids))})", tuple(group_ids)))
        statements.append((f"delete from groups where id not in ({','.join('?' * len(group_ids))})", tuple(group_ids)))
        statements += [("insert into bots (id, data) values (?, ?) on conflict (id) do update set data = excluded.data", self._bot_row(bot)) for bot in bots]
        for group in groups:
            statements.append(("insert into groups (id, data) values (?, ?) on conflict (id) do update set data = excluded.data", self._group_row(group)))
            statements += [("insert or ignore into messages (id, group_id, data) values (?, ?, ?)", self._message_row(group["id"], message)) for message in group["messages"]]
        statements.append(("delete from grants", ()))
        statements += [("insert or ignore into grants (path) values (?)", (path,)) for path in grants]
        statements += [("insert into runs (id, created_at, data) values (?, ?, ?) on conflict (id) do update set data = excluded.data",
                        (run["id"], float(run.get("created_at") or 0), json.dumps(run, ensure_ascii=False))) for run in runs]
        self._write(statements)

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
        with self._lock:
            self._db.close()


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

    @staticmethod
    def _clean_message(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in {"group_id", "seq"} and value is not None}

    def _messages(self, params: dict) -> list[dict]:
        rows = json.loads(self._request("GET", "/rest/v1/messages", params=params))
        if not isinstance(rows, list):
            raise SupabaseError(0, "Supabase returned an unexpected messages response.")
        return rows

    def load(self, window: int) -> dict:
        """Bots, groups with only their latest messages and totals, grants, and recent runs."""
        try:
            bots = [{**row["data"], "id": row["id"]} for row in self._rows("bots", "id,data", "created_at.asc,id.asc") if isinstance(row.get("data"), dict)]
            groups = self._rows("groups", "id,name,mode,master_id,quality_criteria,bot_ids,last_workflow", "created_at.asc,id.asc")
            counts = self._message_counts()
            for group in groups:
                # One extra row tells whether older messages exist even without the counts view.
                recent = self._messages({"select": MESSAGE_COLUMNS, "group_id": f"eq.{group['id']}", "order": "seq.desc", "limit": window + 1})
                group["messages"] = [self._clean_message(row) for row in reversed(recent[:window])]
                group["message_count"] = counts.get(group["id"], len(recent))
                if group.get("last_workflow") is None:
                    group.pop("last_workflow", None)
        except SupabaseError as error:
            if error.status == 404 or "does not exist" in str(error) or "schema cache" in str(error):
                raise SupabaseError(error.status, "The Supabase tables are missing. Run supabase/schema.sql in the Supabase SQL editor first.") from None
            raise
        return {"bots": bots, "groups": groups, "grants": self._read_grants(), "runs": self._recent_runs()}

    def _message_counts(self) -> dict[str, int]:
        try:
            rows = json.loads(self._request("GET", "/rest/v1/message_counts", params={"select": "group_id,message_count"}))
        except SupabaseError as error:
            self.error = f"Message totals are unavailable: run supabase/schema.sql again in the Supabase SQL editor. ({error})"
            return {}
        return {row["group_id"]: int(row["message_count"]) for row in rows if isinstance(row, dict) and "group_id" in row}

    def load_messages(self, group_id: str, before_id: str, limit: int) -> tuple[list[dict], bool]:
        """Up to limit messages older than before_id, oldest first, and whether even older ones exist."""
        try:
            anchor = self._messages({"select": "seq", "id": f"eq.{before_id}", "group_id": f"eq.{group_id}", "limit": 1})
            if not anchor:
                return [], False
            rows = self._messages({"select": MESSAGE_COLUMNS, "group_id": f"eq.{group_id}", "seq": f"lt.{anchor[0]['seq']}", "order": "seq.desc", "limit": limit + 1})
        except SupabaseError as error:
            raise OSError(str(error)) from None
        return [self._clean_message(row) for row in reversed(rows[:limit])], len(rows) > limit

    def _recent_runs(self) -> list[dict]:
        try:
            raw = self._request("GET", "/rest/v1/runs", params={"select": "data", "order": "updated_at.desc", "limit": RECENT_RUNS, "offset": 0})
        except SupabaseError as error:
            # Accounts created before run history existed can still sign in.
            self.error = f"Run history is not saved yet: run supabase/schema.sql again in the Supabase SQL editor. ({error})"
            return []
        runs = [row["data"] for row in json.loads(raw) if isinstance(row, dict) and isinstance(row.get("data"), dict)]
        return sorted(runs, key=lambda run: run.get("created_at") or 0)

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

    def put_run(self, run: dict) -> None:
        row = {"owner_id": self.user_id, "id": run["id"], "group_id": run["group_id"], "data": run,
               "updated_at": datetime.now(timezone.utc).isoformat()}
        self._queue("POST", "/rest/v1/runs", params={"on_conflict": "owner_id,id"}, body=row, headers=UPSERT)

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
