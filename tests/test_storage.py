import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import core
import storage


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
CONFIG = {"url": "https://project.supabase.co", "key": "publishable-key"}


class FakeSupabase:
    """Just enough of Supabase Auth, PostgREST, and Storage for these tests."""

    def __init__(self, password="secret"):
        self.password = password
        self.tables = {"bots": [], "groups": [], "messages": [], "runs": [], "bot_keys": [], "key_protection": []}
        self.missing = set()
        self.reject = set()
        self.down = False
        self.posts = []
        self.objects = {}
        self.calls = []
        self.fail_next = None
        self.sequence = 0

    def __call__(self, method, url, headers, data=None, timeout=30):
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        self.calls.append((method, parts.path, query))
        if self.down and parts.path.startswith(("/rest/", "/storage/")):
            raise storage.SupabaseError(0, "Could not reach Supabase. Check your internet connection.")
        if self.fail_next:
            status, self.fail_next = self.fail_next, None
            raise storage.SupabaseError(status, "Simulated failure")
        if parts.path == "/auth/v1/token":
            body = json.loads(data)
            if query["grant_type"] == "password" and body["password"] != self.password:
                raise storage.SupabaseError(400, "Invalid login credentials")
            return json.dumps({"access_token": f"token-{len(self.calls)}", "refresh_token": "refresh", "expires_in": 3600,
                               "user": {"id": "user-1", "email": body.get("email", "me@example.com")}}).encode()
        if parts.path == "/auth/v1/logout":
            return b""
        if parts.path.startswith("/rest/v1/"):
            assert headers["apikey"] == CONFIG["key"] and headers["Authorization"].startswith("Bearer ")
            return self.rest(method, parts.path.rsplit("/", 1)[1], query, data)
        if parts.path.startswith("/storage/v1/object/authenticated/images/"):
            name = parts.path.removeprefix("/storage/v1/object/authenticated/images/")
            if name not in self.objects:
                raise storage.SupabaseError(404, "Object not found")
            return self.objects[name]
        if parts.path.startswith("/storage/v1/object/images/"):
            self.objects[parts.path.removeprefix("/storage/v1/object/images/")] = data
            return b"{}"
        raise AssertionError(f"Unexpected request {method} {url}")

    @staticmethod
    def identity(row):
        return row["owner_id"], row.get("id", row.get("bot_id"))

    @staticmethod
    def matches(row, key, value):
        operator, _, operand = value.partition(".")
        if operator == "eq":
            return str(row.get(key)) == operand
        if operator == "lt":
            return row.get(key) < int(operand)
        raise AssertionError(f"Unsupported filter {key}={value}")

    def rest(self, method, table, query, data):
        if table in self.missing:
            raise storage.SupabaseError(404, f'relation "public.{table}" does not exist')
        if method == "GET" and table == "message_counts":
            counts = {}
            for row in self.tables["messages"]:
                counts[row["group_id"]] = counts.get(row["group_id"], 0) + 1
            return json.dumps([{"group_id": group_id, "message_count": count} for group_id, count in counts.items()]).encode()
        rows = self.tables[table]
        if method == "GET":
            filters = {key: value for key, value in query.items() if key not in {"select", "order", "limit", "offset"}}
            rows = [row for row in rows if all(self.matches(row, key, value) for key, value in filters.items())]
            if query.get("order"):
                column, _, direction = query["order"].split(",")[0].partition(".")
                rows = sorted(rows, key=lambda row: row.get(column) or "", reverse=direction == "desc")
            offset = int(query.get("offset", 0))
            limit = int(query.get("limit", len(rows)))
            columns = query["select"].split(",")
            return json.dumps([{column: row.get(column) for column in columns} for row in rows[offset:offset + limit]]).encode()
        if method == "POST":
            body = json.loads(data)
            batch = body if isinstance(body, list) else [body]
            # Like Postgres, one bad row fails the whole statement.
            if any(self.identity(row)[1] in self.reject for row in batch):
                raise storage.SupabaseError(400, "Simulated rejected row")
            for row in batch:
                existing = next((item for item in rows if self.identity(item) == self.identity(row)), None)
                if existing:
                    existing.update(row)
                else:
                    self.sequence += 1
                    rows.append({**row, "seq": self.sequence, "created_at": row.get("created_at") or "2026-10-20T12:00:00+00:00"})
            self.posts.append((table, len(batch)))
            return b""
        if method == "DELETE":
            filters = {key: value for key, value in query.items() if "." in value}
            removed = [row for row in rows if all(self.matches(row, key, value) for key, value in filters.items())]
            self.tables[table] = [row for row in rows if row not in removed]
            # Like the schema's foreign keys, deleting a group or bot removes what belongs to it.
            for row in removed:
                if table == "groups":
                    self.tables["messages"] = [item for item in self.tables["messages"] if item["group_id"] != row["id"]]
                if table == "bots":
                    self.tables["bot_keys"] = [item for item in self.tables["bot_keys"] if item["bot_id"] != row["id"]]
            return b""
        raise AssertionError(method)


def session(expires_in=3600):
    return {"access_token": "token", "refresh_token": "refresh", "expires_at": time.time() + expires_in, "user_id": "user-1", "email": "me@example.com"}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_PUBLISHABLE_KEY"):
            os.environ.pop(name, None)

    def test_missing_settings_mean_local_mode(self):
        self.assertIsNone(storage.load_config(self.root))

    def test_env_file_is_read(self):
        (self.root / ".env").write_text('# comment\nSUPABASE_URL=https://abc.supabase.co/\nSUPABASE_ANON_KEY="pub-key"\n')
        self.assertEqual(storage.load_config(self.root), {"url": "https://abc.supabase.co", "key": "pub-key"})

    def test_insecure_url_and_secret_keys_are_rejected(self):
        for url, key in (("http://abc.supabase.co", "pub-key"), ("https://abc.supabase.co", "sb_secret_abc"), ("https://abc.supabase.co", "")):
            with self.subTest(url=url, key=key), patch.dict(os.environ, {"SUPABASE_URL": url, "SUPABASE_ANON_KEY": key}):
                with self.assertRaises(ValueError):
                    storage.load_config(self.root)


class SupabaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()
        http = patch.object(storage, "_http", self.fake)
        http.start()
        self.addCleanup(http.stop)
        self.grants = Path(tempfile.mkdtemp()) / "grants.json"

    def store(self, current_session=None):
        store = storage.SupabaseStore(CONFIG, current_session or session(), self.grants)
        self.addCleanup(store.close)
        return store

    def test_writes_round_trip_in_order_with_the_owner_set(self):
        store = self.store()
        bot = core.blank_bot("bot-1")
        store.put_bot(bot)
        group = {"id": "g1", "name": "General", "bot_ids": ["bot-1"], "messages": [], "mode": "conversation", "master_id": "bot-1", "quality_criteria": ""}
        store.put_group(group)
        for index in range(3):
            store.add_message("g1", {"id": f"m{index}", "sender": "You", "bot_id": "", "text": f"Hello {index}", "created_at": "", "files": []})
        self.assertTrue(store.flush(2))
        self.assertTrue(all(row["owner_id"] == "user-1" for table in self.fake.tables.values() for row in table))
        data = self.store().load(200)
        self.assertEqual(data["bots"][0]["id"], "bot-1")
        self.assertEqual(data["groups"][0]["bot_ids"], ["bot-1"])
        self.assertEqual([m["text"] for m in data["groups"][0]["messages"]], ["Hello 0", "Hello 1", "Hello 2"])

    def test_reads_page_through_large_tables(self):
        store = self.store()
        store.put_group({"id": "g1", "name": "General", "bot_ids": [], "messages": []})
        for index in range(5):
            store.add_message("g1", {"id": f"m{index}", "sender": "You", "text": str(index), "files": []})
        store.flush(2)
        with patch.object(storage, "PAGE_SIZE", 2):
            messages = self.store().load(200)["groups"][0]["messages"]
        self.assertEqual([m["text"] for m in messages], ["0", "1", "2", "3", "4"])

    def test_expired_token_is_refreshed_before_a_request(self):
        store = self.store(session(expires_in=-10))
        store.load(200)
        self.assertEqual(self.fake.calls[0][1], "/auth/v1/token")
        self.assertEqual(self.fake.calls[0][2]["grant_type"], "refresh_token")
        self.assertGreater(store.session["expires_at"], time.time())

    def test_rejected_writes_are_reported_and_not_retried(self):
        store = self.store()
        self.fake.fail_next = 400
        store.put_bot(core.blank_bot("bot-1"))
        store.flush(2)
        self.assertIn("Simulated failure", store.error)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.tables["bots"], [])

    def test_missing_tables_explain_the_schema_step(self):
        store = self.store()
        self.fake.fail_next = 404
        with self.assertRaisesRegex(core.UserError, "schema.sql"):
            store.load(200)

    def test_images_upload_to_the_users_folder_and_load_back(self):
        store = self.store()
        name = store.save_image(PNG, "png")
        self.assertEqual(store.load_image(name), PNG)
        store.flush(2)
        self.assertEqual(self.fake.objects[f"user-1/{name}"], PNG)
        self.assertEqual(self.store().load_image(name), PNG)
        with self.assertRaises(OSError):
            self.store().load_image("0" * 32 + ".png")

    def test_grants_stay_in_a_local_file(self):
        store = self.store()
        store.put_grants(["/Users/me/Desktop/a.txt"])
        self.assertEqual(json.loads(self.grants.read_text()), ["/Users/me/Desktop/a.txt"])
        self.assertEqual(self.grants.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(path.startswith("/rest/") for _, path, _ in self.fake.calls))


class AccountAPITests(unittest.TestCase):
    def setUp(self):
        temp = Path(tempfile.mkdtemp())
        self.settings = temp / "settings.json"
        for name, value in (("SETTINGS_FILE", self.settings), ("GRANTS_FILE", temp / "grants.json"), ("IMAGES_DIR", temp / "images")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = FakeSupabase()
        http = patch.object(storage, "_http", self.fake)
        http.start()
        self.addCleanup(http.stop)

    def start(self):
        self.state = app.State()
        self.state.use_supabase(CONFIG)
        self.addCleanup(self.state.close)
        state_patch = patch.object(app, "STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def call(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"X-Local-Token": self.state.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_data_requires_sign_in_and_a_new_account_gets_a_general_group(self):
        self.start()
        self.assertEqual(self.call("/api/state")[0], 401)
        self.assertTrue(self.call("/api/state")[1]["login_required"])
        self.assertEqual(self.call("/api/session")[1], {"mode": "supabase", "signed_in": False, "email": "", "can_import": False})
        status, body = self.call("/api/login", {"email": "me@example.com", "password": "wrong"})
        self.assertEqual((status, body["error"]), (400, "Invalid login credentials"))
        status, body = self.call("/api/login", {"email": "me@example.com", "password": "secret"})
        self.assertEqual((status, body["signed_in"], body["email"]), (200, True, "me@example.com"))
        status, state = self.call("/api/state")
        self.assertEqual((status, [g["name"] for g in state["groups"]]), (200, ["General"]))
        self.assertEqual(state["account"], {"mode": "supabase", "email": "me@example.com"})
        self.state.store.flush(2)
        self.assertEqual([row["name"] for row in self.fake.tables["groups"]], ["General"])
        self.assertFalse(self.settings.exists())

    def test_changes_are_saved_to_the_account_and_sign_out_clears_memory(self):
        self.start()
        self.call("/api/login", {"email": "me@example.com", "password": "secret"})
        self.call("/api/bots", {"name": "Writer"})
        bot_id = self.state.bots[0]["id"]
        self.call(f"/api/bots/{bot_id}/key", {"key": "provider-key"})
        group_id = self.state.groups[0]["id"]
        self.call(f"/api/groups/{group_id}/members", {"bot_id": bot_id, "enabled": True})
        self.state.store.flush(2)
        self.assertEqual(self.fake.tables["bots"][0]["data"]["name"], "Writer")
        self.assertEqual(self.fake.tables["groups"][0]["bot_ids"], [bot_id])
        self.assertNotIn("provider-key", json.dumps(self.fake.tables))
        self.call("/api/logout", {})
        self.assertEqual((self.state.bots, self.state.keys), ([], {}))
        self.assertEqual(self.call("/api/state")[0], 401)

    def test_local_data_can_be_imported_into_an_empty_account(self):
        image = "a" * 32 + ".png"
        (app.IMAGES_DIR).mkdir()
        (app.IMAGES_DIR / image).write_bytes(PNG)
        bot = core.blank_bot("local-bot")
        self.settings.write_text(json.dumps({"bots": [bot], "groups": [{"id": "local-group", "name": "Studio", "bot_ids": ["local-bot"], "messages": [
            {"id": "m1", "sender": "You", "text": "Paint a boat", "created_at": "2026-10-19T21:00:00+00:00", "files": []},
            {"id": "m2", "sender": "Artist", "bot_id": "local-bot", "text": "A boat", "created_at": "2026-10-19T21:00:05+00:00", "files": [], "image": image}]}], "grants": []}))
        self.start()
        self.assertTrue(self.call("/api/login", {"email": "me@example.com", "password": "secret"})[1]["can_import"])
        status, state = self.call("/api/import-local", {})
        self.assertEqual((status, [g["name"] for g in state["groups"]]), (200, ["Studio"]))
        self.state.store.flush(2)
        self.assertEqual([row["name"] for row in self.fake.tables["groups"]], ["Studio"])
        self.assertEqual([row["text"] for row in self.fake.tables["messages"]], ["Paint a boat", "A boat"])
        self.assertEqual(self.fake.objects[f"user-1/{image}"], PNG)
        self.assertFalse(self.call("/api/session")[1]["can_import"])
        self.assertEqual(self.call("/api/import-local", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
