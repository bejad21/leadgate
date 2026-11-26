"""Encryption of the chat text the engine keeps for itself.

With CHAT_ENCRYPTION_KEY set, the message, reply, tool calls and saved chat state are stored as ciphertext (Fernet:
AES-128-CBC with an HMAC), so someone who reads the database alone cannot read the chats. The key stays with the
engine, in its environment. Values written before the key was set carry no prefix and are still read.

The dashboard's copy of conversations (`conversation_turns`) is not covered: the browser reads it directly and has no
key. Row level security and the deletion period protect it.
"""
import os

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc1:"
KEY_VARIABLE = "CHAT_ENCRYPTION_KEY"


class UnreadableText(ValueError):
    """Stored text that is encrypted and cannot be opened: no key, or another key."""


class TextCipher:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.strip().encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            raise ValueError(
                f"{KEY_VARIABLE} is not a valid key. Make one with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from None

    @classmethod
    def from_env(cls) -> "TextCipher | None":
        key = (os.environ.get(KEY_VARIABLE) or "").strip()
        return cls(key) if key else None

    def seal(self, text: str) -> str:
        return PREFIX + self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def open(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise UnreadableText("stored text was encrypted with another key") from None


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)
