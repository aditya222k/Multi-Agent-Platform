import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import core
import storage
from test_storage import CONFIG, FakeSupabase, session


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def message(index, group, image=None):
    item = {"id": f"{group}-m{index}", "sender": "You", "bot_id": "", "text": f"Message {index}", "created_at": "", "files": []}
    if image:
        item["image"] = image
    return item


class LocalClearTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        for name, value in (("SETTINGS_FILE", self.temp / "settings.json"), ("IMAGES_DIR", self.temp / "images"), ("HISTORY_WINDOW", 3)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = app.State()
        self.addCleanup(self.state.close)
        self.studio = self.state.groups[0]
        self.other = dict(app.general_group(), name="Other")
        self.state.groups.append(self.other)
        self.state.store.put_group(self.other)
        # Six messages so some are only in storage, beyond the three-message memory window.
        for index in range(6):
            name = self.state.store.save_image(PNG, "png") if index in (0, 5) else None
            self.state.add_message(self.studio, message(index, "studio", name))
        self.kept_image = self.state.store.save_image(PNG, "png")
        self.state.add_message(self.other, message(0, "other", self.kept_image))
        patcher = patch.object(app, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.state.events.close)

    def call(self, group, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("POST", f"/api/groups/{group['id']}/clear", body=json.dumps(body),
                           headers={"X-Local-Token": self.state.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_clearing_needs_confirmation(self):
        status, body = self.call(self.studio, {})
        self.assertEqual((status, "Confirm" in body["error"], self.studio["message_count"]), (400, True, 6))

    def test_a_running_chat_must_be_stopped_first(self):
        self.state.runs["r1"] = {"id": "r1", "group_id": self.studio["id"], "status": "running"}
        status, body = self.call(self.studio, {"confirm": True})
        self.assertEqual((status, "Stop the conversation" in body["error"]), (400, True))

    def test_clearing_removes_every_message_and_image_in_that_chat_only(self):
        status, body = self.call(self.studio, {"confirm": True})
        self.assertEqual((status, body["cleared"]), (200, 6))
        self.assertEqual([(g["name"], g["message_count"]) for g in body["groups"]], [("General", 0), ("Other", 1)])
        self.assertEqual((self.studio["messages"], self.studio["message_count"]), ([], 0))
        rows = self.state.store._read("select group_id, count(*) from messages group by group_id")
        self.assertEqual(rows, [(self.other["id"], 1)])
        self.assertEqual(sorted(path.name for path in (self.temp / "images").iterdir()), [self.kept_image])
        events = [(kind, json.loads(data)) for _, kind, data in self.state.events.since(0)]
        self.assertIn(("cleared", {"group_id": self.studio["id"]}), events)
        restarted = app.State()
        self.assertEqual([g["message_count"] for g in restarted.groups], [0, 1])
        restarted.close()


class SupabaseClearTests(unittest.TestCase):
    def test_a_clear_queued_during_an_outage_lands_after_the_messages_it_clears(self):
        fake = FakeSupabase()
        with patch.object(storage, "_http", fake):
            store = storage.SupabaseStore(CONFIG, session(), Path(tempfile.mkdtemp()) / "grants.json")
            store.put_group({"id": "g1", "name": "Studio", "bot_ids": [], "messages": []})
            store.put_group({"id": "g2", "name": "Other", "bot_ids": [], "messages": []})
            old_image = store.save_image(PNG, "png")
            store.add_message("g1", message(0, "g1", old_image))
            store.add_message("g2", message(0, "g2"))
            self.assertTrue(store.flush(2))
            self.assertEqual(store.group_images("g1"), {old_image})
            fake.down = True
            new_image = store.save_image(PNG, "png")
            store.add_message("g1", message(1, "g1", new_image))  # Still waiting to sync when the chat is cleared.
            store.clear_messages("g1", {old_image, new_image})
            self.assertFalse(store.flush(1))
            fake.down = False
            self.assertTrue(store.flush(3))
            self.assertEqual([row["group_id"] for row in fake.tables["messages"]], ["g2"])
            self.assertEqual(fake.objects, {})
            self.assertEqual(store.sync_status()["failed"], 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
