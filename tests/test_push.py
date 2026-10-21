import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import core


class EventHubTests(unittest.TestCase):
    def test_events_are_numbered_and_read_after_a_position(self):
        hub = app.EventHub()
        hub.publish("run", {"id": "r1"})
        hub.publish("message", {"text": "hi"})
        self.assertEqual(hub.latest(), 2)
        self.assertEqual([(i, kind, json.loads(data)) for i, kind, data in hub.since(1)], [(2, "message", {"text": "hi"})])
        self.assertEqual(hub.since(2), [])

    def test_events_are_copied_when_published(self):
        hub = app.EventHub()
        run = {"status": "running"}
        hub.publish("run", run)
        run["status"] = "complete"
        self.assertEqual(json.loads(hub.since(0)[0][2]), {"status": "running"})

    def test_a_reader_that_fell_behind_is_told_to_resync(self):
        hub = app.EventHub(keep=2)
        for index in range(5):
            hub.publish("run", {"n": index})
        self.assertIsNone(hub.since(1))
        self.assertEqual(len(hub.since(3)), 2)

    def test_wait_wakes_on_publish(self):
        hub = app.EventHub()
        threading.Timer(0.05, hub.publish, args=("state", {})).start()
        started = time.monotonic()
        hub.wait(0, 2)
        self.assertLess(time.monotonic() - started, 1)


class PushAPITests(unittest.TestCase):
    def setUp(self):
        temp = Path(tempfile.mkdtemp())
        settings = patch.object(app, "SETTINGS_FILE", temp / "settings.json")
        settings.start()
        self.addCleanup(settings.stop)
        self.state = app.State()
        for name in ("Host", "Expert"):
            bot = core.blank_bot(name)
            bot.update(name=name, endpoint="http://127.0.0.1:9/model", model="fake")
            self.state.bots.append(bot)
            self.state.keys[name] = "test-key"
        self.group = self.state.groups[0]
        self.group["bot_ids"] = ["Host", "Expert"]
        state_patch = patch.object(app, "STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.state.events.close)
        self.port = self.server.server_port

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=None if body is None else json.dumps(body),
                           headers={"X-Local-Token": self.state.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def open_stream(self, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", f"/api/events?token={token or self.state.token}")
        response = connection.getresponse()
        self.addCleanup(connection.close)
        return response

    def read_events(self, response, until, limit=50):
        events, current = [], {}
        while len(events) < limit:
            line = response.fp.readline().decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                current["event"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and "event" in current:
                events.append(current)
                if until(current):
                    return events
                current = {}
        return events

    def test_state_carries_message_counts_instead_of_message_bodies(self):
        self.group["messages"] = [{"id": f"m{i}", "sender": "You", "bot_id": "", "text": str(i), "created_at": "", "files": []} for i in range(3)]
        group = self.request("GET", "/api/state")[1]["groups"][0]
        self.assertNotIn("messages", group)
        self.assertEqual(group["message_count"], 3)

    def test_messages_can_be_read_latest_first_newer_than_or_older_than_a_message(self):
        self.group["messages"] = [{"id": f"m{i}", "sender": "You", "bot_id": "", "text": str(i), "created_at": "", "files": []} for i in range(10)]
        base = f"/api/groups/{self.group['id']}/messages"
        latest = self.request("GET", base + "?limit=4")[1]
        self.assertEqual(([m["id"] for m in latest["messages"]], latest["has_more"], latest["total"]), (["m6", "m7", "m8", "m9"], True, 10))
        older = self.request("GET", base + "?limit=4&before=m6")[1]
        self.assertEqual(([m["id"] for m in older["messages"]], older["has_more"]), (["m2", "m3", "m4", "m5"], True))
        newer = self.request("GET", base + "?after=m7")[1]
        self.assertEqual(([m["id"] for m in newer["messages"]], newer["reset"]), (["m8", "m9"], False))
        stale = self.request("GET", base + "?after=gone&limit=2")[1]
        self.assertEqual(([m["id"] for m in stale["messages"]], stale["reset"]), (["m8", "m9"], True))
        self.assertEqual(self.request("GET", "/api/groups/nope/messages")[0], 404)
        self.assertEqual(self.request("GET", base + "?limit=lots")[0], 400)

    def test_stream_requires_the_launch_token(self):
        response = self.open_stream(token="wrong")
        self.assertEqual(response.status, 403)

    def test_a_run_is_pushed_as_it_happens(self):
        def model(bot, prompt, key):
            return f"{bot['name']} reply"

        def run(*args, **kwargs):
            return core.run_group_conversation(*args, **kwargs, model_call=model)

        stream = self.open_stream()
        self.assertEqual((stream.status, stream.getheader("Content-Type")), (200, "text/event-stream; charset=utf-8"))
        with patch.object(app, "run_group_conversation", side_effect=run):
            status, _ = self.request("POST", f"/api/groups/{self.group['id']}/send", {"text": "Begin", "continuous": False})
            self.assertEqual(status, 202)
            events = self.read_events(stream, lambda event: event["event"] == "run" and event["data"]["run"]["status"] not in app.ACTIVE)
        messages = [event["data"] for event in events if event["event"] == "message"]
        self.assertEqual([m["message"]["text"] for m in messages], ["Begin", "Host reply", "Expert reply"])
        self.assertEqual([m["message_count"] for m in messages], [1, 2, 3])
        runs = [event["data"]["run"] for event in events if event["event"] == "run"]
        self.assertEqual([runs[0]["status"], runs[1]["status"]], ["queued", "running"])
        self.assertIn("Host", [run["current_bot"] for run in runs])
        self.assertEqual((runs[-1]["status"], runs[-1]["completed"]), ("complete", 2))

    def test_other_changes_tell_pages_to_refresh_state(self):
        stream = self.open_stream()
        self.request("POST", f"/api/groups/{self.group['id']}/rename", {"name": "Studio"})
        events = self.read_events(stream, lambda event: event["event"] == "state")
        self.assertEqual(events[-1]["event"], "state")


class SignedOutStreamTests(unittest.TestCase):
    def test_stream_needs_a_signed_in_account_with_supabase(self):
        state = app.State()
        state.use_supabase({"url": "https://project.supabase.co", "key": "publishable"})
        with patch.object(app, "STATE", state):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", f"/api/events?token={state.token}")
                response = connection.getresponse()
                self.assertEqual((response.status, json.loads(response.read())["login_required"]), (401, True))
                connection.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
