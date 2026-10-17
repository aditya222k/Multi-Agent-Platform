import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core


class TLSConfigurationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and Path("/etc/ssl/cert.pem").is_file(), "macOS certificate bundle required")
    def test_empty_default_trust_uses_system_cas_with_verification_enabled(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch("core.ssl.create_default_context", return_value=context), patch.dict("core.os.environ", {}, clear=True):
            configured = core._tls_context()
        self.assertGreater(len(configured.get_ca_certs()), 0)
        self.assertEqual(configured.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(configured.check_hostname)

    def test_explicit_certificate_configuration_is_preserved(self):
        for variable in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            with self.subTest(variable=variable):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                with patch("core.ssl.create_default_context", return_value=context), patch.dict("core.os.environ", {variable: "/custom/trust"}, clear=True):
                    configured = core._tls_context()
                self.assertEqual(configured.get_ca_certs(), [])
                self.assertEqual(configured.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(configured.check_hostname)


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

    def test_assignment_selects_focused_work_instead_of_general_help(self):
        bot = self.bot("one", "Reviewer")
        bot["assignment"] = "Review the supplied budget and flag missing costs."
        instructions = core.bot_instructions(bot)
        self.assertIn(bot["assignment"], instructions)
        self.assertIn("Task mode: assigned.", instructions)
        self.assertNotIn("Task mode: general help", instructions)
        self.assertNotIn("Contribute to the group task.", instructions)

    def test_only_empty_or_whitespace_assignment_allows_general_help(self):
        for assignment in ("", " \n\t "):
            with self.subTest(assignment=assignment):
                bot = self.bot("one", "Planner")
                bot["assignment"] = assignment
                instructions = core.bot_instructions(bot)
                self.assertIn("Task mode: general help", instructions)
                self.assertNotIn("Task mode: assigned.", instructions)
                self.assertIn("Do not invent a project", instructions)

    def test_group_calls_keep_bot_identities_assignments_and_handoffs_separate(self):
        bots = [self.bot("one", "Planner"), self.bot("two", "Reviewer")]
        bots[0]["assignment"] = "Draft a release plan."
        bots[1]["assignment"] = "Review the plan for risks."
        responses = [{"choices": [{"message": {"content": text}}]} for text in ("Planner's result", "Reviewer's result")]
        with patch.object(core, "_post_json", side_effect=responses) as post:
            core.run_group_round("Release", bots, [{"sender": "You", "text": "Proceed."}], [], {"one": "test-key", "two": "test-key"}, lambda message: None)
        first, second = [call.args[2]["messages"] for call in post.call_args_list]
        self.assertIn('Your name is "Planner"', first[0]["content"])
        self.assertIn('Your name is "Reviewer"', second[0]["content"])
        self.assertIn(bots[1]["assignment"], second[0]["content"])
        self.assertNotIn(bots[0]["assignment"], second[0]["content"])
        self.assertIn('next bot scheduled in this round is "Reviewer"', first[1]["content"])
        self.assertIn('This turn belongs only to "Reviewer"', second[1]["content"])
        self.assertIn('"speaker": "Planner", "source": "bot"', second[1]["content"])
        self.assertIn("Planner's result", second[1]["content"])
        self.assertIn("last bot scheduled", second[1]["content"])

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
                    self.assertEqual(core.call_bot(bot, "task", "secret", instruction_context="Workflow control protocol"), expected)
                    self.assertEqual(post.call_args.args[0], bot["endpoint"])
                    self.assertEqual(post.call_args.args[1]["X-Api-Key"], "secret")
                    body = post.call_args.args[2]
                    self.assertEqual(body.get("max_tokens", body.get("max_output_tokens")), 4096)
                    instructions = body["instructions"] if api_format == "responses" else body["system"] if api_format == "messages" else body["messages"][0]["content"]
                    self.assertIn(bot["assignment"], instructions)
                    self.assertIn("Task mode: assigned.", instructions)
                    self.assertIn("Workflow control protocol", instructions)

    def test_custom_json_template_and_response_path(self):
        bot = self.bot("one", "One", "custom")
        bot["request_template"] = '{"model":"{{model}}","query":"{{prompt}}","role":"{{role}}"}'
        bot["response_path"] = "results.0.answer"
        with patch.object(core, "_post_json", return_value={"results": [{"answer": "Custom text"}]}) as post:
            self.assertEqual(core.call_bot(bot, "hello", "secret"), "Custom text")
            query = post.call_args.args[2]["query"]
            self.assertIn("Task mode: assigned.", query)
            self.assertIn(bot["assignment"], query)
            self.assertTrue(query.endswith("hello"))

    def test_custom_template_with_system_keeps_task_instructions_out_of_query(self):
        bot = self.bot("one", "One", "custom")
        bot["request_template"] = '{"instructions":"{{system}}","query":"{{prompt}}"}'
        bot["response_path"] = "answer"
        with patch.object(core, "_post_json", return_value={"answer": "Done"}) as post:
            self.assertEqual(core.call_bot(bot, "hello", "secret"), "Done")
            body = post.call_args.args[2]
            self.assertIn(bot["assignment"], body["instructions"])
            self.assertEqual(body["query"], "hello")

    def test_custom_template_cannot_silently_omit_bot_instructions(self):
        bot = self.bot("one", "One", "custom")
        bot["request_template"] = '{"query":"An unrelated hardcoded task"}'
        bot["response_path"] = "answer"
        with patch.object(core, "_post_json") as post:
            with self.assertRaisesRegex(core.UserError, "must include"):
                core.call_bot(bot, "hello", "test-key")
            post.assert_not_called()

    def test_remote_http_endpoint_is_rejected(self):
        bot = core.blank_bot("one")
        bot["endpoint"] = "http://example.com/v1/model"
        with self.assertRaisesRegex(core.UserError, "HTTPS"):
            core.validate_bot(bot, "one")


if __name__ == "__main__":
    unittest.main()
