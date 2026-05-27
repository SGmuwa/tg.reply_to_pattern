#!/usr/bin/env python3

from asyncio import create_task, sleep as sleep
from json5 import load, loads
from loguru import logger
from os import environ, remove
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import re
import telethon

logger.trace("application started.")


class Settings:
    def __init__(
        self,
        secret_path=environ.get("TELEGRAM_SECRET_PATH", "./secret.json5"),
        content=environ.get("TELEGRAM_SECRET", None),
    ):
        if content:
            logger.debug("use secret environment")
            self.json = loads(content)
        else:
            logger.debug("use secret file")
            with open(secret_path) as f:
                self.json = load(f)
            try:
                remove(secret_path)
            except Exception as e:
                logger.warning("Can't remove secret file, error: «{}»", e)
        self._is_session_and_auth_key_configurated = None

    @property
    def session_and_auth_key(self) -> str:
        output = self.json.pop("session_and_auth_key", None)
        if self._is_session_and_auth_key_configurated is None:
            if output:
                self._is_session_and_auth_key_configurated = True
            else:
                self._is_session_and_auth_key_configurated = False
        return output

    @property
    def is_session_and_auth_key_configurated(self) -> str:
        if self._is_session_and_auth_key_configurated is None:
            return "session_and_auth_key" in self.json
        else:
            return self._is_session_and_auth_key_configurated

    @property
    def api_id(self) -> int:
        return self.json.pop("api_id", 1)

    @property
    def api_hash(self) -> str:
        return self.json.pop("api_hash", "0")

    @property
    def bot_token(self) -> str:
        return self.json.pop("bot_token")

    @property
    def source_chat(self) -> int | None:
        return self.json.get("source_chat")

    @property
    def replies_messages(self) -> dict[str, str]:
        return self.json["replies_messages"]

    @property
    def retries_max_count(self) -> int:
        return self.json.pop("retries_max_count", 3)

    @property
    def retries_sleep_seconds(self) -> int | float:
        return self.json.pop("retries_sleep_seconds", 60 * 20)

    @property
    def auto_delete_reply_after_ms(self) -> int | float:
        return self.json.pop("auto_delete_reply_after_ms", 0)


settings = Settings()

reply_rules: list[tuple[str, re.Pattern[str]]] = [
    (reply_text, re.compile(pattern))
    for reply_text, pattern in settings.replies_messages.items()
]

logger.trace("Init TelegramClient...")
with TelegramClient(
    StringSession(settings.session_and_auth_key),
    settings.api_id,
    settings.api_hash,
    base_logger=logger,
).start() as client:
    client: TelegramClient = client
    logger.trace("Telegram client instance created")
    if not settings.is_session_and_auth_key_configurated:
        raise Exception(
            f"Use session, instead of api_id and api_hash. Set session_and_auth_key to value: «{client.session.save()}»"
        )

    def retries(
        callback,
        max_count=settings.retries_max_count,
        sleep_seconds=settings.retries_sleep_seconds,
    ):
        async def wrapper(*args, **kwargs):
            tries = 0
            while True:
                tries += 1
                try:
                    return await callback(*args, **kwargs)
                except Exception as e:
                    if tries >= max_count:
                        raise Exception("Max retries exceeded", tries, max_count) from e
                    await sleep(sleep_seconds)

        return wrapper

    async def reply_to_message(
        message: telethon.tl.patched.Message, reply_text: str
    ) -> telethon.types.Message:
        logger.debug("reply_to_message start: {} -> {}", message.id, reply_text)
        result = await message.reply(reply_text)
        logger.debug("reply_to_message end, return: {}", result)
        return result

    reply_to_message_retry = retries(reply_to_message)

    auto_delete_reply_after_ms = settings.auto_delete_reply_after_ms

    async def schedule_delete_reply(sent: telethon.types.Message):
        await sleep(auto_delete_reply_after_ms / 1000)
        try:
            await sent.delete()
            logger.info(
                "deleted reply {} in chat {} after {} ms",
                sent.id,
                sent.chat_id,
                auto_delete_reply_after_ms,
            )
        except Exception as e:
            logger.warning("can't delete reply {}: {}", sent.id, e)

    def maybe_schedule_delete_reply(sent: telethon.types.Message):
        if auto_delete_reply_after_ms > 0:
            create_task(schedule_delete_reply(sent))

    async def on_new_message(event: telethon.events.newmessage.NewMessage.Event):
        message: telethon.tl.patched.Message = event.message
        if (await client.get_me()).id == message.sender_id:
            logger.warning("Sender is me! Skip: {}", message.message)
            return
        text = message.message or ""
        for reply_text, pattern in reply_rules:
            if pattern.search(text):
                logger.info(
                    "match {} in chat {}: {}",
                    pattern.pattern,
                    message.peer_id,
                    text,
                )
                sent = await reply_to_message_retry(message, reply_text)
                maybe_schedule_delete_reply(sent)
                return
        logger.debug("no match for message {}", message.id)

    new_message_event = (
        events.NewMessage()
        if settings.source_chat is None
        else events.NewMessage(chats=settings.source_chat)
    )

    @client.on(new_message_event)
    async def handler(event: telethon.events.newmessage.NewMessage.Event):
        try:
            message: telethon.tl.patched.Message = event.message
            logger.info("got message {}, chat: {}, text: {}", message.peer_id, message.chat_id, message.message)
            await on_new_message(event)
        except Exception as e:
            logger.exception(e)

    if settings.source_chat is None:
        logger.info("Telegram ready, all chats, rules: {}", len(reply_rules))
    else:
        logger.info(
            "Telegram ready, chat {}, rules: {}",
            settings.source_chat,
            len(reply_rules),
        )
    if auto_delete_reply_after_ms > 0:
        logger.info(
            "auto-delete reply after {} ms ({:.1f} h)",
            auto_delete_reply_after_ms,
            auto_delete_reply_after_ms / 3_600_000,
        )
    else:
        logger.info("auto-delete reply: disabled")
    client.run_until_disconnected()
