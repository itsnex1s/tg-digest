# tg-digest

Daily Telegram channel digests, written by your favourite LLM.

Subscribes to N Telegram channels through a user account, fetches the last 24 hours of messages, has an LLM produce a structured summary, then posts that summary back into a Telegram chat through a bot.

The user account is **read-only** — it never sends anything. The bot is **write-only** — it never reads. This split is intentional: your account stays passive, your bot token is safe to share with unrelated tools, and the two halves can be operated by different people if you want.

## Why

Telegram is great until you're subscribed to 30 channels and miss everything that matters. Existing "digest bots" are either:

- SaaS you don't want to give your account to;
- bot-only solutions, which means they only see channels where they're an admin (not your subscriptions);
- one-off scripts with no scheduling, no formatting, no error handling.

This is the self-hostable middle ground: one Docker service, one LLM API key, one cron expression.

## Architecture

```
┌──────────────────────────────┐
│ Telegram channels & chats    │
│ the user account subscribes  │
│ to                           │
└──────────────┬───────────────┘
               │ MTProto (Telethon, read-only)
               ▼
┌──────────────────────────────────────────────┐
│                  tg-digest                   │
│  ┌────────────────────────────────────────┐  │
│  │ fetch last N hours of messages,        │  │
│  │ filter noise, attach permalinks        │  │
│  └────────────────────┬───────────────────┘  │
│                       ▼                      │
│  ┌────────────────────────────────────────┐  │
│  │ feed to an OpenAI-compatible LLM with  │  │
│  │ a structured prompt → HTML digest      │  │
│  └────────────────────┬───────────────────┘  │
│                       ▼                      │
│  ┌────────────────────────────────────────┐  │
│  │ POST as Telegram HTML via the Bot API  │  │
│  └────────────────────┬───────────────────┘  │
└───────────────────────┼──────────────────────┘
                        │
                        ▼
              ┌─────────────────────┐
              │ Telegram chat or    │
              │ forum topic         │
              └─────────────────────┘
```

## Quickstart

### 1. Telegram MTProto credentials

Open https://my.telegram.org/apps, sign in, create an application — save `App api_id` and `App api_hash`.

If `my.telegram.org` errors out for you (it routinely does), the publicly known Telegram Desktop credentials work everywhere open-source TG clients work: `TG_API_ID=2040`, `TG_API_HASH=b18441a1ff607e10a989891a5462e627`. Read-only personal use is, in practice, low-risk; use at your own discretion.

### 2. Create a bot

Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → save the token.

Add the bot to the chat where you want digests. If the chat is a supergroup with forum topics, also note the topic id you want to post into.

### 3. Configure

```bash
git clone https://github.com/YOU/tg-digest.git
cd tg-digest
cp .env.example .env
cp config.example.yaml config.yaml
```

Fill in `.env` with your tokens and `config.yaml` with the channels you want digested. Both files are gitignored.

### 4. Bootstrap the Telethon session

The user account needs a one-time login. The bootstrap script waits for files to appear in `data/` instead of prompting on stdin — that way you can run it on a headless server over SSH without TTY allocation.

```bash
docker compose run --rm --entrypoint "python -u" digest /app/bootstrap_login.py
```

You'll see:

```
phone: +15551234567  session: /app/data/digest.session
[CODE] waiting for /app/data/code.txt — write the value into that file
```

Open your Telegram app, copy the login code, then in another shell:

```bash
echo 12345 > data/code.txt
```

If your account has 2FA, the script will then wait for `data/2fa.txt`:

```bash
echo your_2fa_password > data/2fa.txt
```

Once you see `OK — logged in as ...`, `data/digest.session` is your saved login. The container exits.

### 5. Run

```bash
docker compose up -d
docker compose logs -f
```

To trigger a digest immediately (skip the cron):

```bash
docker compose run --rm -e RUN_ONCE=1 digest
```

## Configuration reference

### `config.yaml`

| Key                        | Type              | Purpose                                    |
|----------------------------|-------------------|--------------------------------------------|
| `sources`                  | list of strings   | `@username` / numeric id of each source    |
| `schedule`                 | cron expression   | when to run (5-field cron)                 |
| `timezone`                 | IANA tz string    | timezone the cron is interpreted in        |
| `lookback_hours`           | int               | how far back to fetch                      |
| `max_messages_per_channel` | int               | per-channel cap                            |
| `min_message_length`       | int               | skip messages shorter than this            |
| `max_message_chars`        | int               | truncate single posts before sending to LLM|
| `prompt`                   | string (multiline)| system prompt for the LLM                  |

### `.env`

| Variable             | Purpose                                              |
|----------------------|------------------------------------------------------|
| `TG_API_ID`          | MTProto app id                                       |
| `TG_API_HASH`        | MTProto app hash                                     |
| `TG_PHONE`           | phone of the user account (bootstrap only)           |
| `TELEGRAM_BOT_TOKEN` | bot token from @BotFather                            |
| `OUTPUT_CHAT_ID`     | destination chat id (supergroups start with `-100`)  |
| `OUTPUT_THREAD_ID`   | forum topic id, or empty                             |
| `LLM_API_KEY`        | OpenAI-compatible key                                |
| `LLM_BASE_URL`       | endpoint base URL (default `api.openai.com/v1`)      |
| `LLM_MODEL`          | model name (default `gpt-4o-mini`)                   |

## LLM compatibility

Any OpenAI-compatible `/chat/completions` endpoint works:

| Provider     | `LLM_BASE_URL`                                  | Example `LLM_MODEL` |
|--------------|-------------------------------------------------|---------------------|
| OpenAI       | `https://api.openai.com/v1`                     | `gpt-4o-mini`       |
| Anthropic*   | `https://api.anthropic.com/v1`                  | `claude-haiku-4-5`  |
| Z.AI / Zhipu | `https://open.bigmodel.cn/api/paas/v4`          | `glm-4.5-flash`     |
| OpenRouter   | `https://openrouter.ai/api/v1`                  | `openrouter/auto`   |
| Local        | `http://your-host:11434/v1` (Ollama / vLLM)     | `llama3.1:8b`       |

\* Anthropic exposes OpenAI-compatible chat completions; check their docs for the exact path.

## Output

The default prompt produces something like:

```
📰 Daily Digest — 2026-05-25
Last 24 hours · 6 channels · 47 messages

▍ @durov · founder updates and platform news
• Native editor. Pavel announces a built-in code editor for chat-shared files up to 50 MB. ↗
• Stats. The Stories tab reportedly passed 600 M DAU. ↗

▍ @telegram · official platform releases
• iOS 11.4. Adds chatbot-as-tab and an improved gift API. ↗
• Bot API. New endpoints for managing forum topics programmatically. ↗

✨ Highlights
1. Most important story of the day. ↗
2. Second most important. ↗
```

Every `↗` is a clickable permalink to the source message — one click drills into the original post.

To tweak style, language, or structure, **edit the `prompt:` field in `config.yaml`** — that's where 95% of customization lives. The Python code is just the plumbing.

## Operational notes

- **Subscriptions matter.** The user account must already be subscribed to each source before the bot can read it. The script does not auto-join.
- **Forum topics.** If the target chat is a supergroup with topics enabled, the bot needs `Manage Topics` only if you want it to *create* topics. To post into an existing topic, plain "send messages" is enough.
- **Session file = full account access.** `data/digest.session` is equivalent to logging into your account on a new device. Back it up if you don't want to redo the bootstrap, treat it like a secret if you don't.
- **LLM quality varies.** Smaller/older models will ignore formatting rules, hallucinate, or skip channels. `gpt-4o-mini`, `glm-4.5-flash`, and `claude-haiku-4-5` work reliably in testing. If output looks bad, the model — not the prompt — is usually the bottleneck.
- **Telegram caps a message at 4096 characters.** The bot auto-splits the digest on paragraph boundaries if it's longer.

## Adding channels

Edit `config.yaml`, append to `sources:`, then:

```bash
docker compose restart digest
```

The scheduler picks up the new list at the next run. To verify against the new list immediately:

```bash
docker compose run --rm -e RUN_ONCE=1 digest
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# run locally (still needs valid .env + config.yaml + data/digest.session):
SESSION_PATH=./data/digest CONFIG_PATH=./config.yaml \
  python main.py
```

## License

[MIT](LICENSE).
