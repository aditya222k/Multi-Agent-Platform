"""Where bots, groups, and messages are kept: a local SQLite file or a Supabase account."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode, urlsplit

from core import UserError, _NoRedirect, _error_message, _tls_context
from documents import MEDIA_TYPES as DOCUMENT_TYPES


PAGE_SIZE = 1000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
RECENT_IMAGES = 24
RECENT_RUNS = 30
UPSERT = {"Prefer": "resolution=merge-duplicates,return=minimal"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MESSAGE_COLUMNS = "id,sender,bot_id,text,image,files,created_at"
DOCUMENT_COLUMNS = "id,group_id,name,kind,size,pages,text,warnings,file,created_at"
# The column that identifies a row, for tables where it is not "id".
ROW_KEYS = {"bot_keys": "bot_id", "key_protection": "owner_id"}
# Outbox sync: send every SYNC_INTERVAL seconds, or at once when BATCH_SIZE changes are waiting.
SYNC_INTERVAL = 10
BATCH_SIZE = 100
MAX_BACKOFF = 60


def retryable(status: int) -> bool:
    """Errors worth retrying later: no connection, timeouts, rate limits, expired sign-in, server trouble."""
    return status in {0, 401, 408, 429} or status >= 500


def _open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        # Owner-only from the start; SQLite gives its WAL files the same permissions.
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    connection.execute("pragma journal_mode=wal")
    connection.execute("pragma busy_timeout=5000")
    return connection


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
        create table if not exists bot_keys (bot_id text primary key, token text not null, hint text not null, saved_at text not null);
        create table if not exists documents (id text primary key, group_id text not null, data text not null);
        create index if not exists documents_by_group on documents (group_id);
    """

    def __init__(self, path: Path, images_dir: Path):
        self.path = path
        self.images_dir = images_dir
        self.documents_dir = images_dir.with_name("documents")
        self._lock = threading.Lock()
        self._db = _open_database(path)
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
        return group["id"], json.dumps({key: value for key, value in group.items() if key not in {"messages", "message_count", "documents"}}, ensure_ascii=False)

    @staticmethod
    def _message_row(group_id: str, message: dict) -> tuple:
        return message["id"], group_id, json.dumps(message, ensure_ascii=False)

    def get_meta(self, key: str) -> str | None:
        rows = self._read("select value from meta where key = ?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._write([("insert into meta (key, value) values (?, ?) on conflict (key) do update set value = excluded.value", (key, value))])

    def put_protection(self, protection: dict) -> None:
        self.set_meta("key_protection", json.dumps(protection))

    def delete_protection(self) -> None:
        self._write([("delete from meta where key = 'key_protection'", ())])

    def put_bot_key(self, bot_id: str, record: dict) -> None:
        self._write([("insert into bot_keys (bot_id, token, hint, saved_at) values (?, ?, ?, ?) on conflict (bot_id) do update set "
                      "token = excluded.token, hint = excluded.hint, saved_at = excluded.saved_at",
                      (bot_id, record["token"], record["hint"], record["saved_at"]))])

    def delete_bot_key(self, bot_id: str) -> None:
        self._write([("delete from bot_keys where bot_id = ?", (bot_id,))])

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
            documents = [json.loads(row[0]) for row in self._read("select data from documents where group_id = ? order by rowid", (group_id,))]
            groups.append({**json.loads(data), "id": group_id, "messages": [json.loads(row[0]) for row in reversed(recent)], "message_count": count,
                           "documents": documents})
        grants = [row[0] for row in self._read("select path from grants order by rowid")]
        runs = [json.loads(row[0]) for row in reversed(self._read("select data from runs order by created_at desc limit ?", (RECENT_RUNS,)))]
        protection = self.get_meta("key_protection")
        bot_keys = {bot_id: {"token": token, "hint": hint, "saved_at": saved_at}
                    for bot_id, token, hint, saved_at in self._read("select bot_id, token, hint, saved_at from bot_keys")}
        return {"bots": bots, "groups": groups, "grants": grants, "runs": runs,
                "key_protection": json.loads(protection) if protection else None, "bot_keys": bot_keys}

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
        self._write([("delete from bot_keys where bot_id = ?", (bot_id,)), ("delete from bots where id = ?", (bot_id,))])

    def put_group(self, group: dict) -> None:
        self._write([("insert into groups (id, data) values (?, ?) on conflict (id) do update set data = excluded.data", self._group_row(group))])

    def delete_group(self, group_id: str) -> None:
        self._write([("delete from messages where group_id = ?", (group_id,)), ("delete from documents where group_id = ?", (group_id,)),
                     ("delete from groups where id = ?", (group_id,))])

    def group_images(self, group_id: str) -> set[str]:
        return {name for row in self._read("select data from messages where group_id = ?", (group_id,))
                if isinstance(name := json.loads(row[0]).get("image"), str) and name}

    def clear_messages(self, group_id: str, images: set[str]) -> None:
        """Delete every message in a group and the image files they showed."""
        self._write([("delete from messages where group_id = ?", (group_id,))])
        self._remove_files(self.images_dir, images)

    def put_document(self, group_id: str, document: dict, data: bytes) -> None:
        """Save a document's original file under its random name, then its record with the extracted text."""
        self.documents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.documents_dir / document["file"]
        path.write_bytes(data)
        path.chmod(0o600)
        self._write([("insert into documents (id, group_id, data) values (?, ?, ?) on conflict (id) do update set data = excluded.data",
                      (document["id"], group_id, json.dumps(document, ensure_ascii=False)))])

    @staticmethod
    def _remove_files(folder: Path, names: set[str]) -> None:
        for name in names:
            if Path(name).name == name and not name.startswith("."):  # A bare file name, never a path.
                try:
                    (folder / name).unlink()
                except OSError:
                    pass  # Already gone.

    def delete_document(self, document: dict) -> None:
        self._write([("delete from documents where id = ?", (document["id"],))])
        self._remove_files(self.documents_dir, {document["file"]})

    def clear_documents(self, group_id: str, files: set[str]) -> None:
        self._write([("delete from documents where group_id = ?", (group_id,))])
        self._remove_files(self.documents_dir, files)

    def load_document_file(self, name: str) -> bytes:
        return (self.documents_dir / name).read_bytes()

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
        statements.append((f"delete from bot_keys where bot_id not in ({','.join('?' * len(bot_ids))})", tuple(bot_ids)))
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


class Outbox:
    """Changes waiting to reach Supabase, kept on disk so an outage, crash, or restart loses none of them."""

    SCHEMA = """
        create table if not exists outbox (
            seq integer primary key autoincrement,
            owner_id text not null,
            kind text not null,
            target text not null,
            row_key text not null,
            payload blob not null,
            content_type text not null default '',
            sending integer not null default 0,
            failed integer not null default 0,
            error text not null default ''
        );
        create index if not exists outbox_pending on outbox (owner_id, failed, seq);
    """

    def __init__(self, path: Path, owner_id: str):
        self.owner_id = owner_id
        self._lock = threading.Lock()
        self._db = _open_database(path)
        self._db.executescript(self.SCHEMA)
        # Anything still marked as sending when the app stopped never finished, so it goes again.
        self._db.execute("update outbox set sending = 0 where owner_id = ?", (owner_id,))

    def add(self, kind: str, target: str, key: str, payload: bytes, content_type: str = "", merge: bool = False) -> None:
        """Append a change. With merge, a newer version of a still-waiting row replaces the older one."""
        with self._lock:
            self._db.execute("begin immediate")
            try:
                if merge:
                    latest = self._db.execute(
                        "select seq, kind, sending, failed from outbox where owner_id = ? and target = ? and row_key = ? order by seq desc limit 1",
                        (self.owner_id, target, key)).fetchone()
                    # Only the newest entry for a row may absorb the change, so a delete in between keeps its place.
                    if latest and latest[1] == kind and not latest[2] and not latest[3]:
                        self._db.execute("update outbox set payload = ? where seq = ?", (payload, latest[0]))
                        self._db.execute("commit")
                        return
                self._db.execute("insert into outbox (owner_id, kind, target, row_key, payload, content_type) values (?, ?, ?, ?, ?, ?)",
                                 (self.owner_id, kind, target, key, payload, content_type))
                self._db.execute("commit")
            except BaseException:
                self._db.execute("rollback")
                raise

    def take(self, limit: int) -> list[dict]:
        """The oldest waiting changes, marked as sending so later edits to them become new entries."""
        with self._lock:
            rows = self._db.execute(
                "select seq, kind, target, row_key, payload, content_type from outbox where owner_id = ? and failed = 0 and sending = 0 order by seq limit ?",
                (self.owner_id, limit)).fetchall()
            if rows:
                self._db.execute(f"update outbox set sending = 1 where seq in ({','.join('?' * len(rows))})", tuple(row[0] for row in rows))
        return [dict(zip(("seq", "kind", "target", "key", "payload", "content_type"), row)) for row in rows]

    def finish(self, seqs: list[int]) -> None:
        if seqs:
            with self._lock:
                self._db.execute(f"delete from outbox where seq in ({','.join('?' * len(seqs))})", tuple(seqs))

    def release(self, seqs: list[int]) -> None:
        if seqs:
            with self._lock:
                self._db.execute(f"update outbox set sending = 0 where seq in ({','.join('?' * len(seqs))})", tuple(seqs))

    def park(self, seq: int, error: str) -> None:
        """Set aside a change Supabase rejected, so it stops blocking the ones behind it."""
        with self._lock:
            self._db.execute("update outbox set sending = 0, failed = 1, error = ? where seq = ?", (error[:500], seq))

    def counts(self) -> tuple[int, int, str]:
        """Waiting changes, rejected changes, and the first rejection's reason."""
        with self._lock:
            waiting, failed = self._db.execute(
                "select coalesce(sum(failed = 0), 0), coalesce(sum(failed = 1), 0) from outbox where owner_id = ?", (self.owner_id,)).fetchone()
            first = self._db.execute("select error from outbox where owner_id = ? and failed = 1 order by seq limit 1", (self.owner_id,)).fetchone()
        return waiting, failed, first[0] if first else ""

    def upload(self, target: str) -> bytes | None:
        """A file still waiting to upload, so it can be shown before it reaches Supabase."""
        with self._lock:
            row = self._db.execute("select payload from outbox where owner_id = ? and kind in ('image', 'file') and target = ? order by seq desc limit 1",
                                   (self.owner_id, target)).fetchone()
        return bytes(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


class _Unreachable(Exception):
    """Supabase did not respond usefully; the changes stay in the outbox for a later try."""


class SupabaseStore:
    """One signed-in account. Reads happen at sign-in; writes go through a saved outbox, sent in batches."""

    kind = "supabase"

    def __init__(self, config: dict, session: dict, grants_file: Path, outbox_file: Path | None = None,
                 on_status: Callable[[dict], None] | None = None):
        self.config = config
        self.session = session
        self.grants_file = grants_file
        self.notice = ""
        self.on_status = on_status or (lambda status: None)
        self._session_lock = threading.Lock()
        self.outbox = Outbox(outbox_file or grants_file.with_name("outbox.db"), session["user_id"])
        # Images stay in memory briefly, so the chat can show them before the upload finishes.
        self._recent: OrderedDict[str, bytes] = OrderedDict()
        self._recent_lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._offline = ""
        self._backoff = 0.0
        self._attempts = 0
        self._last_status: dict | None = None
        self._worker = threading.Thread(target=self._sync_loop, daemon=True)
        self._worker.start()

    @property
    def error(self) -> str:
        """A problem the person should know about: rejected changes, or a schema that needs updating."""
        _, failed, reason = self.outbox.counts()
        if failed:
            return f"{failed} change{'s' if failed != 1 else ''} could not be saved to Supabase: {reason}"
        return self.notice

    def sync_status(self) -> dict:
        waiting, failed, _ = self.outbox.counts()
        state = "saved" if not waiting else "offline" if self._offline else "saving"
        return {"state": state, "pending": waiting, "failed": failed, "reason": self._offline if waiting else ""}

    def _report(self) -> None:
        status = self.sync_status()
        if status != self._last_status:
            self._last_status = status
            self.on_status(status)

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
            documents = self._documents()
            for group in groups:
                group["documents"] = documents.get(group["id"], [])
        except SupabaseError as error:
            if error.status == 404 or "does not exist" in str(error) or "schema cache" in str(error):
                raise SupabaseError(error.status, "The Supabase tables are missing. Run supabase/schema.sql in the Supabase SQL editor first.") from None
            raise
        protection, bot_keys = self._saved_keys()
        return {"bots": bots, "groups": groups, "grants": self._read_grants(), "runs": self._recent_runs(),
                "key_protection": protection, "bot_keys": bot_keys}

    def _saved_keys(self) -> tuple[dict | None, dict]:
        try:
            protection = json.loads(self._request("GET", "/rest/v1/key_protection", params={"select": "data", "limit": 1}))
            rows = json.loads(self._request("GET", "/rest/v1/bot_keys", params={"select": "bot_id,token,hint,saved_at"}))
        except SupabaseError as error:
            # Accounts created before saved keys existed can still sign in; keys then last one session.
            self.notice = f"Saved API keys are unavailable: run supabase/schema.sql again in the Supabase SQL editor. ({error})"
            return None, {}
        record = protection[0]["data"] if protection and isinstance(protection[0], dict) else None
        return record, {row["bot_id"]: {"token": row["token"], "hint": row["hint"], "saved_at": row["saved_at"]}
                        for row in rows if isinstance(row, dict) and row.get("token")}

    def _documents(self) -> dict[str, list[dict]]:
        try:
            rows = self._rows("documents", DOCUMENT_COLUMNS, "created_at.asc,id.asc")
        except SupabaseError as error:
            # Accounts created before documents existed can still sign in; attaching documents then fails to sync.
            self.notice = f"Documents are unavailable: run supabase/schema.sql again in the Supabase SQL editor. ({error})"
            return {}
        by_group: dict[str, list[dict]] = {}
        for row in rows:
            if isinstance(row, dict):
                by_group.setdefault(str(row.get("group_id")), []).append(row)
        return by_group

    def _message_counts(self) -> dict[str, int]:
        try:
            rows = json.loads(self._request("GET", "/rest/v1/message_counts", params={"select": "group_id,message_count"}))
        except SupabaseError as error:
            self.notice = f"Message totals are unavailable: run supabase/schema.sql again in the Supabase SQL editor. ({error})"
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
            self.notice = f"Run history is not saved yet: run supabase/schema.sql again in the Supabase SQL editor. ({error})"
            return []
        runs = [row["data"] for row in json.loads(raw) if isinstance(row, dict) and isinstance(row.get("data"), dict)]
        return sorted(runs, key=lambda run: run.get("created_at") or 0)

    def _read_grants(self) -> list:
        try:
            grants = json.loads(self.grants_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return grants if isinstance(grants, list) else []

    def _upsert(self, table: str, row: dict) -> None:
        self.outbox.add("upsert", table, row[ROW_KEYS.get(table, "id")], json.dumps(row, ensure_ascii=False).encode("utf-8"), merge=True)
        self._changed()

    def _changed(self) -> None:
        self._report()
        if self.outbox.counts()[0] >= BATCH_SIZE:
            self._wake.set()

    def put_bot(self, bot: dict) -> None:
        self._upsert("bots", {"owner_id": self.user_id, "id": bot["id"], "data": {key: value for key, value in bot.items() if key != "id"}})

    def delete_bot(self, bot_id: str) -> None:
        self.outbox.add("delete", "bots", bot_id, b"")
        self._changed()

    def put_protection(self, protection: dict) -> None:
        self._upsert("key_protection", {"owner_id": self.user_id, "data": protection, "updated_at": datetime.now(timezone.utc).isoformat()})

    def delete_protection(self) -> None:
        self.outbox.add("delete", "key_protection", self.user_id, b"")
        self._changed()

    def put_bot_key(self, bot_id: str, record: dict) -> None:
        # Only the encrypted token, the last four characters, and the date ever leave this computer.
        self._upsert("bot_keys", {"owner_id": self.user_id, "bot_id": bot_id, **record})

    def delete_bot_key(self, bot_id: str) -> None:
        self.outbox.add("delete", "bot_keys", bot_id, b"")
        self._changed()

    def put_group(self, group: dict) -> None:
        self._upsert("groups", {"owner_id": self.user_id, "id": group["id"], "name": group["name"], "mode": group.get("mode", "conversation"),
                                "master_id": group.get("master_id", ""), "quality_criteria": group.get("quality_criteria", ""),
                                "bot_ids": group["bot_ids"], "last_workflow": group.get("last_workflow")})

    def delete_group(self, group_id: str) -> None:
        self.outbox.add("delete", "groups", group_id, b"")
        self._changed()

    def group_images(self, group_id: str) -> set[str]:
        """Image names in a group's saved messages. Network work, so callers run it outside the state lock."""
        names: set[str] = set()
        offset = 0
        try:
            while True:
                rows = self._messages({"select": "image", "group_id": f"eq.{group_id}", "order": "seq.asc", "limit": PAGE_SIZE, "offset": offset})
                names.update(row["image"] for row in rows if isinstance(row, dict) and row.get("image"))
                if len(rows) < PAGE_SIZE:
                    return names
                offset += len(rows)
        except SupabaseError:
            return names  # Offline: images still in memory are cleared; older ones stay in storage.

    def clear_messages(self, group_id: str, images: set[str]) -> None:
        """Queue deleting a group's messages and images, after any of its changes still waiting to sync."""
        # Queued in order, so a message still waiting to upload cannot reappear after the clear.
        self.outbox.add("delete_where", "messages", group_id, json.dumps({"group_id": f"eq.{group_id}"}).encode("utf-8"))
        for name in sorted(images):
            self.outbox.add("delete_image", self._image_path(name), name, b"")
            with self._recent_lock:
                self._recent.pop(name, None)
        self._changed()

    def _document_path(self, name: str) -> str:
        return f"/storage/v1/object/documents/{quote(self.user_id)}/{quote(name)}"

    def put_document(self, group_id: str, document: dict, data: bytes) -> None:
        """Queue the original file, then the record with its extracted text."""
        if data:  # Imported documents whose original file went missing keep only their text.
            self.outbox.add("file", self._document_path(document["file"]), document["file"], data, DOCUMENT_TYPES[document["kind"]])
        self._upsert("documents", {"owner_id": self.user_id, "group_id": group_id, **document})

    def delete_document(self, document: dict) -> None:
        self.outbox.add("delete", "documents", document["id"], b"")
        self.outbox.add("delete_file", self._document_path(document["file"]), document["file"], b"")
        self._changed()

    def clear_documents(self, group_id: str, files: set[str]) -> None:
        """Queue deleting a group's documents, after any of its changes still waiting to sync."""
        self.outbox.add("delete_where", "documents", group_id, json.dumps({"group_id": f"eq.{group_id}"}).encode("utf-8"))
        for name in sorted(files):
            self.outbox.add("delete_file", self._document_path(name), name, b"")
        self._changed()

    def load_document_file(self, name: str) -> bytes:
        waiting = self.outbox.upload(self._document_path(name))
        if waiting is not None:
            return waiting
        try:
            return self._request("GET", self._document_path(name).replace("/object/", "/object/authenticated/", 1))
        except SupabaseError as error:
            raise OSError(str(error)) from None

    def add_message(self, group_id: str, message: dict) -> None:
        # Every row carries the same columns, so a batch can go in one request.
        self._upsert("messages", {"owner_id": self.user_id, "group_id": group_id, "id": message["id"], "sender": message["sender"],
                                  "bot_id": message.get("bot_id", ""), "text": message["text"], "files": message.get("files", []),
                                  "image": message.get("image") or None,
                                  "created_at": message.get("created_at") or datetime.now(timezone.utc).isoformat()})

    def put_run(self, run: dict) -> None:
        self._upsert("runs", {"owner_id": self.user_id, "id": run["id"], "group_id": run["group_id"], "data": run,
                              "updated_at": datetime.now(timezone.utc).isoformat()})

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
        self.outbox.add("image", self._image_path(name), name, data, IMAGE_TYPES[name.rsplit(".", 1)[1]])
        self._changed()

    def save_image(self, data: bytes, kind: str) -> str:
        name = f"{uuid.uuid4().hex}.{kind}"
        self.upload_image(name, data)
        return name

    def load_image(self, name: str) -> bytes:
        with self._recent_lock:
            if name in self._recent:
                return self._recent[name]
        # An image still waiting to upload is shown from the outbox, even while offline.
        waiting = self.outbox.upload(self._image_path(name))
        if waiting is not None:
            return waiting
        try:
            return self._request("GET", self._image_path(name).replace("/object/", "/object/authenticated/", 1))
        except SupabaseError as error:
            raise OSError(str(error)) from None

    def _sync_loop(self) -> None:
        while not self._stopped:
            self._wake.wait(self._backoff or SYNC_INTERVAL)
            self._wake.clear()
            if self._stopped:
                return
            try:
                self._send_waiting()
                self._offline, self._backoff = "", 0.0
            except Exception as error:
                if self._stopped:
                    return  # The outbox closed while a send was finishing.
                # Keep everything and try again later, waiting longer each time up to a minute.
                self._offline = str(error) if isinstance(error, _Unreachable) else f"Sync paused: {error}"
                self._backoff = min(MAX_BACKOFF, max(2.0, self._backoff * 2))
            self._attempts += 1
            self._report()

    @staticmethod
    def _batches(entries: list[dict]) -> list[list[dict]]:
        """Consecutive upserts to one table travel together; deletes and images go one at a time."""
        batches: list[list[dict]] = []
        for entry in entries:
            previous = batches[-1][-1] if batches else None
            if previous and entry["kind"] == "upsert" and previous["kind"] == "upsert" and previous["target"] == entry["target"]:
                batches[-1].append(entry)
            else:
                batches.append([entry])
        return batches

    def _send_waiting(self) -> None:
        while not self._stopped:
            entries = self.outbox.take(BATCH_SIZE)
            if not entries:
                return
            unsent = [entry["seq"] for entry in entries]
            try:
                for batch in self._batches(entries):
                    self._send(batch)
                    unsent = unsent[len(batch):]
            finally:
                self.outbox.release(unsent)
            self._report()

    def _send(self, batch: list[dict]) -> None:
        try:
            self._deliver(batch)
            self.outbox.finish([entry["seq"] for entry in batch])
            return
        except SupabaseError as error:
            if retryable(error.status):
                raise _Unreachable(str(error)) from None
            if len(batch) == 1:
                self.outbox.park(batch[0]["seq"], str(error))
                return
        # Supabase refused the batch: send each change alone to find the one it rejects.
        for entry in batch:
            self._send([entry])

    def _deliver(self, batch: list[dict]) -> None:
        try:
            self._access_token()
        except SupabaseError as error:
            raise _Unreachable(f"Sign-in could not be refreshed: {error}") from None
        first = batch[0]
        if first["kind"] == "upsert":
            rows = {}
            for entry in batch:
                row = json.loads(entry["payload"])
                rows[entry["key"]] = row
            key_column = ROW_KEYS.get(first["target"], "id")
            conflict = "owner_id" if key_column == "owner_id" else f"owner_id,{key_column}"
            self._request("POST", f"/rest/v1/{first['target']}", params={"on_conflict": conflict}, body=list(rows.values()), headers=UPSERT)
        elif first["kind"] == "delete":
            self._request("DELETE", f"/rest/v1/{first['target']}", params={ROW_KEYS.get(first["target"], "id"): f"eq.{first['key']}"})
        elif first["kind"] == "delete_where":
            self._request("DELETE", f"/rest/v1/{first['target']}", params=json.loads(first["payload"]))
        elif first["kind"] in {"delete_image", "delete_file"}:
            try:
                self._request("DELETE", first["target"])
            except SupabaseError as error:
                if error.status not in {400, 404}:
                    raise  # A file that is already gone needs no retry.
        else:
            self._request("POST", first["target"], data=bytes(first["payload"]), headers={"Content-Type": first["content_type"], "x-upsert": "true"})

    def flush(self, timeout: float = 15) -> bool:
        """Try to send everything now. False means some changes are still waiting in the outbox."""
        attempts = self._attempts
        self._wake.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.outbox.counts()[0]:
                return True
            if self._attempts > attempts and self._offline:
                return False  # Supabase is not answering; the worker keeps retrying on its own schedule.
            time.sleep(0.05)
        return not self.outbox.counts()[0]

    def close(self, timeout: float = 15) -> int:
        """Send what can be sent, then stop. Returns how many changes stay saved for next time."""
        self.flush(timeout)
        waiting = self.outbox.counts()[0]
        self._stopped = True
        self._wake.set()
        self._worker.join(5)
        self.outbox.close()
        return waiting
