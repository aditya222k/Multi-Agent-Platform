import re
import unittest
from pathlib import Path
from unittest.mock import patch

import core


ROOT = Path(__file__).resolve().parent.parent


def bot(api_format, **settings):
    raw = core.blank_bot("b1")
    raw.update(name="Bot", endpoint="https://api.example.com/v1/x", model="m", api_format=api_format, **settings)
    return core.validate_bot(raw, "b1")


class AdapterTests(unittest.TestCase):
    def test_every_format_has_an_adapter_and_unknown_formats_are_rejected(self):
        self.assertEqual(set(core.ADAPTERS), {"chat_completions", "responses", "messages", "custom", "image_generation"})
        with self.assertRaisesRegex(core.UserError, "supported API format"):
            bot("smoke_signals")

    def test_capabilities_live_on_the_adapter(self):
        text = core.ADAPTERS["chat_completions"]
        image = core.ADAPTERS["image_generation"]
        self.assertTrue(text.sends_history and text.joins_ongoing_talk and text.joins_workflows)
        self.assertFalse(image.sends_history or image.joins_ongoing_talk or image.joins_workflows)

    def test_each_text_adapter_builds_its_own_request_and_reads_its_own_reply(self):
        cases = {
            "chat_completions": ("messages", {"choices": [{"message": {"content": "A"}}]}),
            "responses": ("instructions", {"output": [{"type": "message", "content": [{"type": "output_text", "text": "A"}]}]}),
            "messages": ("system", {"content": [{"type": "text", "text": "A"}]}),
        }
        for api_format, (field, reply) in cases.items():
            with self.subTest(api_format=api_format), patch.object(core, "_post_json", return_value=reply) as post:
                self.assertEqual(core.call_bot(bot(api_format), "Hi", "key"), "A")
                self.assertIn(field, post.call_args.args[2])
                self.assertEqual(post.call_args.args[1]["Authorization"], "Bearer key")

    def test_a_reply_cut_off_while_thinking_is_retried_with_more_room(self):
        cut_off = {
            "chat_completions": ({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}, {"choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}]}, "max_tokens"),
            "responses": ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []},
                          {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Answer"}]}]}, "max_output_tokens"),
            "messages": ({"stop_reason": "max_tokens", "content": []}, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Answer"}]}, "max_tokens"),
        }
        for api_format, (first, second, limit_field) in cut_off.items():
            with self.subTest(api_format=api_format), patch.object(core, "_post_json", side_effect=[first, second]) as post:
                self.assertEqual(core.call_bot(bot(api_format), "Hi", "key"), "Answer")
                self.assertEqual([call.args[2][limit_field] for call in post.call_args_list], [1600, 6400])

    def test_a_reply_cut_off_twice_explains_why_and_plain_empty_replies_are_not_retried(self):
        cut_off = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        with patch.object(core, "_post_json", side_effect=[cut_off, cut_off]), self.assertRaisesRegex(core.UserError, "whole reply limit thinking"):
            core.call_bot(bot("chat_completions"), "Hi", "key")
        with patch.object(core, "_post_json", return_value={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}) as post:
            with self.assertRaisesRegex(core.UserError, "received no text"):
                core.call_bot(bot("chat_completions"), "Hi", "key")
        self.assertEqual(post.call_count, 1)

    def test_custom_settings_are_checked_by_the_custom_adapter(self):
        with self.assertRaisesRegex(core.UserError, "request template and response path"):
            core.validate_runnable(bot("custom"), "key")
        core.validate_runnable(bot("chat_completions"), "key")

    def test_a_new_format_only_needs_an_adapter(self):
        class EchoAdapter(core.Adapter):
            def request(self, bot, system, prompt, key, output_limit):
                return {"say": prompt}

            def reply_text(self, bot, response):
                return response["echo"]

        with patch.dict(core.ADAPTERS, {"echo": EchoAdapter()}), patch.object(core, "_post_json", return_value={"echo": "Hi back"}) as post:
            self.assertEqual(core.call_bot(bot("echo"), "Hi", "key"), "Hi back")
        self.assertEqual(post.call_args.args[2], {"say": "Hi"})

    def test_no_code_outside_the_adapters_branches_on_format_names(self):
        pattern = re.compile(r"""api_format["']?\]?\s*(==|!=)|==\s*["'](chat_completions|responses|messages|custom|image_generation)["']""")
        for name in ("core.py", "workflow.py", "app.py", "storage.py"):
            with self.subTest(file=name):
                self.assertEqual([line for line in (ROOT / name).read_text().splitlines() if pattern.search(line)], [])


if __name__ == "__main__":
    unittest.main()
