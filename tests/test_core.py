import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


class FileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        self.inside = self.desktop / "allowed.txt"
        self.inside.write_text("Allowed content", encoding="utf-8")
        self.outside = self.root / "private.txt"
        self.outside.write_text("Outside content", encoding="utf-8")

    def test_only_explicit_desktop_file_is_read(self):
        allowed = core.validate_desktop_file(str(self.inside), self.desktop)
        self.assertEqual(core.read_allowed_files([allowed], [allowed], self.desktop), [(allowed, "Allowed content")])
        with self.assertRaises(core.UserError):
            core.read_allowed_files([str(self.outside)], [allowed], self.desktop)
        with self.assertRaises(core.UserError):
            core.validate_desktop_file(str(self.outside), self.desktop)

    def test_symlink_escape_and_changed_grant_are_blocked(self):
        link = self.desktop / "link.txt"
        link.symlink_to(self.outside)
        with self.assertRaises(core.UserError):
            core.validate_desktop_file(str(link), self.desktop)
        allowed = core.validate_desktop_file(str(self.inside), self.desktop)
        self.inside.unlink()
        self.inside.symlink_to(self.outside)
        with self.assertRaises(core.UserError):
            core.read_allowed_files([allowed], [allowed], self.desktop)

    def test_large_and_binary_files_are_rejected(self):
        self.inside.write_bytes(b"x" * (core.MAX_FILE_BYTES + 1))
        allowed = core.validate_desktop_file(str(self.inside), self.desktop)
        with self.assertRaisesRegex(core.UserError, "128 KB"):
            core.read_allowed_files([allowed], [allowed], self.desktop)
        self.inside.write_bytes(b"binary\x00data")
        with self.assertRaisesRegex(core.UserError, "binary"):
            core.read_allowed_files([allowed], [allowed], self.desktop)


class BotAndConversationTests(unittest.TestCase):
    def bot(self, bot_id, name, api_format="chat_completions"):
        raw = core.blank_bot(bot_id)
        raw.update(name=name, role="Research", assignment="Give a useful handoff", endpoint="http://127.0.0.1:9999/model", model="test-model", api_format=api_format, auth_header="X-Api-Key", auth_prefix="")
        return core.validate_bot(raw, bot_id)

    def test_group_round_passes_prior_bot_message_and_selected_file(self):
        bots = [self.bot("one", "One"), self.bot("two", "Two")]
        calls = []
        added = []

        def fake_model(bot, prompt, key):
            calls.append((bot["name"], prompt, key))
            return "first finding" if bot["id"] == "one" else "final answer"

        result = core.run_group_round("Planning", bots, [{"sender": "You", "text": "Summarize this"}], [("/Desktop/note.txt", "file text")], {"one": "a", "two": "b"}, added.append, fake_model)
        self.assertEqual([message["text"] for message in result], ["first finding", "final answer"])
        self.assertEqual(calls[0][2], "a")
        self.assertEqual(calls[1][2], "b")
        self.assertIn("file text", calls[0][1])
        self.assertIn("first finding", calls[1][1])
        self.assertEqual(added, result)

    def test_three_common_api_shapes(self):
        samples = [
            ("chat_completions", {"choices": [{"message": {"content": "Chat text"}}]}, "Chat text"),
            ("responses", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Response text"}]}]}, "Response text"),
            ("messages", {"content": [{"type": "text", "text": "Message text"}]}, "Message text"),
        ]
        for api_format, response, expected in samples:
            with self.subTest(api_format=api_format):
                bot = self.bot("one", "One", api_format)
                with patch.object(core, "_post_json", return_value=response) as post:
                    self.assertEqual(core.call_bot(bot, "task", "secret"), expected)
                    self.assertEqual(post.call_args.args[0], bot["endpoint"])
                    self.assertEqual(post.call_args.args[1]["X-Api-Key"], "secret")

    def test_custom_json_template_and_response_path(self):
        bot = self.bot("one", "One", "custom")
        bot["request_template"] = '{"model":"{{model}}","query":"{{prompt}}","role":"{{role}}"}'
        bot["response_path"] = "results.0.answer"
        with patch.object(core, "_post_json", return_value={"results": [{"answer": "Custom text"}]}) as post:
            self.assertEqual(core.call_bot(bot, "hello", "secret"), "Custom text")
            self.assertEqual(post.call_args.args[2]["query"], "hello")

    def test_remote_http_endpoint_is_rejected(self):
        bot = core.blank_bot("one")
        bot["endpoint"] = "http://example.com/v1/model"
        with self.assertRaisesRegex(core.UserError, "HTTPS"):
            core.validate_bot(bot, "one")


if __name__ == "__main__":
    unittest.main()
