#!/usr/bin/env python3
"""
Telegram bot: sends Nepal gold & silver prices (from Hamro Patro) whenever they update.

Setup
-----
1. In Telegram, talk to @BotFather -> /newbot -> copy the token.
2. Send any message to your new bot, then open
   https://api.telegram.org/bot<TOKEN>/getUpdates  and copy  "chat":{"id": ...}
   (For a channel: add the bot as admin and use @channelname as the chat id.)
3. pip install requests beautifulsoup4
4. export TELEGRAM_BOT_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_IDS="111111111,@mychannel"      # comma-separated
5. python bot.py --debug      # check the parser reads the page correctly
   python bot.py --force      # send the current price once, as a test
   python bot.py              # run forever, checking every 10 minutes

Run it somewhere that stays on (VPS, Raspberry Pi, Render/Railway/Fly, etc.).
For cron / GitHub Actions use `python bot.py --once` (state is kept in state.json).
"""
import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://www.hamropatro.com/gold"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]


# ---------------------------------------------------------------- scraping
def fetch_text(save_html=False):
    r = requests.get(URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if save_html:
        Path("page.html").write_text(r.text, encoding="utf-8")
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _price_after(label_regex, text):
    # Price = first "Rs 2,42,600" after the label, without running into another
    # label (so headings like "Gold & Silver Price" can't steal the wrong number).
    pattern = label_regex + r"(?:(?!Gold|Silver).){0,200}?Rs\.?\s*([\d,]+)"
    for m in re.finditer(pattern, text, re.I):
        value = int(m.group(1).replace(",", "") or 0)
        if value > 0:
            return value
    return None


def parse(text):
    gold = _price_after(r"Gold\s*\(?\s*Hallmark\s*\)?", text)
    silver = _price_after(r"Silver", text)
    m = re.search(r"Last\s*updated:?\s*(.{0,40}?\d{1,2}:\d{2}\s*[ap]m)", text, re.I)
    updated = re.sub(r"\s*\((?:today|yesterday)\)", "", m.group(1), flags=re.I).strip() if m else ""
    if not gold or not silver or gold < silver * 10:  # sanity check
        raise ValueError(f"Could not parse prices (gold={gold}, silver={silver})")
    return {"gold": gold, "silver": silver, "updated": updated}


# ---------------------------------------------------------------- formatting
def npr(n):
    """Nepali/Indian digit grouping: 242600 -> 2,42,600"""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"{head},{tail}"


def delta(new, old):
    if old is None or new == old:
        return ""
    arrow = "🔺" if new > old else "🔻"
    return f"  {arrow} {npr(abs(new - old))}"


def build_message(cur, prev):
    prev = prev or {}
    lines = [
        "🪙 <b>Gold &amp; Silver — Nepal</b>",
        "",
        f"🥇 Gold (Hallmark): <b>Rs {npr(cur['gold'])}</b> /tola{delta(cur['gold'], prev.get('gold'))}",
        f"🥈 Silver: <b>Rs {npr(cur['silver'])}</b> /tola{delta(cur['silver'], prev.get('silver'))}",
    ]
    if cur["updated"]:
        lines += ["", f"🕚 {html.escape(cur['updated'])}"]
    lines.append(f'<a href="{URL}">Hamro Patro</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram
def send(text):
    for chat_id in CHAT_IDS:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            print(f"Telegram error for {chat_id}: {r.status_code} {r.text}", file=sys.stderr)


# ---------------------------------------------------------------- main loop
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def check(force=False):
    cur = parse(fetch_text())
    prev = load_state()
    if force or prev != cur:
        send(build_message(cur, prev))
        STATE_FILE.write_text(json.dumps(cur))
        print("Sent update:", cur)
    else:
        print("No change:", cur)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--force", action="store_true", help="send current price even if unchanged")
    ap.add_argument("--debug", action="store_true", help="print parsed values, save page.html, send nothing")
    args = ap.parse_args()

    if args.debug:
        text = fetch_text(save_html=True)
        print("Page text (first 600 chars):\n", text[:600], "\n")
        print("Parsed:", parse(text))
        return

    if not TOKEN or not CHAT_IDS:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS first.")

    if args.once or args.force:
        check(force=args.force)
        return

    failures = 0
    while True:
        try:
            check()
            failures = 0
        except Exception as e:  # keep running through network hiccups
            failures += 1
            print(f"Check failed ({failures}): {e}", file=sys.stderr)
            if failures == 6:  # ~1 hour of failures -> tell the user once
                send(f"⚠️ Gold bot can't read Hamro Patro: {html.escape(str(e))}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
