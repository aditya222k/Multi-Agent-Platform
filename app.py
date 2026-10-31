"""Local-only server for configurable bots and named group chats."""

from __future__ import annotations

import argparse
import copy
import json
import queue
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from core import UserError, blank_bot, read_allowed_files, run_group_conversation, validate_bot, validate_desktop_file, validate_runnable
from documents import DOCUMENT_FILE, MAX_DOCUMENTS, MAX_UPLOAD_BYTES, clean_document, fit_documents, read_document, summary
from storage import IMAGE_TYPES, SQLiteStore, SupabaseAuth, SupabaseError, SupabaseStore, load_config
from vault import DeviceKeys, Vault, VaultLocked
from workflow import WorkflowBlocked, run_task_workflow


ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / ".local" / "settings.json"
GRANTS_FILE = ROOT / ".local" / "grants.json"
IMAGES_DIR = ROOT / ".local" / "images"
IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
MAX_REQUEST_BYTES = 100 * 1024
MAX_ACTIVE_RUNS = 4
# Messages per group kept in memory. Older ones stay in storage and load a page at a time.
HISTORY_WINDOW = 200
ACTIVE = {"queued", "running"}


class EventHub:
    """Recent changes, numbered, for browsers listening on /api/events."""

    def __init__(self, keep: int = 500) -> None:
        self.condition = threading.Condition()
        self.events: deque[tuple[int, str, str]] = deque(maxlen=keep)
        self.next_id = 1
        self.closed = False

    def publish(self, kind: str, data: dict) -> None:
        # Serialised now, so later changes to the same objects cannot alter a sent event.
        payload = json.dumps(data, ensure_ascii=False)
        with self.condition:
            self.events.append((self.next_id, kind, payload))
            self.next_id += 1
            self.condition.notify_all()

    def latest(self) -> int:
        with self.condition:
            return self.next_id - 1

    def since(self, last_id: int) -> list[tuple[int, str, str]] | None:
        """Events after last_id, or None when some of them were already dropped."""
        with self.condition:
            if self.events and last_id < self.events[0][0] - 1:
                return None
            return [event for event in self.events if event[0] > last_id]

    def wait(self, last_id: int, timeout: float) -> None:
        with self.condition:
            self.condition.wait_for(lambda: self.closed or self.next_id - 1 > last_id, timeout)

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class RunManager:
    """A fixed pool of workers; runs beyond the pool wait their turn as "queued"."""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self.jobs: queue.Queue = queue.Queue()
        self._started = False
        self._lock = threading.Lock()

    def submit(self, job) -> None:
        with self._lock:
            if not self._started:
                # Workers start on first use, so creating a State costs no threads.
                for _ in range(self.workers):
                    threading.Thread(target=self._work, daemon=True).start()
                self._started = True
        self.jobs.put(job)

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception:
                pass  # execute_group_run records its own failures on the run.
            finally:
                self.jobs.task_done()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def group_name(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise UserError("Enter a group name of 100 characters or fewer.")
    return value.strip()


def general_group() -> dict:
    return {"id": uuid.uuid4().hex, "name": "General", "bot_ids": [], "messages": [], "message_count": 0, "mode": "conversation", "master_id": "", "quality_criteria": "", "documents": []}


def message_count(group: dict) -> int:
    return max(group.get("message_count", 0), len(group["messages"]))


class Workspace:
    """Bots, groups, and Desktop file grants, validated the same way from every source."""

    def __init__(self) -> None:
        self.bots: list[dict] = []
        self.groups: list[dict] = [general_group()]
        self.grants: list[str] = []

    def apply(self, data: dict) -> None:
        """Validate saved data from the settings file or a Supabase account and load it."""
        if "bots" in data:
            bots = data["bots"]
            if not isinstance(bots, list):
                raise ValueError("Invalid bots")
            self.bots = [validate_bot(item, str(item["id"])) for item in bots if isinstance(item, dict) and isinstance(item.get("id"), str)]
        elif isinstance(data.get("agents"), list):
            self._migrate_agents(data["agents"])
        known_ids = {bot["id"] for bot in self.bots}
        if isinstance(data.get("groups"), list):
            groups = []
            for item in data["groups"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                name = group_name(item.get("name"))
                raw_members = item.get("bot_ids", [])
                members = [bot_id for bot_id in raw_members if isinstance(bot_id, str) and bot_id in known_ids] if isinstance(raw_members, list) else []
                messages = []
                for message in item.get("messages", []) if isinstance(item.get("messages"), list) else []:
                    if isinstance(message, dict) and isinstance(message.get("text"), str) and isinstance(message.get("sender"), str):
                        messages.append({"id": str(message.get("id", uuid.uuid4().hex)), "sender": message["sender"][:80], "bot_id": str(message.get("bot_id", "")), "text": message["text"][:100000], "created_at": str(message.get("created_at", "")), "files": message.get("files", []) if isinstance(message.get("files"), list) else []})
                        if isinstance(message.get("image"), str) and IMAGE_NAME.fullmatch(message["image"]):
                            messages[-1]["image"] = message["image"]
                group = {"id": item["id"], "name": name, "bot_ids": list(dict.fromkeys(members)), "messages": messages}
                raw_documents = item.get("documents") if isinstance(item.get("documents"), list) else []
                group["documents"] = [document for document in map(clean_document, raw_documents) if document is not None]
                saved_count = item.get("message_count")
                group["message_count"] = max(saved_count if type(saved_count) is int else 0, len(messages))
                group["mode"] = item.get("mode") if isinstance(item.get("mode"), str) and item.get("mode") in {"workflow", "conversation"} else "conversation"
                group["master_id"] = item.get("master_id") if item.get("master_id") in members else next(iter(members), "")
                group["quality_criteria"] = item.get("quality_criteria", "")[:4000] if isinstance(item.get("quality_criteria", ""), str) else ""
                last = item.get("last_workflow")
                if isinstance(last, dict) and isinstance(last.get("approved_results"), list):
                    group["last_workflow"] = last
                groups.append(group)
            self.groups = groups
        grants = data.get("grants", [])
        if isinstance(grants, list):
            for path in grants:
                try:
                    resolved = validate_desktop_file(path)
                    if resolved == path and path not in self.grants:
                        self.grants.append(path)
                except UserError:
                    pass

    def _migrate_agents(self, agents: list) -> None:
        endpoints = {
            "openai": ("https://api.openai.com/v1/responses", "responses", "Authorization", "Bearer ", {}),
            "anthropic": ("https://api.anthropic.com/v1/messages", "messages", "x-api-key", "", {"anthropic-version": "2023-06-01"}),
        }
        for item in agents:
            if not isinstance(item, dict):
                continue
            bot = blank_bot(uuid.uuid4().hex)
            bot["name"] = str(item.get("name", "Agent"))
            bot["role"] = str(item.get("instructions", ""))
            bot["model"] = str(item.get("model", ""))
            endpoint, api_format, header, prefix, extra = endpoints.get(item.get("provider"), ("", "chat_completions", "Authorization", "Bearer ", {}))
            bot.update(endpoint=endpoint, api_format=api_format, auth_header=header, auth_prefix=prefix, extra_headers=extra)
            self.bots.append(validate_bot(bot, bot["id"]))
            self.groups[0]["bot_ids"].append(bot["id"])


def read_legacy_settings() -> tuple[Workspace, list] | None:
    """The older settings.json, validated, with its saved runs."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        local = Workspace()
        local.apply(data)
        runs = data.get("runs") if isinstance(data.get("runs"), list) else []
        return local, [run for run in runs if isinstance(run, dict) and isinstance(run.get("id"), str) and isinstance(run.get("group_id"), str)]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def open_local_store() -> SQLiteStore:
    """This computer's database, importing an older settings.json into it once."""
    # Kept beside settings.json, so anything that relocates one relocates both.
    store = SQLiteStore(SETTINGS_FILE.with_name("app.db"), IMAGES_DIR)
    if SETTINGS_FILE.exists() and store.get_meta("settings_json_imported") is None:
        legacy = read_legacy_settings()
        if legacy is None:
            print("settings.json could not be read, so it was not imported. The file is unchanged.")
            return store
        if store.is_empty():
            workspace, runs = legacy
            store.save_all(workspace.bots, workspace.groups, workspace.grants, runs)
        # settings.json is left in place as a backup.
        store.set_meta("settings_json_imported", now())
    return store


class State(Workspace):
    def __init__(self, load: bool = True) -> None:
        super().__init__()
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.keys: dict[str, str] = {}
        self.runs: dict[str, dict] = {}
        self.stop_events: dict[str, threading.Event] = {}
        self.events = EventHub()
        self.runner = RunManager(MAX_ACTIVE_RUNS)
        self.vault = Vault()
        self.store: SQLiteStore | SupabaseStore | None = None
        # Set when Supabase is configured; the account is then required before any data loads.
        self.supabase: dict | None = None
        if load:
            self.load()

    def load(self) -> None:
        store = open_local_store()
        data: dict = {}
        try:
            data = store.load(HISTORY_WINDOW)
            self.apply(data)
        except (ValueError, TypeError, KeyError):
            print("Saved data could not be read; starting with defaults.")
            self.bots, self.groups, self.grants = [], [], []
        self.store = store
        if not self.groups:
            self.groups = [general_group()]
            store.put_group(self.groups[0])
        for run in self.restore_runs(data.get("runs")):
            store.put_run(run)
        self._open_vault("local", data, SETTINGS_FILE.parent)

    def _open_vault(self, owner: str, data: dict, folder: Path, password: str | None = None) -> None:
        """Load this account's saved keys; a device that remembers them unlocks straight away."""
        opened = self.vault.reset(owner, data.get("key_protection"), data.get("bot_keys") or {}, DeviceKeys(folder), password)
        known = {bot["id"] for bot in self.bots}
        self.keys.update({bot_id: key for bot_id, key in opened.items() if bot_id in known})

    def vault_action(self, action: str, body: dict) -> dict:
        """Set up, unlock, lock, or change how saved keys are protected. Readable keys never leave memory."""
        store = self.require_store()
        result: dict = {}
        if action == "setup":
            protection, code = self.vault.setup(body.get("passphrase"), body.get("remember") is True)
            store.put_protection(protection)
            result["recovery_code"] = code  # Shown once; it is not stored anywhere readable.
        elif action == "unlock":
            recovery = body.get("recovery_code")
            # scrypt runs here, outside the state lock, so other requests are not held up.
            opened = self.vault.unlock(body.get("passphrase"), recovery, body.get("remember") is True)
            with self.lock:
                known = {bot["id"] for bot in self.bots}
                self.keys.update({bot_id: key for bot_id, key in opened.items() if bot_id in known})
            result["used_recovery_code"] = isinstance(recovery, str) and bool(recovery.strip())
        elif action == "lock":
            with self.lock:
                for bot_id in self.vault.lock():
                    self.keys.pop(bot_id, None)
        elif action == "change":
            store.put_protection(self.vault.change_passphrase(body.get("passphrase")))
        elif action == "recovery":
            protection, code = self.vault.new_recovery_code()
            store.put_protection(protection)
            result["recovery_code"] = code
        elif action == "forget-device":
            self.vault.forget_device()
        elif action == "reset":
            if body.get("confirm") is not True:
                raise UserError("Confirm that every saved key should be deleted.")
            with self.lock:
                for bot_id in self.vault.clear():
                    store.delete_bot_key(bot_id)
                    self.keys.pop(bot_id, None)
                store.delete_protection()
        else:
            raise UserError("Unknown saved-key action.")
        return result

    def save(self) -> None:
        """Write the whole workspace. Normal changes are saved one at a time through the store."""
        if self.supabase is None and isinstance(self.store, SQLiteStore):
            self.store.save_all(self.bots, self.groups, self.grants, list(self.runs.values()))

    def public(self) -> dict:
        with self.lock:
            return copy.deepcopy({
                "bots": self.bots,
                "groups": [{**{key: value for key, value in group.items() if key != "messages"}, "message_count": message_count(group),
                            "documents": [summary(document) for document in group.get("documents", [])]} for group in self.groups],
                "grants": self.grants,
                "key_status": {bot["id"]: bool(self.keys.get(bot["id"])) for bot in self.bots},
                "runs": list(self.runs.values())[-30:][::-1],
                "account": {"mode": "supabase" if self.supabase else "local", "email": getattr(self.store, "email", "")},
                "sync_error": getattr(self.store, "error", ""),
                "sync": self.store.sync_status() if isinstance(self.store, SupabaseStore) else {"state": "local"},
                "vault": self.vault.status(),
            })

    def restore_runs(self, raw: object) -> list[dict]:
        """Load saved runs. Ones that were queued or running when the app stopped become "interrupted"."""
        saved = [run for run in raw if isinstance(run, dict) and all(isinstance(run.get(key), str) for key in ("id", "group_id", "status"))] if isinstance(raw, list) else []
        saved.sort(key=lambda run: run.get("created_at") or 0)
        interrupted = []
        for run in saved[-30:]:
            if run["status"] in ACTIVE:
                run.update(status="interrupted", error="The app stopped before this run finished.", current_bot="", waiting_reason="")
                interrupted.append(run)
            self.runs[run["id"]] = run
        return interrupted

    def publish_run(self, run_id: str, persist: bool = False) -> None:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                return
            if persist and self.store is not None:
                self.store.put_run(run)
            self.events.publish("run", {"run": run, "sync_error": getattr(self.store, "error", "")})

    def publish_message(self, group_id: str, message: dict) -> None:
        with self.lock:
            count = message_count(self.group(group_id))
            self.events.publish("message", {"group_id": group_id, "message": message, "message_count": count})

    def add_message(self, group: dict, message: dict) -> None:
        """Record a new message. Memory keeps a recent window; the store keeps the full history."""
        with self.lock:
            count = message_count(group) + 1
            group["messages"].append(message)
            del group["messages"][:-HISTORY_WINDOW]
            group["message_count"] = count
            self.require_store().add_message(group["id"], message)
            self.publish_message(group["id"], message)

    def messages_page(self, group_id: str, after: str, before: str, limit: int) -> dict:
        """The latest messages, those after a known message, or an older page before one."""
        with self.lock:
            group = self.group(group_id)
            window = copy.deepcopy(group["messages"])
            total = message_count(group)
            store = self.store
        ids = [message["id"] for message in window]
        if after and after in ids:
            return {"messages": window[ids.index(after) + 1:], "has_more": False, "total": total, "reset": False}
        if before:
            end = ids.index(before) if before in ids else -1
            if end >= limit:
                return {"messages": window[end - limit:end], "has_more": True, "total": total, "reset": False}
            # Older than the in-memory window: read that page from storage, outside the state lock.
            if store is None:
                raise UserError("Sign in to continue.")
            messages, has_more = store.load_messages(group_id, before, limit)
            return {"messages": messages, "has_more": has_more, "total": total, "reset": False}
        # The latest page. An unknown "after" means the browser's copy is stale, so it starts again.
        page = window[-limit:]
        return {"messages": page, "has_more": total > len(page), "total": total, "reset": bool(after)}

    def _clear(self) -> None:
        for event in self.stop_events.values():
            event.set()
        self.bots, self.groups, self.grants = [], [], []
        self.keys, self.runs = {}, {}
        self.vault.reset("", None, {}, None)

    def use_supabase(self, config: dict) -> None:
        """Keep data in a Supabase account; nothing loads until someone signs in."""
        with self.lock:
            previous = self.store
            self.supabase = config
            self.store = None
            self._clear()
        if isinstance(previous, SQLiteStore):
            previous.close()

    def signed_in(self) -> bool:
        return self.store is not None

    def session(self) -> dict:
        with self.lock:
            if self.supabase is None:
                return {"mode": "local", "signed_in": True, "email": "", "can_import": False}
            if not isinstance(self.store, SupabaseStore):
                return {"mode": "supabase", "signed_in": False, "email": "", "can_import": False}
            empty = not self.bots and not any(message_count(group) for group in self.groups)
            email = self.store.email
        has_local = False
        if empty:
            try:
                local = open_local_store()
                try:
                    has_local = local.has_data()
                finally:
                    local.close()
            except (OSError, sqlite3.Error):
                pass
        return {"mode": "supabase", "signed_in": True, "email": email, "can_import": has_local}

    def sign_in(self, email: object, password: object) -> None:
        if self.supabase is None:
            raise UserError("Sign-in is only used with Supabase.")
        if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password or len(email) > 320 or len(password) > 1000:
            raise UserError("Enter your email and password.")
        session = SupabaseAuth(self.supabase).sign_in(email.strip(), password)
        store = SupabaseStore(self.supabase, session, GRANTS_FILE, on_status=lambda status: self.events.publish("sync", status))
        try:
            # Changes left in the outbox by an earlier session go first, so the load below includes them.
            store.flush(20)
            data = store.load(HISTORY_WINDOW)  # Network reads stay outside the state lock.
            account = Workspace()
            account.apply(data)
        except (ValueError, TypeError, KeyError):
            store.close()
            raise UserError("Your Supabase data could not be read.") from None
        except UserError:
            store.close()
            raise
        with self.lock:
            previous = self.store
            self._clear()
            self.bots, self.groups, self.grants = account.bots, account.groups, account.grants
            for run in self.restore_runs(data.get("runs")):
                store.put_run(run)
            if not self.groups:
                self.groups = [general_group()]
                store.put_group(self.groups[0])
            self.store = store
            self._open_vault(session["user_id"], data, GRANTS_FILE.parent, password)
        if isinstance(previous, SupabaseStore):
            previous.close()

    def sign_out(self) -> int:
        """Sign out after sending what can be sent. Returns how many changes wait on this computer."""
        with self.lock:
            store, self.store = self.store, None
            self._clear()
        # Wakes open event streams so they notice the sign-out and close.
        self.events.publish("session", {})
        waiting = 0
        if isinstance(store, SupabaseStore):
            waiting = store.close()
            SupabaseAuth(self.supabase).sign_out(store.session)
        return waiting

    def import_local(self) -> None:
        """Copy this computer's saved bots, chats, and images into the signed-in, still-empty account."""
        try:
            source = open_local_store()
        except (OSError, sqlite3.Error):
            raise UserError("No local data was found to import.") from None
        try:
            data = source.load(HISTORY_WINDOW)
            files = {}
            for group in data["groups"]:
                group["messages"] = source.all_messages(group["id"])
                for document in group.get("documents", []):
                    name = document.get("file") if isinstance(document, dict) else None
                    if isinstance(name, str) and DOCUMENT_FILE.fullmatch(name):
                        try:
                            files[name] = source.load_document_file(name)
                        except OSError:
                            pass  # The extracted text is still imported without its original file.
        finally:
            source.close()
        local = Workspace()
        local.apply(data)
        if not local.bots and not any(group["messages"] for group in local.groups):
            raise UserError("No local data was found to import.")
        images = {}
        for group in local.groups:
            for message in group["messages"]:
                if message.get("image"):
                    try:
                        images[message["image"]] = (IMAGES_DIR / message["image"]).read_bytes()
                    except OSError:
                        pass  # A missing image file should not block the rest of the import.
        with self.lock:
            store = self.store
            if not isinstance(store, SupabaseStore):
                raise UserError("Sign in before importing.")
            if self.bots or any(message_count(group) for group in self.groups):
                raise UserError("This account already has data, so nothing was imported.")
            for group in self.groups:
                store.delete_group(group["id"])
            self.bots, self.groups = local.bots, local.groups
            self.grants = list(dict.fromkeys(self.grants + local.grants))
            for bot in self.bots:
                store.put_bot(bot)
            for group in self.groups:
                store.put_group(group)
                for document in group["documents"]:
                    store.put_document(group["id"], document, files.get(document["file"], b""))
                for message in group["messages"]:
                    store.add_message(group["id"], message)
                group["message_count"] = len(group["messages"])
                del group["messages"][:-HISTORY_WINDOW]
            for name, data in images.items():
                store.upload_image(name, data)
            store.put_grants(self.grants)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def require_store(self) -> SQLiteStore | SupabaseStore:
        store = self.store
        if store is None:
            raise UserError("Sign in to continue.")
        return store

    def bot(self, bot_id: str) -> dict:
        for item in self.bots:
            if item["id"] == bot_id:
                return item
        raise UserError("Bot not found.")

    def group(self, group_id: str) -> dict:
        for item in self.groups:
            if item["id"] == group_id:
                return item
        raise UserError("Group chat not found.")

    def document(self, group_id: str, document_id: str) -> dict:
        for item in self.group(group_id).get("documents", []):
            if item["id"] == document_id:
                return item
        raise UserError("Document not found.")


# Storage opens in main(), so importing this module (as the tests do) never touches saved data.
STATE = State(load=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalAgentStudio/0.2"

    def log_message(self, format_string: str, *args: object) -> None:
        # The launch URL contains a temporary access token, so do not log paths.
        pass

    def _host_ok(self) -> bool:
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Local-Token", ""), STATE.token)

    def _send(self, status: int, data: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

    def _check_api(self) -> bool:
        if not self._host_ok() or not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Open the app with the URL printed by the server."})
            return False
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin."})
            return False
        return True

    def _require_account(self) -> bool:
        if STATE.signed_in():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "Sign in to continue.", "login_required": True})
        return False

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise UserError("Send JSON to this endpoint.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UserError("Invalid request size.") from None
        if size < 1 or size > MAX_REQUEST_BYTES:
            raise UserError("Request is too large.")
        try:
            value = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise UserError("Invalid JSON request.") from None
        if not isinstance(value, dict):
            raise UserError("Expected a JSON object.")
        return value

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Use localhost."})
            return
        path = urlsplit(self.path)
        if path.path in {"/", "/bots", "/chats"}:
            token = parse_qs(path.query).get("token", [""])[0]
            if not secrets.compare_digest(token, STATE.token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "Use the URL printed by the server."})
                return
            self._send(HTTPStatus.OK, (ROOT / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path.path.startswith("/media/"):
            # <img> requests cannot send the token header, so images take it as a query.
            name = path.path.removeprefix("/media/")
            token = parse_qs(path.query).get("token", [""])[0]
            if not secrets.compare_digest(token, STATE.token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "Use the URL printed by the server."})
                return
            if not self._require_account():
                return
            try:
                if not IMAGE_NAME.fullmatch(name) or STATE.store is None:
                    raise OSError
                data = STATE.store.load_image(name)
            except OSError:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Image not found."})
                return
            # Image files never change, so the browser can keep them between polls.
            self._send(HTTPStatus.OK, data, IMAGE_TYPES[name.rsplit(".", 1)[1]], "private, max-age=31536000, immutable")
            return
        if path.path.startswith("/files/"):
            self._download_document(path)
            return
        if path.path == "/api/events":
            self._event_stream(parse_qs(path.query).get("token", [""])[0])
            return
        if path.path in {"/static/app.js", "/static/style.css"}:
            file = ROOT / path.path.lstrip("/")
            content_type = "text/javascript; charset=utf-8" if file.suffix == ".js" else "text/css; charset=utf-8"
            self._send(HTTPStatus.OK, file.read_bytes(), content_type)
            return
        if not self._check_api():
            return
        if path.path == "/api/session":
            self._json(HTTPStatus.OK, STATE.session())
            return
        if not self._require_account():
            return
        if path.path == "/api/state":
            self._json(HTTPStatus.OK, STATE.public())
        elif path.path.startswith("/api/groups/") and path.path.endswith("/messages") and path.path.count("/") == 4:
            query = parse_qs(path.query)
            try:
                limit = max(1, min(500, int(query.get("limit", ["200"])[0])))
                page = STATE.messages_page(path.path.split("/")[3], query.get("after", [""])[0], query.get("before", [""])[0], limit)
            except UserError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid message range."})
                return
            except OSError as error:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Older messages could not be loaded: {error}"})
                return
            self._json(HTTPStatus.OK, page)
        elif path.path.startswith("/api/groups/") and path.path.count("/") == 5 and path.path.split("/")[4] == "documents":
            parts = path.path.split("/")
            try:
                with STATE.lock:
                    document = copy.deepcopy(STATE.document(parts[3], parts[5]))
            except UserError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, {**summary(document), "text": document["text"]})
        elif path.path.startswith("/api/runs/"):
            run_id = path.path.rsplit("/", 1)[-1]
            with STATE.lock:
                run = copy.deepcopy(STATE.runs.get(run_id))
            self._json(HTTPStatus.OK if run else HTTPStatus.NOT_FOUND, run or {"error": "Run not found."})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def _download_document(self, path) -> None:
        # A download link cannot send the token header, so it takes the token as a query like images do.
        token = parse_qs(path.query).get("token", [""])[0]
        if not secrets.compare_digest(token, STATE.token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "Use the URL printed by the server."})
            return
        if not self._require_account():
            return
        name = path.path.removeprefix("/files/")
        with STATE.lock:
            document = next((item for group in STATE.groups for item in group.get("documents", []) if item["file"] == name), None)
            store = STATE.store
        try:
            if document is None or not DOCUMENT_FILE.fullmatch(name) or store is None:
                raise OSError
            data = store.load_document_file(name)
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Document not found."})
            return
        content_type = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}.get(document["kind"], "text/plain; charset=utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # Always saved, never shown inside the app's page.
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(document['name'])}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.end_headers()
        self.wfile.write(data)

    def _event_stream(self, token: str) -> None:
        # EventSource cannot send headers, so the stream takes the token as a query like images do.
        if not secrets.compare_digest(token, STATE.token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "Use the URL printed by the server."})
            return
        if not self._require_account():
            return
        hub = STATE.events
        last = hub.latest()
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while not hub.closed and STATE.signed_in():
                events = hub.since(last)
                if events is None:
                    # This browser fell too far behind to catch up from events, so it reloads instead.
                    events = [(hub.latest(), "resync", "{}")]
                for event_id, kind, data in events:
                    self.wfile.write(f"id: {event_id}\nevent: {kind}\ndata: {data}\n\n".encode("utf-8"))
                    last = event_id
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                hub.wait(last, 15)
        except OSError:
            pass  # The browser closed the page or is reconnecting.

    def do_POST(self) -> None:
        if not self._check_api():
            return
        try:
            parts = [part for part in urlsplit(self.path).path.split("/") if part]
            if len(parts) == 4 and parts[:2] == ["api", "groups"] and parts[3] == "documents":
                if self._require_account():
                    self._upload_document(parts[2])
                return
            body = self._body()
            if parts == ["api", "login"]:
                STATE.sign_in(body.get("email"), body.get("password"))
                self._json(HTTPStatus.OK, STATE.session())
                return
            if parts == ["api", "logout"]:
                waiting = STATE.sign_out()
                self._json(HTTPStatus.OK, {**STATE.session(), "unsynced": waiting})
                return
            if not self._require_account():
                return
            if len(parts) == 3 and parts[:2] == ["api", "vault"]:
                result = STATE.vault_action(parts[2], body)
                self._json(HTTPStatus.OK, {**STATE.public(), **result})
            elif parts == ["api", "import-local"]:
                STATE.import_local()
                self._json(HTTPStatus.OK, STATE.public())
            elif parts == ["api", "bots"]:
                bot_id = uuid.uuid4().hex
                bot = validate_bot(body, bot_id)
                with STATE.lock:
                    STATE.bots.append(bot)
                    STATE.require_store().put_bot(bot)
                self._json(HTTPStatus.CREATED, STATE.public())
            elif len(parts) >= 3 and parts[:2] == ["api", "bots"]:
                self._bot_action(parts[2:], body)
            elif parts == ["api", "groups"]:
                item = {"id": uuid.uuid4().hex, "name": group_name(body.get("name", "New group")), "bot_ids": [], "messages": [], "message_count": 0, "mode": "workflow", "master_id": "", "quality_criteria": "", "documents": []}
                with STATE.lock:
                    STATE.groups.append(item)
                    STATE.require_store().put_group(item)
                self._json(HTTPStatus.CREATED, STATE.public())
            elif len(parts) >= 3 and parts[:2] == ["api", "groups"]:
                self._group_action(parts[2:], body)
            elif parts == ["api", "grants"]:
                resolved = validate_desktop_file(body.get("path"))
                with STATE.lock:
                    if resolved not in STATE.grants:
                        STATE.grants.append(resolved)
                        STATE.require_store().put_grants(STATE.grants)
                self._json(HTTPStatus.OK, STATE.public())
            elif parts == ["api", "grants", "remove"]:
                target = body.get("path")
                with STATE.lock:
                    if target in STATE.grants:
                        STATE.grants.remove(target)
                        STATE.require_store().put_grants(STATE.grants)
                self._json(HTTPStatus.OK, STATE.public())
            elif parts == ["api", "pick-file"]:
                script = 'POSIX path of (choose file with prompt "Choose a Desktop file to allow" default location (path to desktop folder))'
                result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120, check=False)
                if result.returncode != 0:
                    raise UserError("File selection was cancelled or unavailable.")
                resolved = validate_desktop_file(result.stdout.strip())
                with STATE.lock:
                    if resolved not in STATE.grants:
                        STATE.grants.append(resolved)
                        STATE.require_store().put_grants(STATE.grants)
                self._json(HTTPStatus.OK, STATE.public())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            if parts[-1] not in {"send", "stop"}:
                STATE.events.publish("state", {})
        except UserError as error:
            # The page offers to unlock saved keys when a run needs one that is locked.
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error), **({"vault_locked": True} if isinstance(error, VaultLocked) else {})})
        except subprocess.TimeoutExpired:
            self._json(HTTPStatus.REQUEST_TIMEOUT, {"error": "File selection timed out."})
        except OSError:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Local operation failed."})

    def _bot_action(self, parts: list[str], body: dict) -> None:
        bot_id = parts[0]
        with STATE.lock:
            existing = STATE.bot(bot_id)
            if len(parts) == 1:
                replacement = validate_bot(body, bot_id)
                index = STATE.bots.index(existing)
                STATE.bots[index] = replacement
                STATE.require_store().put_bot(replacement)
            elif parts == [bot_id, "key"]:
                key = body.get("key")
                if not isinstance(key, str) or len(key) > 1000 or any(ord(char) < 32 for char in key):
                    raise UserError("Enter a valid API key.")
                key = key.strip()
                if body.get("remember") is True and key:
                    # Encrypted here; only the token, hint, and date are stored or synced.
                    STATE.require_store().put_bot_key(bot_id, STATE.vault.seal(bot_id, key))
                STATE.keys[bot_id] = key
            elif parts == [bot_id, "key", "remove"]:
                STATE.keys.pop(bot_id, None)
                if STATE.vault.remove(bot_id):
                    STATE.require_store().delete_bot_key(bot_id)
            elif parts == [bot_id, "delete"]:
                store = STATE.require_store()
                STATE.bots.remove(existing)
                STATE.keys.pop(bot_id, None)
                if STATE.vault.remove(bot_id):
                    store.delete_bot_key(bot_id)
                for group in STATE.groups:
                    if bot_id in group["bot_ids"] or group.get("master_id") == bot_id:
                        group["bot_ids"] = [item for item in group["bot_ids"] if item != bot_id]
                        if group.get("master_id") == bot_id:
                            group["master_id"] = next(iter(group["bot_ids"]), "")
                        store.put_group(group)
                store.delete_bot(bot_id)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
        self._json(HTTPStatus.OK, STATE.public())

    def _group_action(self, parts: list[str], body: dict) -> None:
        group_id = parts[0]
        if parts == [group_id, "send"]:
            self._send_group_message(group_id, body)
            return
        if parts == [group_id, "clear"]:
            self._clear_chat(group_id, body)
            return
        if len(parts) == 4 and parts[1] == "documents" and parts[3] == "remove":
            with STATE.lock:
                group = STATE.group(group_id)
                document = STATE.document(group_id, parts[2])
                group["documents"].remove(document)
                STATE.require_store().delete_document(document)
            self._json(HTTPStatus.OK, STATE.public())
            return
        if parts == [group_id, "stop"]:
            with STATE.lock:
                STATE.group(group_id)
                for run in STATE.runs.values():
                    if run["group_id"] == group_id and run["status"] in ACTIVE:
                        if run["status"] == "queued":
                            # It never started, so no worker will clear its stop event.
                            STATE.stop_events.pop(run["id"], None)
                        elif run["id"] in STATE.stop_events:
                            STATE.stop_events[run["id"]].set()
                        run.update(status="stopped", completion_reason="stopped", current_bot="", waiting_reason="")
                        if run.get("workflow"):
                            run["workflow"]["phase"] = "stopped"
                            STATE.group(group_id)["last_workflow"] = copy.deepcopy(run["workflow"])
                        STATE.publish_run(run["id"], persist=True)
                STATE.require_store().put_group(STATE.group(group_id))
            self._json(HTTPStatus.OK, STATE.public())
            return
        with STATE.lock:
            group = STATE.group(group_id)
            if parts == [group_id, "rename"]:
                group["name"] = group_name(body.get("name"))
            elif parts == [group_id, "settings"]:
                mode = body.get("mode", group.get("mode", "conversation"))
                master_id = body.get("master_id", group.get("master_id", ""))
                criteria = body.get("quality_criteria", group.get("quality_criteria", ""))
                if not isinstance(mode, str) or mode not in {"workflow", "conversation"} or not isinstance(master_id, str) or (master_id and master_id not in group["bot_ids"]):
                    raise UserError("Choose a valid mode and a master who belongs to this group.")
                if not isinstance(criteria, str) or len(criteria) > 4000:
                    raise UserError("Quality criteria must be 4,000 characters or fewer.")
                group.update(mode=mode, master_id=master_id, quality_criteria=criteria.strip())
            elif parts == [group_id, "members"]:
                bot_id = body.get("bot_id")
                enabled = body.get("enabled")
                if not isinstance(bot_id, str) or not isinstance(enabled, bool):
                    raise UserError("Choose a bot and whether it should join this group.")
                STATE.bot(bot_id)
                if enabled and bot_id not in group["bot_ids"]:
                    group["bot_ids"].append(bot_id)
                if not enabled and bot_id in group["bot_ids"]:
                    group["bot_ids"].remove(bot_id)
                if group.get("master_id") not in group["bot_ids"]:
                    group["master_id"] = next(iter(group["bot_ids"]), "")
            elif parts == [group_id, "order"]:
                order = body.get("bot_ids")
                if not isinstance(order, list) or not all(isinstance(item, str) for item in order) or len(order) != len(set(order)) or set(order) != set(group["bot_ids"]):
                    raise UserError("Choose a valid order for the bots in this group.")
                group["bot_ids"] = order
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            STATE.require_store().put_group(group)
        self._json(HTTPStatus.OK, STATE.public())

    def _upload_document(self, group_id: str) -> None:
        """Read an uploaded PDF, Word, or text file once and keep it as context for this chat."""
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise UserError("Invalid request size.") from None
        if size < 1:
            raise UserError("The file is empty.")
        if size > MAX_UPLOAD_BYTES:
            raise UserError("Documents can be up to 25 MB.")
        with STATE.lock:
            if len(STATE.group(group_id).get("documents", [])) >= MAX_DOCUMENTS:
                raise UserError(f"A chat can hold up to {MAX_DOCUMENTS} documents. Remove one first.")
        data = self.rfile.read(size)
        if len(data) != size:
            raise UserError("The upload did not finish. Try again.")
        # Reading can take seconds, so it happens outside the state lock.
        document = read_document(data, unquote(self.headers.get("X-File-Name", "")))
        with STATE.lock:
            group = STATE.group(group_id)
            documents = group.setdefault("documents", [])
            if len(documents) >= MAX_DOCUMENTS:
                raise UserError(f"A chat can hold up to {MAX_DOCUMENTS} documents. Remove one first.")
            STATE.require_store().put_document(group_id, document, data)
            documents.append(document)
        STATE.events.publish("state", {})
        self._json(HTTPStatus.CREATED, {**STATE.public(), "document": summary(document)})

    def _clear_chat(self, group_id: str, body: dict) -> None:
        """Delete every message, image, and document in one group. Bots, settings, and run history stay."""
        if body.get("confirm") is not True:
            raise UserError("Confirm that every message in this chat should be deleted.")
        STATE.group(group_id)
        store = STATE.require_store()
        # Finding saved images can mean a network call, so it happens before taking the state lock.
        images = store.group_images(group_id)
        with STATE.lock:
            group = STATE.group(group_id)
            if any(run["group_id"] == group_id and run["status"] in ACTIVE for run in STATE.runs.values()):
                raise UserError("Stop the conversation before clearing the chat.")
            cleared = message_count(group)
            images |= {message["image"] for message in group["messages"] if message.get("image")}
            group["messages"], group["message_count"] = [], 0
            group.pop("last_workflow", None)
            files = {document["file"] for document in group.get("documents", [])}
            group["documents"] = []
            store.put_group(group)
            store.clear_messages(group_id, images)
            store.clear_documents(group_id, files)
            STATE.events.publish("cleared", {"group_id": group_id})
        self._json(HTTPStatus.OK, {**STATE.public(), "cleared": cleared})

    def _send_group_message(self, group_id: str, body: dict) -> None:
        message = body.get("text")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 10000:
            raise UserError("Enter a message of 10,000 characters or fewer.")
        duration = body.get("duration_seconds", 0)
        if type(duration) is not int or not 0 <= duration <= 600:
            raise UserError("Choose one round or a duration between 1 and 600 seconds.")
        continuous = body.get("continuous", duration == 0)
        if type(continuous) is not bool or (continuous and duration):
            raise UserError("Choose an ongoing conversation or a timed run.")
        with STATE.lock:
            group = STATE.group(group_id)
            mode = body.get("mode", group.get("mode", "conversation"))
            if not isinstance(mode, str) or mode not in {"workflow", "conversation"}:
                raise UserError("Choose task workflow or group conversation.")
            if mode == "workflow":
                if duration:
                    raise UserError("Task workflows finish through approvals, not a duration.")
                continuous = False
            if any(run["group_id"] == group_id and run["status"] in ACTIVE for run in STATE.runs.values()):
                raise UserError("This group is already responding. Wait for its current turn.")
            bots = [copy.deepcopy(STATE.bot(bot_id)) for bot_id in group["bot_ids"]]
            if not bots:
                raise UserError("Add at least one bot to this group chat.")
            master_id = group.get("master_id") or bots[0]["id"]
            if mode == "workflow" and (len(bots) < 2 or master_id not in group["bot_ids"]):
                raise UserError("Choose a master and add at least one different worker.")
            if (continuous or duration) and len(bots) < 2:
                raise UserError("Add at least two bots to start a conversation.")
            keys = {bot["id"]: STATE.keys.get(bot["id"], "") for bot in bots}
            locked = [bot["name"] for bot in bots if not keys[bot["id"]] and bot["id"] in STATE.vault.saved and not STATE.vault.unlocked]
            if locked:
                raise VaultLocked(f"Unlock your saved keys to run {', '.join(locked)}.")
            for bot in bots:
                validate_runnable(bot, keys[bot["id"]])
            files = read_allowed_files(body.get("files", []), STATE.grants)
            documents = fit_documents(group.get("documents", []))
            store = STATE.require_store()
            user_message = {"id": uuid.uuid4().hex, "sender": "You", "bot_id": "", "text": message.strip(), "created_at": now(), "files": [path for path, _ in files]}
            STATE.add_message(group, user_message)
            history = copy.deepcopy(group["messages"])
            group_title = group["name"]
            run_id = uuid.uuid4().hex
            STATE.runs[run_id] = {"id": run_id, "group_id": group_id, "status": "queued", "created_at": time.time(), "completed": 0, "images": 0, "total": len(bots), "error": "", "continuous": continuous, "duration_seconds": duration, "ends_at": time.time() + duration if duration else None, "current_bot": "", "completion_reason": ""}
            STATE.runs[run_id].update(mode=mode, master_id=master_id, quality_criteria=group.get("quality_criteria", ""))
            if mode == "workflow" and group.pop("last_workflow", None) is not None:
                store.put_group(group)
            STATE.stop_events[run_id] = threading.Event()
            while len(STATE.runs) > 30:
                finished = next((key for key, run in STATE.runs.items() if run["status"] not in ACTIVE and key not in STATE.stop_events), None)
                if finished is None:
                    break
                STATE.runs.pop(finished)
            STATE.publish_run(run_id, persist=True)
            runner = STATE.runner
        runner.submit(lambda: execute_group_run(run_id, group_id, group_title, bots, history, files, keys, documents))
        self._json(HTTPStatus.ACCEPTED, {"run_id": run_id})


def execute_group_run(run_id: str, group_id: str, group_title: str, bots: list[dict], history: list[dict], files: list[tuple[str, str]], keys: dict[str, str],
                      documents: list[tuple[str, str]] = ()) -> None:
    with STATE.lock:
        run = STATE.runs.get(run_id)
        if run is None or run["status"] != "queued" or run_id not in STATE.stop_events:
            # Stopped, or signed out, while it waited in the queue.
            STATE.stop_events.pop(run_id, None)
            return
        run.update(status="running", started_at=time.time())
        duration = run["duration_seconds"]
        if duration:
            # A timed run's clock starts when it leaves the queue.
            run["ends_at"] = time.time() + duration
        continuous = run["continuous"]
        mode = run.get("mode", "conversation")
        master_id = run.get("master_id")
        quality_criteria = run.get("quality_criteria", "")
        stop_event = STATE.stop_events[run_id]
        STATE.publish_run(run_id, persist=True)

    def on_turn(bot: dict) -> None:
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running":
                STATE.runs[run_id]["current_bot"] = bot["name"]
                STATE.runs[run_id]["waiting_reason"] = ""
                STATE.publish_run(run_id)

    def on_wait(message: str) -> None:
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running":
                STATE.runs[run_id]["waiting_reason"] = message
                STATE.publish_run(run_id)

    def on_progress(progress: dict) -> None:
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running" and not stop_event.is_set():
                run = STATE.runs[run_id]
                run["workflow"] = copy.deepcopy(progress)
                run["current_bot"] = progress["master"] if progress["phase"] == "reviewing" else progress["current_worker"]
                group = STATE.group(group_id)
                group["last_workflow"] = copy.deepcopy(progress)
                STATE.require_store().put_group(group)
                STATE.publish_run(run_id, persist=True)

    def accepting() -> bool:
        run = STATE.runs.get(run_id)
        return bool(run) and run["status"] == "running" and not stop_event.is_set() and (run["ends_at"] is None or time.time() < run["ends_at"])

    def load_image(name: str) -> bytes:
        # Bots that see images get saved pictures from local files, Supabase, or the outbox.
        store = STATE.store
        if store is None:
            raise OSError("signed out")
        return store.load_image(name)

    def on_message(message: dict) -> None:
        complete = {"id": uuid.uuid4().hex, "sender": message["sender"], "bot_id": message["bot_id"], "text": message["text"], "created_at": now(), "files": []}
        if message.get("image"):
            with STATE.lock:
                store = STATE.store if accepting() else None
            if store is None:
                return
            # Image files are megabytes, so they are written before taking the state lock.
            complete["image"] = store.save_image(message["image"], message["image_type"])
        with STATE.lock:
            if not accepting():
                return
            run = STATE.runs[run_id]
            run["completed"] += 1
            if complete.get("image"):
                run["images"] = run.get("images", 0) + 1
            if message.get("approved"):
                # Recorded on the run, so the chat can mark the approving message without a schema change.
                run.update(approved_message_id=complete["id"], approved_by=message["sender"])
            STATE.add_message(STATE.group(group_id), complete)
            STATE.publish_run(run_id, persist=True)

    try:
        if mode == "workflow":
            master = next(bot for bot in bots if bot["id"] == master_id)
            workers = [bot for bot in bots if bot["id"] != master_id]
            reason = run_task_workflow(group_title, master, workers, history, files, keys, on_message,
                                       quality_criteria=quality_criteria, stop_event=stop_event,
                                       on_progress=on_progress, on_wait=on_wait, documents=documents)
        else:
            reason = run_group_conversation(group_title, bots, history, files, keys, on_message, duration_seconds=duration, continuous=continuous,
                                            stop_event=stop_event, on_turn=on_turn, on_wait=on_wait, load_image=load_image, documents=documents)
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running":
                STATE.runs[run_id].update(status="stopped" if reason == "stopped" else "complete", completion_reason=reason, current_bot="")
    except WorkflowBlocked as error:
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running":
                run = STATE.runs[run_id]
                run.update(status="blocked", error=str(error), current_bot="")
                if run.get("workflow"):
                    run["workflow"]["phase"] = "blocked"
                    group = STATE.group(group_id)
                    group["last_workflow"] = copy.deepcopy(run["workflow"])
                    STATE.require_store().put_group(group)
    except UserError as error:
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running":
                STATE.runs[run_id].update(status="failed", error=str(error), current_bot="")
    except Exception:
        with STATE.lock:
            if run_id in STATE.runs and STATE.runs[run_id]["status"] == "running":
                STATE.runs[run_id].update(status="failed", error="The group turn failed unexpectedly. Check the server terminal.", current_bot="")
    finally:
        with STATE.lock:
            STATE.stop_events.pop(run_id, None)
            STATE.publish_run(run_id, persist=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Local Agent Studio.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        supabase = load_config(ROOT)
    except ValueError as error:
        raise SystemExit(f"Supabase settings error: {error}") from None
    if supabase:
        STATE.use_supabase(supabase)
    else:
        STATE.load()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/bots?token={STATE.token}"
    print(f"Local Agent Studio: {url}", flush=True)
    if supabase:
        print(f"Storage: Supabase ({supabase['url']}). Sign in on the page that opens.", flush=True)
    print("Press Ctrl+C to stop. API keys clear when the server stops.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.events.close()
        server.server_close()
        # Send any Supabase changes still waiting in the write queue.
        STATE.close()


if __name__ == "__main__":
    main()
