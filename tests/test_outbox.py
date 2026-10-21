import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import storage
from test_storage import CONFIG, FakeSupabase, session


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def message(index):
    return {"id": f"m{index}", "sender": "You", "bot_id": "", "text": f"Message {index}", "created_at": "", "files": []}


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "outbox.db"
        self.outbox = storage.Outbox(self.path, "user-1")
        self.addCleanup(self.outbox.close)

    def payloads(self):
        return [(entry["kind"], entry["key"], bytes(entry["payload"])) for entry in storage.Outbox(self.path, "user-1").take(100)]

    def test_newer_versions_of_a_waiting_row_replace_older_ones(self):
        self.outbox.add("upsert", "runs", "r1", b"v1", merge=True)
        self.outbox.add("upsert", "messages", "m1", b"hello", merge=True)
        self.outbox.add("upsert", "runs", "r1", b"v2", merge=True)
        self.assertEqual(self.payloads(), [("upsert", "r1", b"v2"), ("upsert", "m1", b"hello")])

    def test_a_delete_in_between_keeps_its_place(self):
        self.outbox.add("upsert", "bots", "b1", b"v1", merge=True)
        self.outbox.add("delete", "bots", "b1", b"")
        self.outbox.add("upsert", "bots", "b1", b"v2", merge=True)
        self.assertEqual([kind for kind, _, _ in self.payloads()], ["upsert", "delete", "upsert"])

    def test_a_row_being_sent_is_not_changed_underneath(self):
        self.outbox.add("upsert", "runs", "r1", b"v1", merge=True)
        sending = self.outbox.take(10)
        self.outbox.add("upsert", "runs", "r1", b"v2", merge=True)
        self.outbox.finish([sending[0]["seq"]])
        self.assertEqual(self.payloads(), [("upsert", "r1", b"v2")])

    def test_entries_survive_a_restart_and_unfinished_sends_go_again(self):
        self.outbox.add("upsert", "messages", "m1", b"x", merge=True)
        self.outbox.take(10)  # The app stops mid-send.
        self.outbox.close()
        reopened = storage.Outbox(self.path, "user-1")
        self.assertEqual(len(reopened.take(10)), 1)
        reopened.close()
        self.outbox = storage.Outbox(self.path, "user-1")

    def test_each_account_only_sees_its_own_changes(self):
        self.outbox.add("upsert", "messages", "m1", b"x")
        other = storage.Outbox(self.path, "user-2")
        self.assertEqual((other.take(10), other.counts()[0]), ([], 0))
        other.close()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase()
        http = patch.object(storage, "_http", self.fake)
        http.start()
        self.addCleanup(http.stop)
        self.grants = Path(tempfile.mkdtemp()) / "grants.json"
        self.statuses = []

    def store(self, current_session=None):
        store = storage.SupabaseStore(CONFIG, current_session or session(), self.grants, on_status=self.statuses.append)
        self.addCleanup(lambda: store._stopped or store.close(1))
        store.put_group({"id": "g1", "name": "General", "bot_ids": [], "messages": []})
        return store

    def test_changes_go_out_in_batches(self):
        store = self.store()
        for index in range(30):
            store.add_message("g1", message(index))
            store.put_run({"id": "r1", "group_id": "g1", "status": "running", "completed": index})
        self.assertTrue(store.flush(2))
        # 61 changes become 4 requests: the 30 run updates merged into one waiting row, kept in its original place.
        self.assertEqual(self.fake.posts, [("groups", 1), ("messages", 1), ("runs", 1), ("messages", 29)])
        self.assertEqual(self.fake.tables["runs"][0]["data"]["completed"], 29)
        self.assertEqual([row["text"] for row in self.fake.tables["messages"]][-1], "Message 29")

    def test_an_outage_keeps_every_change_until_supabase_answers_again(self):
        store = self.store()
        self.fake.down = True
        for index in range(5):
            store.add_message("g1", message(index))
        self.assertFalse(store.flush(2))
        status = store.sync_status()
        self.assertEqual((status["state"], status["pending"], status["failed"]), ("offline", 6, 0))
        self.assertIn("Could not reach Supabase", status["reason"])
        for index in range(5, 8):
            store.add_message("g1", message(index))
        self.assertEqual(store.sync_status()["pending"], 9)
        self.fake.down = False
        self.assertTrue(store.flush(2))
        self.assertEqual([row["id"] for row in self.fake.tables["messages"]], [f"m{i}" for i in range(8)])
        self.assertEqual(store.sync_status(), {"state": "saved", "pending": 0, "failed": 0, "reason": ""})
        self.assertIn("offline", [status["state"] for status in self.statuses])

    def test_changes_left_by_a_crash_are_sent_by_the_next_session(self):
        first = self.store()
        self.fake.down = True
        first.add_message("g1", message(1))
        self.assertEqual(first.close(0.5), 2)
        self.fake.down = False
        second = storage.SupabaseStore(CONFIG, session(), self.grants)
        self.addCleanup(lambda: second._stopped or second.close(1))
        self.assertTrue(second.flush(2))
        self.assertEqual([row["id"] for row in self.fake.tables["messages"]], ["m1"])

    def test_a_rejected_change_is_set_aside_and_the_rest_still_save(self):
        store = self.store()
        self.fake.reject = {"m2"}
        for index in range(5):
            store.add_message("g1", message(index))
        self.assertTrue(store.flush(2))
        self.assertEqual([row["id"] for row in self.fake.tables["messages"]], ["m0", "m1", "m3", "m4"])
        self.assertEqual(store.sync_status()["failed"], 1)
        self.assertIn("1 change could not be saved", store.error)

    def test_an_expired_sign_in_during_an_outage_is_retried_not_discarded(self):
        store = self.store(session(expires_in=-10))
        self.fake.down = True
        store.add_message("g1", message(1))
        with patch.object(storage.SupabaseAuth, "refresh", side_effect=storage.SupabaseError(0, "no network")):
            self.assertFalse(store.flush(2))
        self.assertEqual((store.sync_status()["pending"], store.sync_status()["failed"]), (2, 0))

    def test_an_image_waiting_to_upload_can_still_be_shown(self):
        store = self.store()
        self.fake.down = True
        name = store.save_image(PNG, "png")
        store.close(0.5)
        reopened = storage.SupabaseStore(CONFIG, session(), self.grants)
        self.addCleanup(lambda: reopened._stopped or reopened.close(0.5))
        self.assertEqual(reopened.load_image(name), PNG)
        self.fake.down = False
        self.assertTrue(reopened.flush(2))
        self.assertEqual(self.fake.objects[f"user-1/{name}"], PNG)


class AppSyncTests(unittest.TestCase):
    def test_sign_in_sends_leftovers_before_loading_and_sign_out_reports_what_waits(self):
        fake = FakeSupabase()
        with patch.object(storage, "_http", fake), tempfile.TemporaryDirectory() as temp, patch.object(app, "GRANTS_FILE", Path(temp) / "grants.json"):
            fake.down = True
            leftover = storage.SupabaseStore(CONFIG, session(), Path(temp) / "grants.json")
            leftover.put_group({"id": "g1", "name": "Studio", "bot_ids": [], "messages": []})
            leftover.add_message("g1", message(1))
            leftover.close(0.5)
            fake.down = False
            state = app.State(load=False)
            state.use_supabase(CONFIG)
            state.sign_in("me@example.com", "secret")
            self.assertEqual(([g["name"] for g in state.groups], state.groups[0]["message_count"]), (["Studio"], 1))
            self.assertEqual(state.public()["sync"]["state"], "saved")
            fake.down = True
            state.add_message(state.groups[0], message(2))
            with patch.object(storage.SupabaseStore, "close", lambda store, timeout=15: storage.SupabaseStore.flush(store, 0.5) and 0 or store.outbox.counts()[0]):
                self.assertEqual(state.sign_out(), 1)


if __name__ == "__main__":
    unittest.main()
