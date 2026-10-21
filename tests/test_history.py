import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import core
import storage
from test_storage import CONFIG, FakeSupabase, session


def message(index, group="g1"):
    return {"id": f"{group}-m{index}", "sender": "You", "bot_id": "", "text": f"Message {index}", "created_at": "", "files": []}


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        temp = Path(tempfile.mkdtemp())
        self.store = storage.SQLiteStore(temp / "app.db", temp / "images")
        self.addCleanup(self.store.close)
        self.path = temp / "app.db"

    def test_file_is_owner_only(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_bots_and_groups_keep_their_order_when_updated(self):
        for name in ("a", "b", "c"):
            self.store.put_bot(dict(core.blank_bot(name), name=name.upper()))
        self.store.put_bot(dict(core.blank_bot("a"), name="Renamed"))
        self.store.put_group({"id": "g1", "name": "One", "bot_ids": ["a"], "messages": [], "message_count": 0})
        self.store.put_group({"id": "g2", "name": "Two", "bot_ids": [], "messages": []})
        data = self.store.load(10)
        self.assertEqual([(bot["id"], bot["name"]) for bot in data["bots"]], [("a", "Renamed"), ("b", "B"), ("c", "C")])
        self.assertEqual([group["name"] for group in data["groups"]], ["One", "Two"])
        self.assertNotIn("message_count", json.loads(self.store._read("select data from groups where id = 'g1'")[0][0]))

    def test_load_returns_only_the_latest_window_with_the_full_count(self):
        self.store.put_group({"id": "g1", "name": "One", "bot_ids": [], "messages": []})
        for index in range(12):
            self.store.add_message("g1", message(index))
        self.store.add_message("g1", message(11))  # Saving the same message twice is harmless.
        group = self.store.load(5)["groups"][0]
        self.assertEqual(([m["text"] for m in group["messages"]], group["message_count"]),
                         ([f"Message {i}" for i in range(7, 12)], 12))

    def test_older_messages_load_a_page_at_a_time(self):
        self.store.put_group({"id": "g1", "name": "One", "bot_ids": [], "messages": []})
        for index in range(10):
            self.store.add_message("g1", message(index))
        page, more = self.store.load_messages("g1", "g1-m6", 4)
        self.assertEqual(([m["id"] for m in page], more), (["g1-m2", "g1-m3", "g1-m4", "g1-m5"], True))
        page, more = self.store.load_messages("g1", "g1-m2", 4)
        self.assertEqual(([m["id"] for m in page], more), (["g1-m0", "g1-m1"], False))
        self.assertEqual(self.store.load_messages("g1", "missing", 4), ([], False))

    def test_runs_load_most_recent_in_time_order(self):
        for index in range(35):
            self.store.put_run({"id": f"r{index}", "group_id": "g1", "status": "complete", "created_at": float(index)})
        runs = self.store.load(5)["runs"]
        self.assertEqual((len(runs), runs[0]["id"], runs[-1]["id"]), (30, "r5", "r34"))

    def test_save_all_replaces_the_workspace_and_deleting_a_group_removes_its_messages(self):
        group = {"id": "g1", "name": "One", "bot_ids": [], "messages": [message(0)]}
        self.store.save_all([core.blank_bot("a"), core.blank_bot("b")], [group, {"id": "g2", "name": "Two", "bot_ids": [], "messages": [message(0, "g2")]}], ["/x"], [])
        self.store.save_all([core.blank_bot("b")], [group], ["/y"], [])
        data = self.store.load(5)
        self.assertEqual(([bot["id"] for bot in data["bots"]], [g["id"] for g in data["groups"]], data["grants"]), (["b"], ["g1"], ["/y"]))
        self.assertEqual(self.store._read("select count(*) from messages where group_id = 'g2'")[0][0], 0)
        self.store.delete_group("g1")
        self.assertEqual(self.store._read("select count(*) from messages")[0][0], 0)
        self.assertTrue(self.store.has_data())  # Bot "b" is still saved.


class LocalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.settings = self.temp / "settings.json"
        for name, value in (("SETTINGS_FILE", self.settings), ("IMAGES_DIR", self.temp / "images"), ("HISTORY_WINDOW", 5)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_legacy(self, count):
        bot = core.blank_bot("bot-1")
        self.settings.write_text(json.dumps({"version": 2, "bots": [bot], "grants": [], "groups": [
            {"id": "g1", "name": "Studio", "bot_ids": ["bot-1"], "messages": [message(i) for i in range(count)]}],
            "runs": [{"id": "r1", "group_id": "g1", "status": "running", "created_at": 1.0}]}))

    def test_settings_json_is_imported_once_and_kept_as_a_backup(self):
        self.write_legacy(12)
        state = app.State()
        group = state.groups[0]
        self.assertEqual((group["name"], len(group["messages"]), group["message_count"]), ("Studio", 5, 12))
        self.assertEqual(group["messages"][-1]["text"], "Message 11")
        self.assertEqual(state.runs["r1"]["status"], "interrupted")
        self.assertTrue(self.settings.exists())
        state.close()
        again = app.State()
        self.assertEqual(again.groups[0]["message_count"], 12)
        self.assertEqual(again.store._read("select count(*) from messages")[0][0], 12)
        again.close()

    def test_memory_keeps_a_window_while_storage_keeps_everything(self):
        state = app.State()
        group = state.groups[0]
        for index in range(8):
            state.add_message(group, message(index, group["id"]))
        self.assertEqual((len(group["messages"]), group["message_count"]), (5, 8))
        self.assertEqual(state.store._read("select count(*) from messages")[0][0], 8)
        self.assertEqual(state.public()["groups"][0]["message_count"], 8)
        state.close()

    def test_pages_past_the_window_come_from_storage(self):
        self.write_legacy(12)
        state = app.State()
        latest = state.messages_page("g1", "", "", 5)
        self.assertEqual(([m["id"] for m in latest["messages"]], latest["has_more"], latest["total"]), ([f"g1-m{i}" for i in range(7, 12)], True, 12))
        older = state.messages_page("g1", "", "g1-m7", 5)
        self.assertEqual(([m["id"] for m in older["messages"]], older["has_more"]), ([f"g1-m{i}" for i in range(2, 7)], True))
        oldest = state.messages_page("g1", "", "g1-m2", 5)
        self.assertEqual(([m["id"] for m in oldest["messages"]], oldest["has_more"]), (["g1-m0", "g1-m1"], False))
        state.close()

    def test_a_run_only_receives_the_recent_window_as_history(self):
        self.write_legacy(12)
        state = app.State()
        bot = state.bots[0]
        bot.update(endpoint="http://127.0.0.1:9/model", model="fake")
        other = dict(core.blank_bot("bot-2"), name="Other", endpoint="http://127.0.0.1:9/model", model="fake")
        state.bots.append(other)
        state.groups[0]["bot_ids"] = ["bot-1", "bot-2"]
        state.keys.update({"bot-1": "k", "bot-2": "k"})
        seen = []
        with patch.object(app, "STATE", state), patch.object(state.runner, "submit", side_effect=lambda job: seen.append(job)):
            handler = app.Handler.__new__(app.Handler)
            handler._json = lambda status, body: None
            with patch.object(app, "execute_group_run") as execute:
                handler._send_group_message("g1", {"text": "Go", "continuous": False})
                seen[0]()
        history = execute.call_args.args[4]
        self.assertEqual((len(history), history[-1]["text"]), (5, "Go"))
        self.assertEqual(state.groups[0]["message_count"], 13)
        state.close()


class SupabaseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()
        http = patch.object(storage, "_http", self.fake)
        http.start()
        self.addCleanup(http.stop)
        self.grants = Path(tempfile.mkdtemp()) / "grants.json"

    def filled_store(self, count):
        store = storage.SupabaseStore(CONFIG, session(), self.grants)
        self.addCleanup(store.close)
        store.put_group({"id": "g1", "name": "One", "bot_ids": [], "messages": []})
        for index in range(count):
            store.add_message("g1", message(index))
        store.flush(2)
        return store

    def test_sign_in_loads_only_the_latest_window_with_counts(self):
        group = self.filled_store(12).load(5)["groups"][0]
        self.assertEqual(([m["id"] for m in group["messages"]], group["message_count"]), ([f"g1-m{i}" for i in range(7, 12)], 12))

    def test_older_pages_come_from_supabase(self):
        store = self.filled_store(12)
        page, more = store.load_messages("g1", "g1-m7", 5)
        self.assertEqual(([m["id"] for m in page], more), ([f"g1-m{i}" for i in range(2, 7)], True))
        page, more = store.load_messages("g1", "g1-m2", 5)
        self.assertEqual(([m["id"] for m in page], more), (["g1-m0", "g1-m1"], False))

    def test_without_the_counts_view_older_messages_are_still_detected(self):
        store = self.filled_store(12)
        self.fake.missing.add("message_counts")
        group = store.load(5)["groups"][0]
        self.assertEqual((len(group["messages"]), group["message_count"]), (5, 6))
        self.assertIn("schema.sql", store.error)

    def test_a_long_local_history_imports_completely_but_memory_keeps_a_window(self):
        temp = Path(tempfile.mkdtemp())
        with patch.object(app, "SETTINGS_FILE", temp / "settings.json"), patch.object(app, "IMAGES_DIR", temp / "images"), \
                patch.object(app, "GRANTS_FILE", self.grants), patch.object(app, "HISTORY_WINDOW", 5):
            local = app.State()
            for index in range(12):
                local.add_message(local.groups[0], message(index, local.groups[0]["id"]))
            local.close()
            state = app.State()
            state.use_supabase(CONFIG)
            state.sign_in("me@example.com", "secret")
            self.assertTrue(state.session()["can_import"])
            state.import_local()
            state.store.flush(2)
            self.assertEqual(len(self.fake.tables["messages"]), 12)
            self.assertEqual((len(state.groups[0]["messages"]), state.groups[0]["message_count"]), (5, 12))
            state.close()


if __name__ == "__main__":
    unittest.main()
