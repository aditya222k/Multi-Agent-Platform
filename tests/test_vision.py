import base64
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PIL import Image
except ImportError:  # These tests build real images; without Pillow the app's own fallback applies instead.
    raise unittest.SkipTest("Pillow is not installed")

import app
import core


def png(width=1200, height=900, color=(200, 80, 40)):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, "PNG")
    return output.getvalue()


def bot(api_format="chat_completions", **settings):
    raw = core.blank_bot(settings.pop("bot_id", "judge"))
    raw.update(name=settings.pop("name", "Judge"), endpoint="https://api.example.com/v1/x", model="m", api_format=api_format, **settings)
    return core.validate_bot(raw, raw["id"])


class SettingsTests(unittest.TestCase):
    def test_abilities_default_off_and_must_be_true_or_false(self):
        self.assertEqual((bot()["vision"], bot()["can_finish"]), (False, False))
        with self.assertRaisesRegex(core.UserError, "vision"):
            bot(vision="yes")

    def test_only_formats_that_accept_images_can_see_them(self):
        for api_format in ("chat_completions", "responses", "messages"):
            self.assertTrue(bot(api_format, vision=True)["vision"])
        for api_format in ("custom", "image_generation"):
            with self.subTest(api_format=api_format), self.assertRaisesRegex(core.UserError, "can see images"):
                bot(api_format, vision=True)


class AttachmentTests(unittest.TestCase):
    image = {"data": b"\xff\xd8\xffjpeg-bytes", "media_type": "image/jpeg"}

    def sent(self, api_format, reply):
        with patch.object(core, "_post_json", return_value=reply) as post:
            core.call_bot(bot(api_format, vision=True), "Judge this", "key", image=self.image)
        return post.call_args.args[2]

    def test_each_format_attaches_the_picture_its_own_way(self):
        url = "data:image/jpeg;base64," + base64.b64encode(self.image["data"]).decode()
        chat = self.sent("chat_completions", {"choices": [{"message": {"content": "ok"}}]})
        self.assertEqual(chat["messages"][1]["content"], [{"type": "text", "text": "Judge this"}, {"type": "image_url", "image_url": {"url": url}}])
        responses = self.sent("responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]})
        self.assertEqual(responses["input"][0]["content"][1], {"type": "input_image", "image_url": url})
        messages = self.sent("messages", {"content": [{"type": "text", "text": "ok"}]})
        self.assertEqual(messages["messages"][0]["content"][0]["source"], {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(self.image["data"]).decode()})

    def test_formats_without_image_support_refuse_a_picture(self):
        custom = bot("custom", request_template='{"q":"{{prompt}}"}', response_path="a")
        with patch.object(core, "_post_json") as post, self.assertRaisesRegex(core.UserError, "cannot receive images"):
            core.call_bot(custom, "Hi", "key", image=self.image)
        post.assert_not_called()


class PreparationTests(unittest.TestCase):
    def setUp(self):
        core._prepared.clear()

    def test_pillow_shrinks_to_a_small_jpeg_once(self):
        original = png()
        prepared = core.prepare_image(original)
        with Image.open(io.BytesIO(prepared["data"])) as shrunk:
            self.assertEqual((shrunk.format, max(shrunk.size)), ("JPEG", 512))
        self.assertLess(len(prepared["data"]), len(original))
        with patch.object(core.PILImage, "open", side_effect=AssertionError("decoded twice")):
            self.assertIs(core.prepare_image(original), prepared)

    def test_without_pillow_small_images_go_as_they_are_and_large_ones_explain(self):
        small = png(64, 64)
        with patch.object(core, "PILImage", None):
            self.assertEqual(core.prepare_image(small), {"data": small, "media_type": "image/png"})
            with patch.object(core, "MAX_UNSHRUNK_IMAGE_BYTES", 10), self.assertRaisesRegex(core.UserError, "Install Pillow"):
                core.prepare_image(png(65, 65))


class ConversationTests(unittest.TestCase):
    def setUp(self):
        core._prepared.clear()
        self.artist = bot("image_generation", bot_id="artist", name="Artist")
        self.judge = bot(bot_id="judge", name="Judge", vision=True, can_finish=True)
        self.writer = bot(bot_id="writer", name="Writer")

    def test_the_judge_sees_the_newest_picture_and_approval_ends_the_run(self):
        calls = []

        def model(current, prompt, key, **extra):
            calls.append((current["id"], prompt, extra.get("image")))
            if current["id"] == "writer":
                return "A red boat at dawn"
            if current["id"] == "artist":
                return {"text": prompt, "image": png(), "image_type": "png"}
            return "Matches the request.\n[[DONE]]" if len(calls) > 3 else "Make the boat bigger."

        messages = []
        reason = core.run_group_conversation("Studio", [self.writer, self.artist, self.judge], [{"sender": "You", "text": "Paint a boat"}],
                                             [], {}, messages.append, continuous=True, model_call=model)
        self.assertEqual(reason, "approved")
        judged = [call for call in calls if call[0] == "judge"]
        self.assertEqual(len(judged), 2)
        self.assertEqual(judged[0][2]["media_type"], "image/jpeg")
        self.assertIn('posted by "Artist"', judged[0][1])
        self.assertIn("A red boat at dawn", judged[0][1])
        self.assertIn("[[DONE]]", judged[0][1])
        self.assertTrue(all(call[2] is None for call in calls if call[0] != "judge"))
        self.assertEqual((messages[-1]["text"], messages[-1].get("approved")), ("Matches the request.", True))
        self.assertNotIn("approved", messages[-2])

    def test_a_saved_picture_is_loaded_and_missing_ones_are_said_plainly(self):
        seen, loaded = [], []

        def model(current, prompt, key, **extra):
            seen.append((prompt, extra.get("image")))
            return "Looks fine"

        history = [{"sender": "Artist", "bot_id": "artist", "text": "A boat", "image": "a" * 32 + ".png"}]
        core.run_group_conversation("Studio", [self.judge], history, [], {}, lambda message: None, model_call=model,
                                    load_image=lambda name: loaded.append(name) or png())
        self.assertEqual((loaded, seen[0][1]["media_type"]), (["a" * 32 + ".png"], "image/jpeg"))
        core.run_group_conversation("Studio", [self.judge], [{"sender": "You", "text": "Hi"}], [], {}, lambda message: None, model_call=model)
        self.assertIsNone(seen[1][1])
        self.assertIn("No picture has been posted", seen[1][0])
        self.assertIn("Do not write [[DONE]] before a picture exists", seen[1][0])

    def test_only_a_bot_allowed_to_finish_can_end_the_run(self):
        talker = bot(bot_id="talker", name="Talker")
        stop = threading.Event()
        replies = []

        def model(current, prompt, key, **extra):
            replies.append(current["id"])
            if len(replies) >= 4:
                stop.set()
            return "I think we are done [[DONE]]"

        messages = []
        reason = core.run_group_conversation("Studio", [talker, self.writer], [{"sender": "You", "text": "Go"}], [], {}, messages.append,
                                             continuous=True, stop_event=stop, model_call=model)
        self.assertEqual(reason, "stopped")
        self.assertIn("[[DONE]]", messages[0]["text"])

    def test_requests_without_a_picture_keep_their_old_shape(self):
        with patch.object(core, "_post_json", return_value={"choices": [{"message": {"content": "ok"}}]}) as post:
            core.call_bot(self.judge, "Hi", "key")
        self.assertEqual(post.call_args.args[2]["messages"][1]["content"], "Hi")


class RunRecordTests(unittest.TestCase):
    def test_the_run_counts_images_and_records_who_approved(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app, "SETTINGS_FILE", Path(temp) / "settings.json"), \
                patch.object(app, "IMAGES_DIR", Path(temp) / "images"):
            state = app.State()
            artist = dict(core.blank_bot("artist"), name="Artist", endpoint="http://127.0.0.1:9/x", model="m", api_format="image_generation")
            judge = dict(core.blank_bot("judge"), name="Judge", endpoint="http://127.0.0.1:9/x", model="m", vision=True, can_finish=True)
            state.bots += [artist, judge]
            state.groups[0]["bot_ids"] = ["artist", "judge"]
            state.keys.update({"artist": "k", "judge": "k"})
            state.save()

            def model(current, prompt, key, **extra):
                if current["id"] == "artist":
                    return {"text": "A boat", "image": png(), "image_type": "png"}
                return "Great.\n[[DONE]]"

            def run(*args, **kwargs):
                return core.run_group_conversation(*args, **kwargs, model_call=model)

            with patch.object(app, "STATE", state), patch.object(app, "run_group_conversation", side_effect=run):
                handler = app.Handler.__new__(app.Handler)
                handler._json = lambda status, body: None
                handler._send_group_message(state.groups[0]["id"], {"text": "Paint a boat", "continuous": True})
                limit = time.monotonic() + 5
                while state.stop_events and time.monotonic() < limit:
                    time.sleep(0.02)
            record = next(iter(state.runs.values()))
            approving = state.groups[0]["messages"][-1]
            self.assertEqual((record["status"], record["completion_reason"], record["images"], record["approved_by"]), ("complete", "approved", 1, "Judge"))
            self.assertEqual((record["approved_message_id"], approving["text"]), (approving["id"], "Great."))
            saved = state.store.load(app.HISTORY_WINDOW)["runs"][-1]
            self.assertEqual((saved["completion_reason"], saved["images"]), ("approved", 1))
            state.close()


if __name__ == "__main__":
    unittest.main()
