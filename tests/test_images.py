import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import core
import workflow


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class ImageGenerationTests(unittest.TestCase):
    def bot(self, bot_id="artist", endpoint="https://chat.example.edu/api/v1/images/generations", model="dall-e-2"):
        raw = core.blank_bot(bot_id)
        raw.update(name="Artist", endpoint=endpoint, model=model, api_format="image_generation")
        return core.validate_bot(raw, bot_id)

    def test_open_webui_file_link_is_downloaded_from_the_same_server_with_the_key(self):
        bot = self.bot()
        response = [{"id": "abc", "url": "/api/v1/files/abc/content", "content_type": "image/png"}]
        with patch.object(core, "_post_json", return_value=response) as post, patch.object(core, "_get_bytes", return_value=PNG) as get:
            output = core.call_bot(bot, "A red paper boat", "test-key")
        body = post.call_args.args[2]
        self.assertEqual(body, {"model": "dall-e-2", "prompt": "A red paper boat", "n": 1})
        self.assertTrue(post.call_args.kwargs["allow_list"])
        url, headers, _ = get.call_args.args
        self.assertEqual(url, "https://chat.example.edu/api/v1/files/abc/content")
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(output, {"text": "A red paper boat", "image": PNG, "image_type": "png"})

    def test_image_links_on_another_host_never_receive_the_api_key(self):
        bot = self.bot()
        with patch.object(core, "_post_json", return_value={"data": [{"url": "https://cdn.example.com/image.png"}]}), patch.object(core, "_get_bytes", return_value=PNG) as get:
            core.call_bot(bot, "A boat", "test-key")
        self.assertEqual(get.call_args.args[1], {})

    def test_base64_images_are_decoded(self):
        bot = self.bot(model="gpt-image-1.5")
        with patch.object(core, "_post_json", return_value={"data": [{"b64_json": base64.b64encode(PNG).decode()}]}):
            self.assertEqual(core.call_bot(bot, "A boat", "test-key")["image"], PNG)

    def test_non_image_downloads_are_rejected(self):
        bot = self.bot()
        with patch.object(core, "_post_json", return_value=[{"url": "/api/v1/files/abc/content"}]), patch.object(core, "_get_bytes", return_value=b"<html>"):
            with self.assertRaisesRegex(core.UserError, "not a supported image"):
                core.call_bot(bot, "A boat", "test-key")

    def test_dall_e_2_prompt_is_cut_to_its_limit(self):
        bot = self.bot()
        with patch.object(core, "_post_json", return_value=[{"url": "/f"}]) as post, patch.object(core, "_get_bytes", return_value=PNG):
            core.call_bot(bot, "x" * 5000, "test-key")
        self.assertEqual(len(post.call_args.args[2]["prompt"]), 1000)

    def test_fastapi_validation_errors_are_readable(self):
        raw = b'{"detail":[{"type":"missing","loc":["body","prompt"],"msg":"Field required"}]}'
        self.assertEqual(core._error_message(raw), "prompt: Field required")
        self.assertEqual(core._error_message(b'{"detail":"Not allowed"}'), "Not allowed")
        self.assertEqual(core._error_message(b'{"error":{"message":"Bad key"}}'), "Bad key")
        self.assertEqual(core._error_message(b"oops"), "Request failed")

    def test_conversation_sends_only_the_latest_other_message_and_keeps_the_image(self):
        writer = dict(core.blank_bot("writer"), name="Writer")
        artist = self.bot()
        prompts, messages = [], []

        def model(bot, prompt, key):
            prompts.append(prompt)
            if bot["id"] == "writer":
                return "A lighthouse at dusk"
            return {"text": prompt, "image": PNG, "image_type": "png"}

        core.run_group_conversation("Studio", [writer, artist], [{"sender": "You", "text": "Begin"}], [], {}, messages.append, model_call=model)
        self.assertEqual(prompts[1], "A lighthouse at dusk")
        self.assertEqual(messages[1]["image"], PNG)
        self.assertEqual(messages[1]["text"], "A lighthouse at dusk")

    def test_image_bots_cannot_join_task_workflows(self):
        master = dict(core.blank_bot("master"), name="Master")
        with self.assertRaisesRegex(core.UserError, "Image generation bots"):
            workflow.run_task_workflow("Studio", master, [self.bot()], [], [], {}, lambda message: None)


class ImageStorageTests(unittest.TestCase):
    def test_saved_images_survive_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / "settings.json"
            with patch.object(app, "SETTINGS_FILE", settings), patch.object(app, "IMAGES_DIR", Path(temp) / "images"):
                name = app.save_image(PNG, "png")
                self.assertTrue(app.IMAGE_NAME.fullmatch(name))
                self.assertEqual((Path(temp) / "images" / name).read_bytes(), PNG)
                self.assertEqual((Path(temp) / "images" / name).stat().st_mode & 0o777, 0o600)
                state = app.State()
                state.groups[0]["messages"].append({"id": "m", "sender": "Artist", "bot_id": "a", "text": "Boat", "created_at": "", "files": [], "image": name})
                state.groups[0]["messages"].append({"id": "n", "sender": "Artist", "bot_id": "a", "text": "Bad", "created_at": "", "files": [], "image": "../settings.json"})
                state.save()
                reloaded = app.State().groups[0]["messages"]
                self.assertEqual(reloaded[0]["image"], name)
                self.assertNotIn("image", reloaded[1])


if __name__ == "__main__":
    unittest.main()
