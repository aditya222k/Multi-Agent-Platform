"""Local-only server for configurable bots and named group chats."""

from __future__ import annotations

import argparse
import copy
import json
import secrets
import subprocess
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core import UserError, blank_bot, read_allowed_files, run_group_round, validate_bot, validate_desktop_file, validate_runnable


ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / ".local" / "settings.json"
MAX_REQUEST_BYTES = 100 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def group_name(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise UserError("Enter a group name of 100 characters or fewer.")
    return value.strip()


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.bots: list[dict] = []
        self.groups: list[dict] = [{"id": uuid.uuid4().hex, "name": "General", "bot_ids": [], "messages": []}]
        self.grants: list[str] = []
        self.keys: dict[str, str] = {}
        self.runs: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid settings")
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
                    groups.append({"id": item["id"], "name": name, "bot_ids": list(dict.fromkeys(members)), "messages": messages})
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
        except (OSError, ValueError, TypeError, KeyError):
            print("Saved settings could not be loaded; starting with defaults.")

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

    def save(self) -> None:
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
                "groups": self.groups,
                "grants": self.grants,
                "key_status": {bot["id"]: bool(self.keys.get(bot["id"])) for bot in self.bots},
                "runs": list(self.runs.values())[-30:][::-1],
            })

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

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
        if path.path in {"/static/app.js", "/static/style.css"}:
            file = ROOT / path.path.lstrip("/")
            content_type = "text/javascript; charset=utf-8" if file.suffix == ".js" else "text/css; charset=utf-8"
            self._send(HTTPStatus.OK, file.read_bytes(), content_type)
            return
        if not self._check_api():
            return
        if path.path == "/api/state":
            self._json(HTTPStatus.OK, STATE.public())
        elif path.path.startswith("/api/runs/"):
            run_id = path.path.rsplit("/", 1)[-1]
            with STATE.lock:
                run = copy.deepcopy(STATE.runs.get(run_id))
            self._json(HTTPStatus.OK if run else HTTPStatus.NOT_FOUND, run or {"error": "Run not found."})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._check_api():
            return
        try:
            body = self._body()
            parts = [part for part in urlsplit(self.path).path.split("/") if part]
            if parts == ["api", "bots"]:
                bot_id = uuid.uuid4().hex
                bot = validate_bot(body, bot_id)
                with STATE.lock:
                    STATE.bots.append(bot)
                    STATE.save()
                self._json(HTTPStatus.CREATED, STATE.public())
            elif len(parts) >= 3 and parts[:2] == ["api", "bots"]:
                self._bot_action(parts[2:], body)
            elif parts == ["api", "groups"]:
                item = {"id": uuid.uuid4().hex, "name": group_name(body.get("name", "New group")), "bot_ids": [], "messages": []}
                with STATE.lock:
                    STATE.groups.append(item)
                    STATE.save()
                self._json(HTTPStatus.CREATED, STATE.public())
            elif len(parts) >= 3 and parts[:2] == ["api", "groups"]:
                self._group_action(parts[2:], body)
            elif parts == ["api", "grants"]:
                resolved = validate_desktop_file(body.get("path"))
                with STATE.lock:
                    if resolved not in STATE.grants:
                        STATE.grants.append(resolved)
                        STATE.save()
                self._json(HTTPStatus.OK, STATE.public())
            elif parts == ["api", "grants", "remove"]:
                target = body.get("path")
                with STATE.lock:
                    if target in STATE.grants:
                        STATE.grants.remove(target)
                        STATE.save()
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
                        STATE.save()
                self._json(HTTPStatus.OK, STATE.public())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
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
                STATE.save()
            elif parts == [bot_id, "key"]:
                key = body.get("key")
                if not isinstance(key, str) or len(key) > 1000 or any(ord(char) < 32 for char in key):
                    raise UserError("Enter a valid API key.")
                STATE.keys[bot_id] = key.strip()
            elif parts == [bot_id, "delete"]:
                STATE.bots.remove(existing)
                STATE.keys.pop(bot_id, None)
                for group in STATE.groups:
                    group["bot_ids"] = [item for item in group["bot_ids"] if item != bot_id]
                STATE.save()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
        self._json(HTTPStatus.OK, STATE.public())

    def _group_action(self, parts: list[str], body: dict) -> None:
        group_id = parts[0]
        if parts == [group_id, "send"]:
            self._send_group_message(group_id, body)
            return
        with STATE.lock:
            group = STATE.group(group_id)
            if parts == [group_id, "rename"]:
                group["name"] = group_name(body.get("name"))
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
            elif parts == [group_id, "order"]:
                order = body.get("bot_ids")
                if not isinstance(order, list) or not all(isinstance(item, str) for item in order) or len(order) != len(set(order)) or set(order) != set(group["bot_ids"]):
                    raise UserError("Choose a valid order for the bots in this group.")
                group["bot_ids"] = order
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            STATE.save()
        self._json(HTTPStatus.OK, STATE.public())

    def _send_group_message(self, group_id: str, body: dict) -> None:
        message = body.get("text")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 10000:
            raise UserError("Enter a message of 10,000 characters or fewer.")
        with STATE.lock:
            group = STATE.group(group_id)
            if any(run["group_id"] == group_id and run["status"] == "running" for run in STATE.runs.values()):
                raise UserError("This group is already responding. Wait for its current turn.")
            bots = [copy.deepcopy(STATE.bot(bot_id)) for bot_id in group["bot_ids"]]
            if not bots:
                raise UserError("Add at least one bot to this group chat.")
            keys = {bot["id"]: STATE.keys.get(bot["id"], "") for bot in bots}
            for bot in bots:
                validate_runnable(bot, keys[bot["id"]])
            files = read_allowed_files(body.get("files", []), STATE.grants)
            user_message = {"id": uuid.uuid4().hex, "sender": "You", "bot_id": "", "text": message.strip(), "created_at": now(), "files": [path for path, _ in files]}
            group["messages"].append(user_message)
            history = copy.deepcopy(group["messages"])
            group_title = group["name"]
            run_id = uuid.uuid4().hex
            STATE.runs[run_id] = {"id": run_id, "group_id": group_id, "status": "running", "completed": 0, "total": len(bots), "error": ""}
            while len(STATE.runs) > 30:
                STATE.runs.pop(next(iter(STATE.runs)))
            STATE.save()
        threading.Thread(target=execute_group_run, args=(run_id, group_id, group_title, bots, history, files, keys), daemon=True).start()
        self._json(HTTPStatus.ACCEPTED, {"run_id": run_id})


def execute_group_run(run_id: str, group_id: str, group_title: str, bots: list[dict], history: list[dict], files: list[tuple[str, str]], keys: dict[str, str]) -> None:
    def on_message(message: dict) -> None:
        complete = {"id": uuid.uuid4().hex, "sender": message["sender"], "bot_id": message["bot_id"], "text": message["text"], "created_at": now(), "files": []}
        with STATE.lock:
            STATE.group(group_id)["messages"].append(complete)
            STATE.runs[run_id]["completed"] += 1
            STATE.save()

    try:
        run_group_round(group_title, bots, history, files, keys, on_message)
        with STATE.lock:
            STATE.runs[run_id]["status"] = "complete"
    except UserError as error:
        with STATE.lock:
            STATE.runs[run_id].update(status="failed", error=str(error))
    except Exception:
        with STATE.lock:
            STATE.runs[run_id].update(status="failed", error="The group turn failed unexpectedly. Check the server terminal.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Local Agent Studio.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/bots?token={STATE.token}"
    print(f"Local Agent Studio: {url}", flush=True)
    print("Press Ctrl+C to stop. API keys clear when the server stops.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
