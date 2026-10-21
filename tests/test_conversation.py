import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import core
import workflow


class ConversationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.bots = [dict(core.blank_bot(name), name=name) for name in ("Host", "Expert")]

    def test_timed_run_wraps_order_and_passes_each_reply_forward(self):
        clock = [0]
        calls, messages = [], []

        def model(bot, prompt, key):
            calls.append((bot["name"], prompt))
            clock[0] += 1
            return f"Reply {len(calls)}"

        reason = core.run_group_conversation("Test", self.bots, [], [], {}, messages.append,
                                             duration_seconds=5, model_call=model, clock=lambda: clock[0])
        self.assertEqual(reason, "time_limit")
        self.assertEqual([m["sender"] for m in messages], ["Host", "Expert", "Host", "Expert"])
        self.assertIn("Reply 2", calls[2][1])
        self.assertIn('next bot scheduled in this round is "Host"', calls[1][1])
        self.assertEqual(len(calls), 5)

    def test_default_mode_still_runs_exactly_one_round(self):
        messages = []
        reason = core.run_group_conversation("Test", self.bots, [], [], {}, messages.append,
                                             model_call=lambda bot, prompt, key: bot["name"])
        self.assertEqual(reason, "one_round")
        self.assertEqual([m["sender"] for m in messages], ["Host", "Expert"])

    def test_continuous_mode_keeps_building_on_replies_until_stopped(self):
        stop = threading.Event()
        messages, prompts = [], []

        def model(bot, prompt, key):
            prompts.append(prompt)
            return f"Answer {len(prompts)}"

        def publish(message):
            messages.append(message)
            if len(messages) == 4:
                stop.set()

        reason = core.run_group_conversation("Test", self.bots, [], [], {}, publish,
                                             continuous=True, stop_event=stop, model_call=model)
        self.assertEqual(reason, "stopped")
        self.assertEqual([m["sender"] for m in messages], ["Host", "Expert", "Host", "Expert"])
        self.assertIn("Answer 2", prompts[2])
        self.assertIn("ongoing conversation between the bots", prompts[2])

    def test_stop_between_turns_prevents_another_call(self):
        stop = threading.Event()
        messages = []

        def publish(message):
            messages.append(message)
            stop.set()

        reason = core.run_group_conversation("Test", self.bots, [], [], {}, publish,
                                             duration_seconds=120, stop_event=stop,
                                             model_call=lambda bot, prompt, key: "Done")
        self.assertEqual(reason, "stopped")
        self.assertEqual(len(messages), 1)

    def test_stop_during_request_returns_promptly_and_discards_late_reply(self):
        entered, release, finished, stop = [threading.Event() for _ in range(4)]
        messages, reasons = [], []

        def model(bot, prompt, key):
            entered.set()
            release.wait(2)
            finished.set()
            return "Late reply"

        runner = threading.Thread(target=lambda: reasons.append(core.run_group_conversation(
            "Test", self.bots, [], [], {}, messages.append, duration_seconds=120,
            stop_event=stop, model_call=model)), daemon=True)
        runner.start()
        try:
            self.assertTrue(entered.wait(1))
            stop.set()
            runner.join(1)
            self.assertFalse(runner.is_alive())
            self.assertEqual(reasons, ["stopped"])
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(messages, [])

    def test_deadline_does_not_wait_for_slow_provider(self):
        release = threading.Event()
        messages = []
        try:
            reason = core.run_group_conversation("Test", self.bots, [], [], {}, messages.append,
                                                 duration_seconds=0.05,
                                                 model_call=lambda *args: release.wait(2) or "Late")
            self.assertEqual(reason, "time_limit")
            self.assertEqual(messages, [])
        finally:
            release.set()

    def test_provider_failure_does_not_start_another_turn(self):
        calls = []

        def model(bot, prompt, key):
            calls.append(bot["id"])
            raise core.UserError("Rate limited")

        with self.assertRaisesRegex(core.UserError, "Rate limited"):
            core.run_group_conversation("Test", self.bots, [], [], {}, lambda message: None,
                                        duration_seconds=120, model_call=model)
        self.assertEqual(len(calls), 1)

    def test_timed_rate_limit_retries_same_bot_without_adding_failed_reply(self):
        stop = threading.Event()
        calls, messages, waits = [], [], []

        def model(bot, prompt, key):
            calls.append(bot["name"])
            if len(calls) == 1:
                raise core.RateLimitError("Busy", retry_after=0.01)
            return "Recovered"

        def publish(message):
            messages.append(message)
            stop.set()

        reason = core.run_group_conversation("Test", self.bots, [], [], {}, publish,
                                             duration_seconds=2, stop_event=stop,
                                             model_call=model, on_wait=waits.append)
        self.assertEqual(reason, "stopped")
        self.assertEqual(calls, ["Host", "Host"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(waits), 1)

    def test_rate_limit_wait_cannot_extend_timer(self):
        calls = []

        def model(bot, prompt, key):
            calls.append(bot["id"])
            raise core.RateLimitError("Busy", retry_after=60)

        reason = core.run_group_conversation("Test", self.bots, [], [], {}, lambda message: self.fail("Unexpected reply"),
                                             duration_seconds=0.05, model_call=model)
        self.assertEqual(reason, "time_limit")
        self.assertEqual(len(calls), 1)

    def test_continuous_rate_limit_retries_without_a_deadline(self):
        stop = threading.Event()
        calls = []

        def model(bot, prompt, key):
            calls.append(bot["id"])
            if len(calls) == 1:
                raise core.RateLimitError("Busy", retry_after=0.01)
            return "Recovered"

        reason = core.run_group_conversation("Test", self.bots, [], [], {}, lambda message: stop.set(),
                                             continuous=True, stop_event=stop, model_call=model)
        self.assertEqual(reason, "stopped")
        self.assertEqual(calls, ["Host", "Host"])


class ConversationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings_patch = patch.object(app, "SETTINGS_FILE", Path(self.temp.name) / "settings.json")
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.state = app.State()
        for name in ("Host", "Expert"):
            bot = core.blank_bot(name)
            bot.update(name=name, endpoint="http://127.0.0.1:9/model", model="fake")
            self.state.bots.append(bot)
            self.state.keys[name] = "test-key"
        self.group = self.state.groups[0]
        self.group["bot_ids"] = ["Host", "Expert"]
        # The bots above were created in memory, so write them to storage like the API would.
        self.state.save()
        state_patch = patch.object(app, "STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, suffix, body):
        req = urllib.request.Request(self.base + f"/api/groups/{self.group['id']}/" + suffix,
                                     data=json.dumps(body).encode(),
                                     headers={"X-Local-Token": self.state.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.load(response)

    def test_stop_endpoint_is_immediate_and_keeps_late_result_out_of_history(self):
        entered, release = threading.Event(), threading.Event()

        def model(*args):
            entered.set()
            release.wait(2)
            return "Late reply"

        def run(*args, **kwargs):
            return core.run_group_conversation(*args, **kwargs, model_call=model)

        with patch.object(app, "run_group_conversation", side_effect=run):
            try:
                run_id = self.post("send", {"text": "Begin", "duration_seconds": 120})["run_id"]
                self.assertTrue(entered.wait(1))
                stopped = self.post("stop", {})
                self.assertEqual(stopped["runs"][0]["status"], "stopped")
                limit = time.monotonic() + 1
                while run_id in self.state.stop_events and time.monotonic() < limit:
                    time.sleep(0.01)
                self.assertNotIn(run_id, self.state.stop_events)
                self.assertEqual(len(self.group["messages"]), 1)
            finally:
                release.set()

    def test_invalid_duration_is_rejected_before_saving_message(self):
        for duration in (-1, 601, True, "120", 1.5):
            with self.subTest(duration=duration), self.assertRaises(urllib.error.HTTPError) as error:
                self.post("send", {"text": "Begin", "duration_seconds": duration})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertEqual(self.group["messages"], [])
        self.assertEqual(self.state.runs, {})

    def test_default_chat_request_starts_continuous_turns(self):
        calls = []

        def model(bot, prompt, key):
            calls.append(bot["name"])
            if len(calls) == 4:
                for stop in list(self.state.stop_events.values()):
                    stop.set()
            return "Reply"

        def run(*args, **kwargs):
            return core.run_group_conversation(*args, **kwargs, model_call=model)

        with patch.object(app, "run_group_conversation", side_effect=run):
            run_id = self.post("send", {"text": "Begin"})["run_id"]
            limit = time.monotonic() + 1
            while run_id in self.state.stop_events and time.monotonic() < limit:
                time.sleep(0.01)
        run = self.state.runs[run_id]
        self.assertTrue(run["continuous"])
        self.assertIsNone(run["ends_at"])
        self.assertEqual(run["status"], "stopped")
        self.assertGreater(run["completed"], 2)
        self.assertEqual(calls, ["Host", "Expert", "Host", "Expert"])

    def test_invalid_conversation_mode_is_rejected(self):
        for options in ({"continuous": "true"}, {"continuous": True, "duration_seconds": 120}):
            with self.subTest(options=options), self.assertRaises(urllib.error.HTTPError) as error:
                self.post("send", {"text": "Begin", **options})
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertEqual(self.group["messages"], [])

    def test_timed_mode_requires_two_bots(self):
        self.group["bot_ids"] = ["Host"]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post("send", {"text": "Begin", "duration_seconds": 120})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.group["messages"], [])

    def test_workflow_settings_are_saved_and_master_can_be_selected(self):
        self.post("settings", {"mode": "workflow", "master_id": "Expert", "quality_criteria": "Include test evidence"})
        reloaded = app.State()
        group = next(g for g in reloaded.groups if g["id"] == self.group["id"])
        self.assertEqual(group["mode"], "workflow")
        self.assertEqual(group["master_id"], "Expert")
        self.assertEqual(group["quality_criteria"], "Include test evidence")

    def test_workflow_api_finishes_and_persists_both_approvals(self):
        self.post("settings", {"mode": "workflow", "master_id": "Expert"})

        def model(bot, prompt, key, **kwargs):
            raw = prompt.split("Authoritative context for this workflow action:\n", 1)[1]
            context = json.JSONDecoder().raw_decode(raw)[0]
            if bot["id"] == "Expert":
                result = {"approved": True, "feedback": "Good", "submission_id": context["submission_id"]}
            elif "master_review" in context:
                result = {"accepted": True, "feedback": "Complete", "submission_id": context["submission_id"]}
            else:
                result = {"result": "Completed artifact", "ready": True, "needs_input": False}
            return json.dumps(result)

        def run(*args, **kwargs):
            return workflow.run_task_workflow(*args, **kwargs, model_call=model)

        with patch.object(app, "run_task_workflow", side_effect=run):
            run_id = self.post("send", {"text": "Build the artifact", "mode": "workflow"})["run_id"]
            limit = time.monotonic() + 2
            while run_id in self.state.stop_events and time.monotonic() < limit:
                time.sleep(0.01)
        run = self.state.runs[run_id]
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["completion_reason"], "workflow_complete")
        result = app.State().groups[0]["last_workflow"]["approved_results"][0]
        self.assertTrue(result["master_approved"])
        self.assertTrue(result["worker_confirmed"])
        self.assertEqual(result["result"], "Completed artifact")


if __name__ == "__main__":
    unittest.main()
