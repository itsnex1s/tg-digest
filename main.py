"""Daily Telegram digest bot.

Reads N channels via a Telethon user account, summarises the last
`lookback_hours` of messages with an OpenAI-compatible LLM, then posts
the result to a Telegram chat through a bot.

Two-account split:
  - User account (Telethon, MTProto): can read any channel/chat the user
    is subscribed to. Never sends anything.
  - Bot (HTTP Bot API): can post into the target chat. Never reads.

This separation keeps the user account passive and the bot token
shareable with unrelated tools.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telethon import TelegramClient
from telethon.tl.types import Message

logger = logging.getLogger("digest")

# Telegram caps a single sendMessage at 4096 chars. We split on
# paragraph boundaries below that to leave headroom for HTML overhead.
TELEGRAM_MAX_MESSAGE = 4000


@dataclass
class Config:
    sources: list[str]
    schedule: str
    timezone: str
    lookback_hours: int
    max_messages_per_channel: int
    min_message_length: int
    max_message_chars: int
    prompt: str

    @classmethod
    def load(cls, path: Path) -> Config:
        with path.open() as f:
            return cls(**yaml.safe_load(f))


@dataclass
class Settings:
    """All runtime dependencies in one place to keep call signatures tidy."""

    config: Config
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    bot_token: str
    output_chat: int
    output_thread: int | None


@dataclass
class ChannelBatch:
    source: str
    username: str | None  # @-handle without the @, None for private channels
    title: str
    messages: list[Message]


# ─── Telegram fetching (user account, read-only) ─────────────────────


async def fetch_channel(
    client: TelegramClient,
    source: str,
    since: datetime,
    limit: int,
    min_len: int,
) -> ChannelBatch | None:
    """Fetch messages from one source produced after `since`."""
    try:
        entity = await client.get_entity(source)
    except Exception as e:
        logger.warning("resolve %s: %s", source, e)
        return None

    title = getattr(entity, "title", None) or getattr(entity, "first_name", source)
    username = getattr(entity, "username", None)

    messages: list[Message] = []
    async for m in client.iter_messages(entity, limit=limit):
        if m.date < since:
            break
        text = m.message or ""
        if len(text) < min_len:
            continue
        messages.append(m)
    messages.reverse()
    return ChannelBatch(source, username, title, messages)


# ─── LLM input / output ──────────────────────────────────────────────


def build_llm_input(batches: list[ChannelBatch], cfg: Config) -> str:
    """Format the structured input the LLM will summarise.

    Each message is annotated with its direct t.me link so the model can
    cite specific posts in the output. Long messages are truncated.
    """
    parts: list[str] = []
    total_msgs = sum(len(b.messages) for b in batches)
    parts.append(
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Lookback: last {cfg.lookback_hours} hours\n"
        f"Active channels: {len(batches)}\n"
        f"Total messages: {total_msgs}\n"
    )
    for i, b in enumerate(batches, 1):
        link = f"https://t.me/{b.username}" if b.username else "(private)"
        parts.append(f"\n=== Channel {i}: {b.title} (@{b.username or '?'}) — {link}")
        for m in b.messages:
            text = (m.message or "").strip()
            if len(text) > cfg.max_message_chars:
                text = text[: cfg.max_message_chars] + "…"
            stamp = m.date.strftime("%H:%M")
            msg_link = (
                f"https://t.me/{b.username}/{m.id}" if b.username else "(no link)"
            )
            parts.append(f"[{stamp}] {msg_link}\n{text}\n")
    return "\n".join(parts)


async def summarise(
    api_key: str, base_url: str, model: str, system: str, content: str
) -> str:
    """Call the OpenAI-compatible chat/completions endpoint."""
    async with httpx.AsyncClient(timeout=300) as h:
        r = await h.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "temperature": 0.3,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# ─── Telegram posting (bot, write-only) ──────────────────────────────


def strip_markdown_fence(text: str) -> str:
    """Strip ```html ... ``` if the model wrapped its output."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    first_nl = text.find("\n")
    if first_nl == -1:
        return text
    text = text[first_nl + 1 :]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text


def split_for_telegram(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    """Split on paragraph boundaries to fit Telegram's per-message cap."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) + 2 > limit and buf:
            parts.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts


async def post_via_bot(
    bot_token: str, chat: int, thread: int | None, text: str
) -> None:
    """POST text (Telegram HTML) to the configured chat/thread."""
    payload: dict = {
        "chat_id": chat,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread is not None:
        payload["message_thread_id"] = thread
    async with httpx.AsyncClient(timeout=60) as h:
        for chunk in split_for_telegram(text):
            payload["text"] = chunk
            r = await h.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload
            )
            if r.status_code != 200:
                logger.error("bot send failed: %s", r.text)
                r.raise_for_status()


# ─── Pipeline ────────────────────────────────────────────────────────


async def run_digest(client: TelegramClient, s: Settings) -> None:
    """Fetch → summarise → post. The whole pipeline, end to end."""
    cfg = s.config
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)
    logger.info(
        "digest start: %d sources, lookback %dh (since %s)",
        len(cfg.sources),
        cfg.lookback_hours,
        since.isoformat(timespec="minutes"),
    )

    batches: list[ChannelBatch] = []
    for src in cfg.sources:
        b = await fetch_channel(
            client, src, since, cfg.max_messages_per_channel, cfg.min_message_length
        )
        if b is None or not b.messages:
            logger.info("  %s: 0 messages", src)
            continue
        batches.append(b)
        logger.info("  %s: %d messages", src, len(b.messages))

    if not batches:
        logger.info("nothing to digest, skipping")
        return

    user_msg = build_llm_input(batches, cfg)
    logger.info(
        "calling LLM: %d chars, %d channels, %d messages",
        len(user_msg),
        len(batches),
        sum(len(b.messages) for b in batches),
    )
    summary = await summarise(
        s.llm_api_key, s.llm_base_url, s.llm_model, cfg.prompt, user_msg
    )
    summary = strip_markdown_fence(summary)
    await post_via_bot(s.bot_token, s.output_chat, s.output_thread, summary)
    logger.info("posted, %d chars", len(summary))


def load_settings() -> Settings:
    """Read config file and environment variables into a Settings object."""
    cfg_path = Path(os.environ.get("CONFIG_PATH", "/app/config.yaml"))
    return Settings(
        config=Config.load(cfg_path),
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        output_chat=int(os.environ["OUTPUT_CHAT_ID"]),
        output_thread=(
            int(os.environ["OUTPUT_THREAD_ID"])
            if os.environ.get("OUTPUT_THREAD_ID")
            else None
        ),
    )


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    settings = load_settings()

    session_path = os.environ.get("SESSION_PATH", "/app/data/digest")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        logger.error(
            "no Telethon session at %s.session — run bootstrap_login.py first",
            session_path,
        )
        sys.exit(2)
    me = await client.get_me()
    logger.info("logged in as %s (id=%d)", getattr(me, "first_name", "?"), me.id)

    if os.getenv("RUN_ONCE") == "1":
        try:
            await run_digest(client, settings)
        finally:
            await client.disconnect()
        return

    scheduler = AsyncIOScheduler(timezone=settings.config.timezone)
    scheduler.add_job(
        run_digest,
        CronTrigger.from_crontab(
            settings.config.schedule, timezone=settings.config.timezone
        ),
        args=[client, settings],
        id="digest",
    )
    scheduler.start()
    next_run = scheduler.get_jobs()[0].next_run_time
    logger.info(
        "scheduled '%s' (%s); next run: %s",
        settings.config.schedule,
        settings.config.timezone,
        next_run,
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
