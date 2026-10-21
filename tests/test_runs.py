import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import core
import storage
from test_storage import CONFIG, FakeSupabase, session


def wait_for(condition, timeout=3):
    limit = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > limit:
            raise AssertionError("Timed out waiting for condition")
        time.sleep(0.01)


class RunManagerTests(unittest.TestCase):
    def test_only_the_worker_count_runs_at_once_and_the_rest_wait_in_order(self):
        manager = app.RunManager(2)
        release, lock = threading.Event(), threading.Lock()
        active, peak, started = [0], [0], []

        def job(name):
            def run():
                with lock:
                    started.append(name)
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                release.wait(2)
                with lock:
                    active[0] -= 1
            return run

        for name in "abcd":
            manager.submit(job(name))
        wait_for(lambda: len(started) == 2)
        time.sleep(0.05)
        self.assertEqual(sorted(started), ["a", "b"])
        release.set()
        manager.jobs.join()
        self.assertEqual((started[2:], peak[0]), (["c", "d"], 2))


class QueueAPITests(unittest.TestCase):
    def setUp(self):
        temp = Path(tempfile.mkdtemp())
        settings = patch.object(app, "SETTINGS_FILE", temp / "settings.json")
        settings.start()
        self.addCleanup(settings.stop)
        self.state = app.State()
        self.state.runner = app.RunManager(1)
        for name in ("Host", "Expert"):
            bot = core.blank_bot(name)
            bot.update(name=name, endpoint="http://127.0.0.1:9/model", model="fake")
            self.state.bots.append(bot)
            self.state.keys[name] = "test-key"
        self.first = self.state.groups[0]
        self.second = dict(app.general_group(), name="Second")
        self.state.groups.append(self.second)
        for group in (self.first, self.second):
            group["bot_ids"] = ["Host", "Expert"]
        state_patch = patch.object(app, "STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.prompts = []

        def model(bot, prompt, key):
            self.prompts.append(prompt)
            if "Group chat: General" in prompt:
                self.release.wait(3)
            return "Reply"

        def run(*args, **kwargs):
            return core.run_group_conversation(*args, **kwargs, model_call=model)

        conversation = patch.object(app, "run_group_conversation", side_effect=run)
        conversation.start()
        self.addCleanup(conversation.stop)

    def post(self, group, action, body):
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/api/groups/{group['id']}/{action}", data=json.dumps(body).encode(),
                                         headers={"X-Local-Token": self.state.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def status(self, run_id):
        return self.state.runs[run_id]["status"]

    def test_runs_beyond_the_worker_pool_queue_and_start_when_a_slot_frees(self):
        first = self.post(self.first, "send", {"text": "Go", "continuous": False})["run_id"]
        wait_for(lambda: self.status(first) == "running")
        second = self.post(self.second, "send", {"text": "Go", "continuous": False})["run_id"]
        time.sleep(0.1)
        self.assertEqual(self.status(second), "queued")
        self.release.set()
        wait_for(lambda: self.status(second) == "complete")
        self.assertEqual(self.status(first), "complete")
        self.assertIn("Group chat: Second", self.prompts[-1])

    def test_a_queued_group_cannot_start_twice_and_stop_cancels_it_before_it_runs(self):
        first = self.post(self.first, "send", {"text": "Go", "continuous": False})["run_id"]
        wait_for(lambda: self.status(first) == "running")
        second = self.post(self.second, "send", {"text": "Go", "continuous": False})["run_id"]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(self.second, "send", {"text": "Again", "continuous": False})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        stopped = self.post(self.second, "stop", {})
        self.assertEqual(next(run for run in stopped["runs"] if run["id"] == second)["status"], "stopped")
        self.assertNotIn(second, self.state.stop_events)
        self.release.set()
        wait_for(lambda: self.status(first) == "complete")
        self.state.runner.jobs.join()
        self.assertEqual(self.status(second), "stopped")
        self.assertFalse(any("Group chat: Second" in prompt for prompt in self.prompts))


class RunHistoryTests(unittest.TestCase):
    def test_local_runs_survive_a_restart_and_unfinished_ones_become_interrupted(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app, "SETTINGS_FILE", Path(temp) / "settings.json"):
            state = app.State()
            group_id = state.groups[0]["id"]
            state.runs["done"] = {"id": "done", "group_id": group_id, "status": "complete", "created_at": 1.0, "completed": 4}
            state.runs["live"] = {"id": "live", "group_id": group_id, "status": "running", "created_at": 2.0, "current_bot": "Host"}
            state.save()
            restarted = app.State()
            self.assertEqual(list(restarted.runs), ["done", "live"])
            self.assertEqual(restarted.runs["done"]["status"], "complete")
            self.assertEqual((restarted.runs["live"]["status"], restarted.runs["live"]["current_bot"]), ("interrupted", ""))
            saved = app.State().store.load(app.HISTORY_WINDOW)["runs"]
            self.assertEqual([run["status"] for run in saved], ["complete", "interrupted"])

    def test_supabase_runs_round_trip_and_interrupted_runs_are_saved_back(self):
        fake = FakeSupabase()
        with patch.object(storage, "_http", fake), tempfile.TemporaryDirectory() as temp:
            grants = Path(temp) / "grants.json"
            store = storage.SupabaseStore(CONFIG, session(), grants)
            store.put_group({"id": "g1", "name": "General", "bot_ids": [], "messages": []})
            store.put_run({"id": "r1", "group_id": "g1", "status": "complete", "created_at": 1.0})
            store.put_run({"id": "r2", "group_id": "g1", "status": "running", "created_at": 2.0})
            store.close()
            self.assertEqual([run["id"] for run in storage.SupabaseStore(CONFIG, session(), grants).load(200)["runs"]], ["r1", "r2"])
            with patch.object(app, "GRANTS_FILE", grants):
                state = app.State(load=False)
                state.use_supabase(CONFIG)
                state.sign_in("me@example.com", "secret")
                state.store.flush(2)
                self.assertEqual(state.runs["r2"]["status"], "interrupted")
                self.assertEqual(next(row for row in fake.tables["runs"] if row["id"] == "r2")["data"]["status"], "interrupted")
                state.close()

    def test_accounts_without_the_runs_table_can_still_sign_in(self):
        fake = FakeSupabase()
        fake.missing.add("runs")
        with patch.object(storage, "_http", fake), tempfile.TemporaryDirectory() as temp, patch.object(app, "GRANTS_FILE", Path(temp) / "grants.json"):
            state = app.State(load=False)
            state.use_supabase(CONFIG)
            state.sign_in("me@example.com", "secret")
            self.assertEqual([group["name"] for group in state.groups], ["General"])
            self.assertIn("schema.sql", state.public()["sync_error"])
            state.close()


class ModelCallLimitTests(unittest.TestCase):
    def test_simultaneous_model_calls_are_capped_across_runs(self):
        bot = core.blank_bot("b1")
        bot.update(name="Bot", endpoint="https://api.example.com/v1/x", model="m")
        lock, release = threading.Lock(), threading.Event()
        active, peak = [0], [0]

        def slow_call(*args):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            release.wait(2)
            with lock:
                active[0] -= 1
            return "ok"

        with patch.object(core, "MODEL_CALLS", threading.BoundedSemaphore(2)), patch.object(core.ChatCompletionsAdapter, "call", side_effect=slow_call):
            threads = [threading.Thread(target=core.call_bot, args=(bot, "Hi", "key")) for _ in range(5)]
            for thread in threads:
                thread.start()
            wait_for(lambda: active[0] == 2)
            time.sleep(0.05)
            self.assertEqual(active[0], 2)
            release.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(peak[0], 2)


if __name__ == "__main__":
    unittest.main()
