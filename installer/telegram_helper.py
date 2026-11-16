"""Telegram helpers for setting up alerts: check a bot token, find the chat
ID of whoever messaged the bot, and send a test message.

These run on this computer, with the token the user typed. The token is part
of Telegram's URLs, so it is removed from any error text before it is shown.
"""

import json
import re
import urllib.error
import urllib.request

API = "https://api.telegram.org"
TOKEN_FORMAT = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


class TelegramError(Exception):
    """A problem with the bot or the request; the message is for the user."""


def _call(token, method, params=None, base=API, timeout=15):
    if not TOKEN_FORMAT.match(token or ""):
        raise TelegramError("That does not look like a bot token. It looks like 123456789:ABCdef... "
                            "and comes from @BotFather.")
    request = urllib.request.Request(
        "%s/bot%s/%s" % (base, token, method), data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "wyzer-cam-installer"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    except OSError as exc:
        raise TelegramError("Could not reach Telegram: %s" % str(exc).replace(token, "<token>"))
    try:
        data = json.loads(body)
    except ValueError:
        raise TelegramError("Telegram sent a reply that could not be read.")
    if not data.get("ok"):
        description = str(data.get("description") or "unknown error").replace(token, "<token>")
        if data.get("error_code") == 401:
            description = "Telegram does not recognise this token."
        raise TelegramError(description)
    return data.get("result")


def get_me(token, base=API, timeout=15):
    """The bot's details: {"username": ..., "name": ...}."""
    me = _call(token, "getMe", base=base, timeout=timeout)
    return {"username": me.get("username", ""), "name": me.get("first_name", "")}


def _chat_name(chat):
    if chat.get("title"):
        return chat["title"]
    name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
    if chat.get("username"):
        name = (name + " " if name else "") + "@" + chat["username"]
    return name or str(chat.get("id"))


def find_chats(token, base=API, timeout=15):
    """Chats that recently messaged the bot: [{"id", "name", "type"}], newest first.

    Telegram keeps a bot's unread messages for a day, so message the bot just
    before calling this.
    """
    updates = _call(token, "getUpdates", {"limit": 100, "timeout": 0}, base=base, timeout=timeout)
    chats = {}
    for update in reversed(updates or []):
        for kind in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(kind) or {}).get("chat")
            if chat and "id" in chat and chat["id"] not in chats:
                chats[chat["id"]] = {"id": chat["id"], "name": _chat_name(chat), "type": chat.get("type", "")}
    return list(chats.values())


def send_test(token, chat_id, text="Test message from the wyzer-cam installer. Alerts will arrive here.",
              base=API, timeout=15):
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text}, base=base, timeout=timeout)
