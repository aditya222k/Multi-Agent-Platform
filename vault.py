"""Saved bot API keys, locked by each person's own passphrase.

One random data key per account encrypts every saved API key. The data key is only ever
stored wrapped: once by a key derived from the passphrase, once by a key derived from a
one-time recovery code, and optionally by a secret kept on this device. Nothing that is
stored or synced can unlock an API key on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core import UserError

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # Optional: without it, keys stay in memory only, as before.
    Fernet = None

    class InvalidToken(Exception):
        pass


SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
MIN_PASSPHRASE = 4
MAX_PASSPHRASE = 1000
MAX_DELAY = 60


class VaultLocked(UserError):
    """A run needs a saved key that is still locked."""


def available() -> bool:
    return Fernet is not None


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(secret: str, salt: bytes, params: dict) -> Fernet:
    key = hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                         maxmem=128 * 1024 * 1024, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def new_recovery_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(15)).decode("ascii")
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))


def _normalize_code(code: str) -> str:
    return "".join(code.split()).replace("-", "").upper()


def _hint(key: str) -> str:
    return key[-4:] if len(key) >= 8 else ""


class DeviceKeys:
    """"Remember on this device": the data key, wrapped by a secret file that never leaves this computer."""

    def __init__(self, folder: Path):
        self.secret_file = folder / "device.key"
        self.remembered_file = folder / "remembered.json"

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        descriptor = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        temp.replace(path)

    def _fernet(self, create: bool) -> Fernet | None:
        try:
            return Fernet(self.secret_file.read_bytes().strip())
        except (OSError, ValueError):
            if not create:
                return None
        key = Fernet.generate_key()
        self._write(self.secret_file, key)
        return Fernet(key)

    def _entries(self) -> dict:
        try:
            entries = json.loads(self.remembered_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return entries if isinstance(entries, dict) else {}

    def remember(self, owner: str, data_key: bytes) -> None:
        entries = self._entries()
        entries[owner] = self._fernet(create=True).encrypt(data_key).decode("ascii")
        self._write(self.remembered_file, json.dumps(entries).encode("utf-8"))

    def recall(self, owner: str) -> bytes | None:
        token, fernet = self._entries().get(owner), self._fernet(create=False)
        if not token or fernet is None:
            return None
        try:
            return fernet.decrypt(token.encode("ascii"))
        except InvalidToken:
            return None

    def forget(self, owner: str) -> None:
        entries = self._entries()
        if entries.pop(owner, None) is not None:
            self._write(self.remembered_file, json.dumps(entries).encode("utf-8"))

    def remembers(self, owner: str) -> bool:
        return owner in self._entries()


class Vault:
    """One account's saved keys. Readable keys and the data key exist only in memory, only while unlocked."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner = ""
        self.devices: DeviceKeys | None = None
        self.protection: dict | None = None
        self.saved: dict[str, dict] = {}
        self._data: Fernet | None = None
        self._data_key: bytes | None = None
        self._failures = 0
        self._retry_at = 0.0
        self._password_check: tuple[bytes, bytes] | None = None

    def reset(self, owner: str, protection: dict | None, saved: dict, devices: DeviceKeys | None, password: str | None = None) -> dict[str, str]:
        """Switch to an account. Returns keys it could unlock straight away from this device."""
        with self._lock:
            self.owner, self.devices = owner, devices
            self.protection = protection if isinstance(protection, dict) and protection.get("wrapped") else None
            self.saved = {bot_id: record for bot_id, record in (saved or {}).items() if isinstance(record, dict) and record.get("token")}
            self._data = self._data_key = None
            self._failures, self._retry_at = 0, 0.0
            self._password_check = None
            if password:
                # Kept only to refuse a passphrase identical to the sign-in password; the password itself is not kept.
                pepper = secrets.token_bytes(16)
                self._password_check = (pepper, hmac.new(pepper, password.encode("utf-8"), "sha256").digest())
            if available() and self.protection and devices:
                data_key = devices.recall(owner)
                if data_key:
                    self._open(data_key)
                    return self._open_all()
        return {}

    def _open(self, data_key: bytes) -> None:
        self._data_key, self._data = data_key, Fernet(data_key)

    def _open_all(self) -> dict[str, str]:
        opened = {}
        for bot_id, record in self.saved.items():
            try:
                opened[bot_id] = self._data.decrypt(record["token"].encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError):
                pass  # A token from another account or a damaged one stays locked rather than failing the unlock.
        return opened

    @property
    def unlocked(self) -> bool:
        return self._data is not None

    def status(self) -> dict:
        with self._lock:
            return {
                "available": available(),
                "configured": self.protection is not None,
                "unlocked": self._data is not None,
                "remembered": bool(self.devices and self.protection and self.devices.remembers(self.owner)),
                "saved": {bot_id: {"hint": record.get("hint", ""), "saved_at": record.get("saved_at", "")} for bot_id, record in self.saved.items()},
                "retry_after": max(0, round(self._retry_at - time.time())),
                "min_length": MIN_PASSPHRASE,
            }

    def _check_passphrase(self, passphrase: object) -> str:
        if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE or len(passphrase) > MAX_PASSPHRASE:
            raise UserError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
        if self._password_check:
            pepper, digest = self._password_check
            if hmac.compare_digest(digest, hmac.new(pepper, passphrase.encode("utf-8"), "sha256").digest()):
                raise UserError("Use a passphrase that is different from your sign-in password.")
        return passphrase

    def _require(self, unlocked: bool = True) -> None:
        if not available():
            raise UserError("Saving keys needs the cryptography package: python3 -m pip install cryptography")
        if self.protection is None:
            raise UserError("Set up a passphrase for saved keys first.")
        if unlocked and self._data is None:
            raise VaultLocked("Unlock your saved keys first.")

    def setup(self, passphrase: object, remember: bool) -> tuple[dict, str]:
        """Create the data key and lock it with the passphrase and a new recovery code. Returns the record and the code."""
        if not available():
            raise UserError("Saving keys needs the cryptography package: python3 -m pip install cryptography")
        with self._lock:
            if self.protection is not None:
                raise UserError("Saved keys already have a passphrase. Change it instead.")
            passphrase = self._check_passphrase(passphrase)
            data_key = Fernet.generate_key()
            code = new_recovery_code()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.protection = {"version": 1, "kdf": "scrypt", **SCRYPT, "created_at": now, **self._wrap(passphrase, data_key, "")}
            self.protection.update(self._wrap(_normalize_code(code), data_key, "recovery_"))
            self._open(data_key)
            if remember and self.devices:
                self.devices.remember(self.owner, data_key)
            return dict(self.protection), code

    def _wrap(self, secret: str, data_key: bytes, prefix: str) -> dict:
        salt = secrets.token_bytes(16)
        return {f"{prefix}salt": _encode(salt), f"{prefix}wrapped": _derive(secret, salt, SCRYPT).encrypt(data_key).decode("ascii")}

    def unlock(self, passphrase: object = None, recovery_code: object = None, remember: bool = False) -> dict[str, str]:
        """Open the saved keys with the passphrase or the recovery code. Wrong attempts wait 1, 2, 4… seconds."""
        self._require(unlocked=False)
        with self._lock:
            wait = self._retry_at - time.time()
            if wait > 0:
                seconds = max(1, round(wait))
                raise UserError(f"Too many wrong attempts. Try again in {seconds} second{'s' if seconds != 1 else ''}.")
            prefix, secret = ("recovery_", _normalize_code(recovery_code)) if isinstance(recovery_code, str) and recovery_code.strip() else ("", passphrase)
            if not isinstance(secret, str) or not secret or len(secret) > MAX_PASSPHRASE:
                raise UserError("Enter your passphrase or recovery code.")
            params = {name: self.protection[name] for name in ("n", "r", "p")}
            try:
                kek = _derive(secret, base64.b64decode(self.protection[f"{prefix}salt"]), params)
                data_key = kek.decrypt(self.protection[f"{prefix}wrapped"].encode("ascii"))
            except (InvalidToken, KeyError, ValueError):
                self._failures += 1
                self._retry_at = time.time() + min(MAX_DELAY, 2 ** (self._failures - 1))
                raise UserError("That recovery code is not correct." if prefix else "That passphrase is not correct.") from None
            self._failures, self._retry_at = 0, 0.0
            self._open(data_key)
            if remember and self.devices:
                self.devices.remember(self.owner, data_key)
            return self._open_all()

    def lock(self) -> list[str]:
        with self._lock:
            self._data = self._data_key = None
            return list(self.saved)

    def seal(self, bot_id: str, key: str) -> dict:
        """Encrypt a key for storage. Fernet adds a fresh random IV, so equal keys give different tokens."""
        self._require()
        with self._lock:
            record = {"token": self._data.encrypt(key.encode("utf-8")).decode("ascii"), "hint": _hint(key),
                      "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.saved[bot_id] = record
            return dict(record)

    def remove(self, bot_id: str) -> bool:
        with self._lock:
            return self.saved.pop(bot_id, None) is not None

    def change_passphrase(self, passphrase: object) -> dict:
        """Re-wrap the same data key with a new passphrase and salt. Saved key tokens do not change."""
        self._require()
        with self._lock:
            passphrase = self._check_passphrase(passphrase)
            self.protection.update(self._wrap(passphrase, self._data_key, ""))
            self.protection["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return dict(self.protection)

    def new_recovery_code(self) -> tuple[dict, str]:
        self._require()
        with self._lock:
            code = new_recovery_code()
            self.protection.update(self._wrap(_normalize_code(code), self._data_key, "recovery_"))
            self.protection["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return dict(self.protection), code

    def forget_device(self) -> None:
        if self.devices:
            self.devices.forget(self.owner)

    def clear(self) -> list[str]:
        """Remove the passphrase setup and every saved key. Returns the bots whose saved keys are gone."""
        with self._lock:
            removed = list(self.saved)
            self.protection, self.saved = None, {}
            self._data = self._data_key = None
            self._failures, self._retry_at = 0, 0.0
        self.forget_device()
        return removed
