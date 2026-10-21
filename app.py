"""Local-only server for configurable bots and named group chats."""

from __future__ import annotations

import argparse
import copy
import json
import re
import secrets
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
from urllib.parse import parse_qs, urlsplit

from core import UserError, blank_bot, read_allowed_files, run_group_conversation, validate_bot, validate_desktop_file, validate_runnable
from storage import IMAGE_TYPES, LocalStore, SupabaseAuth, SupabaseError, SupabaseStore, load_config
from workflow import WorkflowBlocked, run_task_workflow


ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / ".local" / "settings.json"
GRANTS_FILE = ROOT / ".local" / "grants.json"
IMAGES_DIR = ROOT / ".local" / "images"
IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
MAX_REQUEST_BYTES = 100 * 1024


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


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def group_name(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise UserError("Enter a group name of 100 characters or fewer.")
    return value.strip()


def general_group() -> dict:
    return {"id": uuid.uuid4().hex, "name": "General", "bot_ids": [], "messages": [], "mode": "conversation", "master_id": "", "quality_criteria": ""}


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


def read_local_workspace() -> Workspace | None:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        local = Workspace()
        local.apply(data if isinstance(data, dict) else {})
        return local
    except (OSError, ValueError, TypeError, KeyError):
        return None


class State(Workspace):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.keys: dict[str, str] = {}
        self.runs: dict[str, dict] = {}
        self.stop_events: dict[str, threading.Event] = {}
        self.events = EventHub()
        self.store: LocalStore | SupabaseStore | None = LocalStore(self.save, IMAGES_DIR)
        # Set when Supabase is configured; the account is then required before any data loads.
        self.supabase: dict | None = None
        self.load()

    def load(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid settings")
            self.apply(data)
        except (OSError, ValueError, TypeError, KeyError):
            print("Saved settings could not be loaded; starting with defaults.")

    def save(self) -> None:
        if self.supabase is not None:
            return  # Account data must never overwrite the local settings file.
        SETTINGS_FILE.parent.mkdir(mode=0o700, exist_ok=True)
        payload = {"version": 2, "bots": self.bots, "groups": self.groups, "grants": self.grants}
        temp = SETTINGS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(SETTINGS_FILE)

    def public(self) -> dict:
        with self.lock:
            return copy.deepcopy({
                "bots": self.bots,
                "groups": [{**{key: value for key, value in group.items() if key != "messages"}, "message_count": len(group["messages"])} for group in self.groups],
                "grants": self.grants,
                "key_status": {bot["id"]: bool(self.keys.get(bot["id"])) for bot in self.bots},
                "runs": list(self.runs.values())[-30:][::-1],
                "account": {"mode": "supabase" if self.supabase else "local", "email": getattr(self.store, "email", "")},
                "sync_error": getattr(self.store, "error", ""),
            })

    def publish_run(self, run_id: str) -> None:
        with self.lock:
            run = self.runs.get(run_id)
            if run is not None:
                self.events.publish("run", {"run": run, "sync_error": getattr(self.store, "error", "")})

    def publish_message(self, group_id: str, message: dict) -> None:
        with self.lock:
            count = len(self.group(group_id)["messages"])
            self.events.publish("message", {"group_id": group_id, "message": message, "message_count": count})

    def messages_page(self, group_id: str, after: str, before: str, limit: int) -> dict:
        """The latest messages, those after a known message, or an older page before one."""
        with self.lock:
            messages = self.group(group_id)["messages"]
            ids = [message["id"] for message in messages]
            reset = False
            if after and after in ids:
                window, has_more = messages[ids.index(after) + 1:], False
            else:
                # An unknown "after" means the browser's copy is stale, so it gets a fresh window.
                reset = bool(after)
                end = ids.index(before) if before in ids else len(messages)
                start = max(0, end - limit)
                window, has_more = messages[start:end], start > 0
            return copy.deepcopy({"messages": window, "has_more": has_more, "total": len(messages), "reset": reset})

    def _clear(self) -> None:
        for event in self.stop_events.values():
            event.set()
        self.bots, self.groups, self.grants = [], [], []
        self.keys, self.runs = {}, {}

    def use_supabase(self, config: dict) -> None:
        """Keep data in a Supabase account; nothing loads until someone signs in."""
        with self.lock:
            self.supabase = config
            self.store = None
            self._clear()

    def signed_in(self) -> bool:
        return self.store is not None

    def session(self) -> dict:
        with self.lock:
            if self.supabase is None:
                return {"mode": "local", "signed_in": True, "email": "", "can_import": False}
            if not isinstance(self.store, SupabaseStore):
                return {"mode": "supabase", "signed_in": False, "email": "", "can_import": False}
            empty = not self.bots and not any(group["messages"] for group in self.groups)
            email = self.store.email
        local = read_local_workspace() if empty else None
        has_local = bool(local and (local.bots or any(group["messages"] for group in local.groups)))
        return {"mode": "supabase", "signed_in": True, "email": email, "can_import": has_local}

    def sign_in(self, email: object, password: object) -> None:
        if self.supabase is None:
            raise UserError("Sign-in is only used with Supabase.")
        if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password or len(email) > 320 or len(password) > 1000:
            raise UserError("Enter your email and password.")
        session = SupabaseAuth(self.supabase).sign_in(email.strip(), password)
        store = SupabaseStore(self.supabase, session, GRANTS_FILE)
        try:
            data = store.load()  # Network reads stay outside the state lock.
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
            if not self.groups:
                self.groups = [general_group()]
                store.put_group(self.groups[0])
            self.store = store
        if isinstance(previous, SupabaseStore):
            previous.close()

    def sign_out(self) -> None:
        with self.lock:
            store, self.store = self.store, None
            self._clear()
        # Wakes open event streams so they notice the sign-out and close.
        self.events.publish("session", {})
        if isinstance(store, SupabaseStore):
            store.close()
            SupabaseAuth(self.supabase).sign_out(store.session)

    def import_local(self) -> None:
        """Copy this computer's saved bots, chats, and images into the signed-in, still-empty account."""
        local = read_local_workspace()
        if local is None:
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
            if self.bots or any(group["messages"] for group in self.groups):
                raise UserError("This account already has data, so nothing was imported.")
            for group in self.groups:
                store.delete_group(group["id"])
            self.bots, self.groups = local.bots, local.groups
            self.grants = list(dict.fromkeys(self.grants + local.grants))
            for bot in self.bots:
                store.put_bot(bot)
            for group in self.groups:
                store.put_group(group)
                for message in group["messages"]:
                    store.add_message(group["id"], message)
            for name, data in images.items():
                store.upload_image(name, data)
            store.put_grants(self.grants)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def require_store(self) -> LocalStore | SupabaseStore:
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


STATE = State()


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
            self._json(HTTPStatus.OK, page)
        elif path.path.startswith("/api/runs/"):
            run_id = path.path.rsplit("/", 1)[-1]
            with STATE.lock:
                run = copy.deepcopy(STATE.runs.get(run_id))
            self._json(HTTPStatus.OK if run else HTTPStatus.NOT_FOUND, run or {"error": "Run not found."})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

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
            body = self._body()
            parts = [part for part in urlsplit(self.path).path.split("/") if part]
            if parts == ["api", "login"]:
                STATE.sign_in(body.get("email"), body.get("password"))
                self._json(HTTPStatus.OK, STATE.session())
                return
            if parts == ["api", "logout"]:
                STATE.sign_out()
                self._json(HTTPStatus.OK, STATE.session())
                return
            if not self._require_account():
                return
            if parts == ["api", "import-local"]:
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
                item = {"id": uuid.uuid4().hex, "name": group_name(body.get("name", "New group")), "bot_ids": [], "messages": [], "mode": "workflow", "master_id": "", "quality_criteria": ""}
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
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
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
                STATE.keys[bot_id] = key.strip()
            elif parts == [bot_id, "delete"]:
                store = STATE.require_store()
                STATE.bots.remove(existing)
                STATE.keys.pop(bot_id, None)
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
        if parts == [group_id, "stop"]:
            with STATE.lock:
                STATE.group(group_id)
                for run in STATE.runs.values():
                    if run["group_id"] == group_id and run["status"] == "running":
                        STATE.stop_events[run["id"]].set()
                        run.update(status="stopped", completion_reason="stopped", current_bot="")
                        if run.get("workflow"):
                            run["workflow"]["phase"] = "stopped"
                            STATE.group(group_id)["last_workflow"] = copy.deepcopy(run["workflow"])
                        STATE.publish_run(run["id"])
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
            if any(run["group_id"] == group_id and run["status"] == "running" for run in STATE.runs.values()):
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
            for bot in bots:
                validate_runnable(bot, keys[bot["id"]])
            files = read_allowed_files(body.get("files", []), STATE.grants)
            store = STATE.require_store()
            user_message = {"id": uuid.uuid4().hex, "sender": "You", "bot_id": "", "text": message.strip(), "created_at": now(), "files": [path for path, _ in files]}
            group["messages"].append(user_message)
            history = copy.deepcopy(group["messages"])
            group_title = group["name"]
            run_id = uuid.uuid4().hex
            STATE.runs[run_id] = {"id": run_id, "group_id": group_id, "status": "running", "completed": 0, "total": len(bots), "error": "", "continuous": continuous, "duration_seconds": duration, "ends_at": time.time() + duration if duration else None, "current_bot": "", "completion_reason": ""}
            STATE.runs[run_id].update(mode=mode, master_id=master_id, quality_criteria=group.get("quality_criteria", ""))
            if mode == "workflow" and group.pop("last_workflow", None) is not None:
                store.put_group(group)
            STATE.stop_events[run_id] = threading.Event()
            while len(STATE.runs) > 30:
                finished = next((key for key, run in STATE.runs.items() if run["status"] != "running" and key not in STATE.stop_events), None)
                if finished is None:
                    break
                STATE.runs.pop(finished)
            store.add_message(group_id, user_message)
            STATE.publish_message(group_id, user_message)
            STATE.publish_run(run_id)
        threading.Thread(target=execute_group_run, args=(run_id, group_id, group_title, bots, history, files, keys), daemon=True).start()
        self._json(HTTPStatus.ACCEPTED, {"run_id": run_id})


def execute_group_run(run_id: str, group_id: str, group_title: str, bots: list[dict], history: list[dict], files: list[tuple[str, str]], keys: dict[str, str]) -> None:
    with STATE.lock:
        duration = STATE.runs[run_id]["duration_seconds"]
        continuous = STATE.runs[run_id]["continuous"]
        mode = STATE.runs[run_id].get("mode", "conversation")
        master_id = STATE.runs[run_id].get("master_id")
        quality_criteria = STATE.runs[run_id].get("quality_criteria", "")
        stop_event = STATE.stop_events[run_id]

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
                STATE.publish_run(run_id)

    def accepting() -> bool:
        run = STATE.runs.get(run_id)
        return bool(run) and run["status"] == "running" and not stop_event.is_set() and (run["ends_at"] is None or time.time() < run["ends_at"])

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
            STATE.group(group_id)["messages"].append(complete)
            STATE.runs[run_id]["completed"] += 1
            STATE.require_store().add_message(group_id, complete)
            STATE.publish_message(group_id, complete)
            STATE.publish_run(run_id)

    try:
        if mode == "workflow":
            master = next(bot for bot in bots if bot["id"] == master_id)
            workers = [bot for bot in bots if bot["id"] != master_id]
            reason = run_task_workflow(group_title, master, workers, history, files, keys, on_message,
                                       quality_criteria=quality_criteria, stop_event=stop_event,
                                       on_progress=on_progress, on_wait=on_wait)
        else:
            reason = run_group_conversation(group_title, bots, history, files, keys, on_message, duration_seconds=duration, continuous=continuous, stop_event=stop_event, on_turn=on_turn, on_wait=on_wait)
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
            STATE.publish_run(run_id)


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
