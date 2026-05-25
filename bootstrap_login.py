"""One-time interactive Telethon login (file-callback flavour).

Designed to run inside a Docker container on a remote host where stdin
isn't easily piped from the operator's terminal. Instead of prompting,
the script waits for the login code (and optionally the 2FA password)
to appear as files at /app/data/code.txt and /app/data/2fa.txt.

Flow:

    1. Run this script.
    2. Telegram sends a login code to the phone (via Telegram itself if
       another client is logged in, otherwise via SMS).
    3. Write the code into /app/data/code.txt — the script picks it up.
    4. If the account has 2FA, write the cloud password into
       /app/data/2fa.txt.
    5. The session is saved to /app/data/digest.session and the script
       exits.

Required environment:
    TG_API_ID, TG_API_HASH, TG_PHONE.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient


async def file_callback(path: Path, label: str) -> str:
    print(f"[{label}] waiting for {path} — write the value into that file", flush=True)
    while not path.exists():
        await asyncio.sleep(1)
    value = path.read_text().strip()
    path.unlink()
    return value


async def main() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    phone = os.environ["TG_PHONE"]

    data_dir = Path(os.environ.get("DATA_DIR", "/app/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    session_path = str(data_dir / "digest")
    code_path = data_dir / "code.txt"
    pw_path = data_dir / "2fa.txt"

    print(f"phone: {phone}  session: {session_path}.session", flush=True)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start(
        phone=phone,
        code_callback=lambda: file_callback(code_path, "CODE"),
        password=lambda: file_callback(pw_path, "2FA-PASSWORD"),
    )
    me = await client.get_me()
    print(f"OK — logged in as {me.first_name} (id={me.id})", flush=True)
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyError as e:
        print(f"missing env var: {e}", file=sys.stderr)
        sys.exit(1)
