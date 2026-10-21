import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import core


class SettingsTests(unittest.TestCase):
    def test_per_bot_api_keys_never_reach_local_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / "settings.json"
            with patch.object(app, "SETTINGS_FILE", settings):
                state = app.State()
                bot = core.blank_bot("bot-one")
                state.bots.append(bot)
                state.groups[0]["bot_ids"].append(bot["id"])
                state.keys[bot["id"]] = "private-test-key"
                state.save()
                state.close()
                for file in Path(temp).iterdir():
                    self.assertNotIn(b"private-test-key", file.read_bytes(), file.name)
                self.assertEqual((Path(temp) / "app.db").stat().st_mode & 0o777, 0o600)
                reloaded = app.State()
                self.assertEqual(reloaded.groups[0]["bot_ids"], ["bot-one"])
                self.assertFalse(reloaded.keys.get("bot-one"))

    def test_old_workflow_migrates_into_bots_and_general_group(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / "settings.json"
            settings.write_text(json.dumps({"agents": [{"name": "Researcher", "provider": "openai", "model": "sample-model", "instructions": "Research"}], "grants": []}), encoding="utf-8")
            with patch.object(app, "SETTINGS_FILE", settings):
                state = app.State()
                self.assertEqual(state.bots[0]["name"], "Researcher")
                self.assertEqual(state.bots[0]["role"], "Research")
                self.assertEqual(state.groups[0]["bot_ids"], [state.bots[0]["id"]])


if __name__ == "__main__":
    unittest.main()
