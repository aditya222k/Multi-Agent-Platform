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
import vault
from test_storage import CONFIG, FakeSupabase


API_KEY = "sk-live-0123456789abcdef9f59"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fresh_vault(owner="me", devices=None, password=None):
    keys = vault.Vault()
    keys.reset(owner, None, {}, devices, password)
    return keys


class VaultTests(unittest.TestCase):
    def test_passphrase_and_recovery_code_each_unlock_the_same_keys(self):
        keys = fresh_vault()
        protection, code = keys.setup("blue", remember=False)
        self.assertRegex(code, r"^[A-Z2-7]{4}(-[A-Z2-7]{4}){5}$")
        record = keys.seal("bot-1", API_KEY)
        for secret in ({"passphrase": "blue"}, {"recovery_code": code.lower().replace("-", " ")}):
            with self.subTest(secret=list(secret)):
                other = vault.Vault()
                other.reset("me", protection, {"bot-1": record}, None)
                self.assertEqual(other.unlock(**secret), {"bot-1": API_KEY})

    def test_stored_values_never_contain_the_key_or_passphrase(self):
        keys = fresh_vault()
        protection, code = keys.setup("blue-otter", remember=False)
        record = keys.seal("bot-1", API_KEY)
        stored = json.dumps([protection, record])
        for secret in (API_KEY, "blue-otter", code, code.replace("-", "")):
            self.assertNotIn(secret, stored)
        self.assertEqual((record["hint"], set(protection) >= {"salt", "wrapped", "recovery_salt", "recovery_wrapped", "n", "r", "p"}), ("9f59", True))
        self.assertEqual((protection["n"], protection["r"], protection["p"]), (2 ** 15, 8, 1))

    def test_the_same_key_saved_twice_gives_different_tokens(self):
        keys = fresh_vault()
        keys.setup("blue", remember=False)
        self.assertNotEqual(keys.seal("a", API_KEY)["token"], keys.seal("b", API_KEY)["token"])

    def test_wrong_guesses_wait_longer_each_time(self):
        clock = Clock()
        keys = fresh_vault()
        protection, _ = keys.setup("blue", remember=False)
        other = vault.Vault()
        other.reset("me", protection, {}, None)
        with patch.object(vault.time, "time", clock):
            waits = []
            for _ in range(4):
                with self.assertRaisesRegex(core.UserError, "not correct"):
                    other.unlock("wrong")
                waits.append(other.status()["retry_after"])
                with self.assertRaisesRegex(core.UserError, "Try again in"):
                    other.unlock("blue")  # Even the right passphrase waits out the delay.
                clock.now += waits[-1]
            self.assertEqual(waits, [1, 2, 4, 8])
            other.unlock("blue")
            self.assertEqual(other.status()["retry_after"], 0)

    def test_changing_the_passphrase_keeps_saved_keys_and_retires_the_old_one(self):
        keys = fresh_vault()
        keys.setup("blue", remember=False)
        record = keys.seal("bot-1", API_KEY)
        protection = keys.change_passphrase("green")
        other = vault.Vault()
        other.reset("me", protection, {"bot-1": record}, None)
        with self.assertRaises(core.UserError):
            other.unlock("blue")
        other._retry_at = 0
        self.assertEqual(other.unlock("green"), {"bot-1": API_KEY})

    def test_a_new_recovery_code_retires_the_old_one(self):
        keys = fresh_vault()
        _, old_code = keys.setup("blue", remember=False)
        protection, new_code = keys.new_recovery_code()
        other = vault.Vault()
        other.reset("me", protection, {}, None)
        with self.assertRaisesRegex(core.UserError, "recovery code is not correct"):
            other.unlock(recovery_code=old_code)
        other._retry_at = 0
        other.unlock(recovery_code=new_code)
        self.assertTrue(other.unlocked)

    def test_passphrase_rules(self):
        with self.assertRaisesRegex(core.UserError, "at least 4"):
            fresh_vault().setup("abc", remember=False)
        with self.assertRaisesRegex(core.UserError, "different from your sign-in password"):
            fresh_vault(password="secret-123").setup("secret-123", remember=False)
        fresh_vault().setup("abcd", remember=False)

    def test_locked_or_missing_setup_refuses_to_save(self):
        with self.assertRaisesRegex(core.UserError, "Set up a passphrase"):
            fresh_vault().seal("bot-1", API_KEY)
        keys = fresh_vault()
        keys.setup("blue", remember=False)
        keys.lock()
        with self.assertRaises(vault.VaultLocked):
            keys.seal("bot-1", API_KEY)

    def test_remember_on_this_device(self):
        folder = Path(tempfile.mkdtemp())
        devices = vault.DeviceKeys(folder)
        keys = fresh_vault(devices=devices)
        protection, _ = keys.setup("blue", remember=True)
        record = keys.seal("bot-1", API_KEY)
        self.assertEqual({path.name: path.stat().st_mode & 0o777 for path in folder.iterdir()}, {"device.key": 0o600, "remembered.json": 0o600})
        self.assertNotIn(API_KEY, (folder / "remembered.json").read_text())
        here = vault.Vault()
        self.assertEqual(here.reset("me", protection, {"bot-1": record}, devices), {"bot-1": API_KEY})
        self.assertEqual(vault.Vault().reset("someone-else", protection, {"bot-1": record}, devices), {})
        here.forget_device()
        self.assertEqual(vault.Vault().reset("me", protection, {"bot-1": record}, devices), {})

    def test_without_cryptography_keys_stay_session_only(self):
        with patch.object(vault, "Fernet", None):
            keys = fresh_vault()
            self.assertFalse(keys.status()["available"])
            with self.assertRaisesRegex(core.UserError, "pip install cryptography"):
                keys.setup("blue", remember=False)


class LocalKeysAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        for name, value in (("SETTINGS_FILE", self.temp / "settings.json"), ("IMAGES_DIR", self.temp / "images")):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.start()

    def start(self):
        self.state = app.State()
        bot = dict(core.blank_bot("bot-1"), name="Writer", endpoint="http://127.0.0.1:9/model", model="fake")
        other = dict(core.blank_bot("bot-2"), name="Artist", endpoint="http://127.0.0.1:9/model", model="fake")
        if not self.state.bots:
            self.state.bots += [bot, other]
            self.state.groups[0]["bot_ids"] = ["bot-1", "bot-2"]
            self.state.save()
        state_patch = patch.object(app, "STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.state.events.close)

    def restart(self):
        self.server.shutdown()
        self.server.server_close()
        self.state.close()
        self.start()

    def call(self, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("GET" if body is None else "POST", path, body=None if body is None else json.dumps(body),
                           headers={"X-Local-Token": self.state.token, "Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_saved_keys_survive_a_restart_locked_and_unlock_with_the_passphrase(self):
        status, result = self.call("/api/vault/setup", {"passphrase": "blue-otter", "remember": False})
        self.assertEqual((status, result["vault"]["configured"], result["vault"]["unlocked"]), (200, True, True))
        code = result["recovery_code"]
        self.call("/api/bots/bot-1/key", {"key": API_KEY, "remember": True})
        self.call("/api/bots/bot-2/key", {"key": "sk-session-only-0000", "remember": False})
        self.restart()
        state = self.call("/api/state")[1]
        self.assertEqual((state["vault"]["unlocked"], state["key_status"]["bot-1"], state["key_status"]["bot-2"]), (False, False, False))
        self.assertEqual(state["vault"]["saved"]["bot-1"]["hint"], "9f59")
        self.assertNotIn("bot-2", state["vault"]["saved"])
        status, body = self.call(f"/api/groups/{self.state.groups[0]['id']}/send", {"text": "Go", "continuous": False})
        self.assertEqual((status, body.get("vault_locked")), (400, True))
        self.assertIn("Writer", body["error"])
        self.assertEqual(self.call("/api/vault/unlock", {"passphrase": "nope"})[0], 400)
        self.state.vault._retry_at = 0
        status, result = self.call("/api/vault/unlock", {"recovery_code": code})
        self.assertEqual((status, result["used_recovery_code"], result["key_status"]["bot-1"]), (200, True, True))
        self.assertEqual(self.state.keys["bot-1"], API_KEY)

    def test_the_plain_key_never_reaches_disk_state_or_events(self):
        self.call("/api/vault/setup", {"passphrase": "blue-otter", "remember": True})
        status, state = self.call("/api/bots/bot-1/key", {"key": API_KEY, "remember": True})
        self.assertEqual(status, 200)
        self.assertNotIn(API_KEY, json.dumps(state))
        self.assertNotIn(API_KEY, json.dumps([event for event in self.state.events.since(0)]))
        self.state.close()
        for file in self.temp.rglob("*"):
            if file.is_file():
                self.assertNotIn(API_KEY.encode(), file.read_bytes(), file.name)
                self.assertNotIn(b"blue-otter", file.read_bytes(), file.name)

    def test_a_remembered_device_unlocks_by_itself_and_can_be_forgotten(self):
        self.call("/api/vault/setup", {"passphrase": "blue-otter", "remember": True})
        self.call("/api/bots/bot-1/key", {"key": API_KEY, "remember": True})
        self.restart()
        self.assertEqual((self.state.vault.unlocked, self.state.keys.get("bot-1")), (True, API_KEY))
        self.call("/api/vault/forget-device", {})
        self.restart()
        self.assertEqual((self.state.vault.unlocked, self.state.keys.get("bot-1")), (False, None))

    def test_remove_delete_and_reset_clear_saved_keys(self):
        self.call("/api/vault/setup", {"passphrase": "blue-otter"})
        self.call("/api/bots/bot-1/key", {"key": API_KEY, "remember": True})
        self.call("/api/bots/bot-2/key", {"key": API_KEY + "b", "remember": True})
        status, state = self.call("/api/bots/bot-1/key/remove", {})
        self.assertEqual((status, list(state["vault"]["saved"]), state["key_status"]["bot-1"]), (200, ["bot-2"], False))
        self.call("/api/bots/bot-2/delete", {})
        self.assertEqual(self.state.store._read("select count(*) from bot_keys")[0][0], 0)
        self.call("/api/bots", {"name": "Third"})
        third = self.state.bots[-1]["id"]
        self.call(f"/api/bots/{third}/key", {"key": API_KEY, "remember": True})
        self.assertEqual(self.call("/api/vault/reset", {})[0], 400)
        status, state = self.call("/api/vault/reset", {"confirm": True})
        self.assertEqual((status, state["vault"]["configured"], state["vault"]["saved"]), (200, False, {}))
        self.assertIsNone(self.state.store.get_meta("key_protection"))

    def test_saving_needs_an_unlocked_vault(self):
        status, body = self.call("/api/bots/bot-1/key", {"key": API_KEY, "remember": True})
        self.assertEqual((status, "passphrase" in body["error"]), (400, True))
        self.call("/api/vault/setup", {"passphrase": "blue-otter"})
        self.call("/api/vault/lock", {})
        status, body = self.call("/api/bots/bot-1/key", {"key": API_KEY, "remember": True})
        self.assertEqual((status, body.get("vault_locked")), (400, True))


class SupabaseKeysTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSupabase(password="account-password")
        http_patch = patch.object(storage, "_http", self.fake)
        http_patch.start()
        self.addCleanup(http_patch.stop)

    def signed_in(self, folder):
        state = app.State(load=False)
        state.use_supabase(CONFIG)
        with patch.object(app, "GRANTS_FILE", folder / "grants.json"):
            state.sign_in("me@example.com", "account-password")
        return state

    def test_only_encrypted_data_syncs_and_another_computer_can_unlock(self):
        laptop, desktop = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        state = self.signed_in(laptop)
        bot = dict(core.blank_bot("bot-1"), name="Writer")
        state.bots.append(bot)
        state.store.put_bot(bot)
        with self.assertRaisesRegex(core.UserError, "sign-in password"):
            state.vault_action("setup", {"passphrase": "account-password"})
        state.vault_action("setup", {"passphrase": "blue-otter"})
        state.store.put_bot_key("bot-1", state.vault.seal("bot-1", API_KEY))
        state.store.flush(2)
        synced = json.dumps(self.fake.tables)
        self.assertNotIn(API_KEY, synced)
        self.assertNotIn("blue-otter", synced)
        self.assertEqual(self.fake.tables["bot_keys"][0]["hint"], "9f59")
        self.assertIn("wrapped", self.fake.tables["key_protection"][0]["data"])
        state.close()
        other = self.signed_in(desktop)
        self.assertEqual((other.vault.status()["configured"], other.vault.unlocked, list(other.vault.saved)), (True, False, ["bot-1"]))
        other.vault_action("unlock", {"passphrase": "blue-otter"})
        self.assertEqual(other.keys["bot-1"], API_KEY)
        other.close()

    def test_accounts_without_the_new_tables_still_sign_in(self):
        self.fake.missing.update({"bot_keys", "key_protection"})
        state = self.signed_in(Path(tempfile.mkdtemp()))
        self.assertEqual(state.vault.status()["configured"], False)
        self.assertIn("schema.sql", state.public()["sync_error"])
        state.close()


if __name__ == "__main__":
    unittest.main()
