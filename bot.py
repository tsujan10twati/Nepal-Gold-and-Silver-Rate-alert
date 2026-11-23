#!/usr/bin/env python3
"""
Telegram bot: sends Nepal gold & silver prices whenever they change.

Price source: sharehubnepal.com (more pages can be added to SOURCE_URLS as fallbacks).

Setup
-----
1. In Telegram, talk to @BotFather -> /newbot -> copy the token.
2. Message your bot (press Start), then open
   https://api.telegram.org/bot<TOKEN>/getUpdates  and copy  "chat":{"id": ...}
   (groups have negative ids; for a channel use @channelname)
3. pip install requests beautifulsoup4
4. export TELEGRAM_BOT_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_IDS="111111111,-100222222222"      # comma-separated
5. python bot.py --debug      # shows what each source returns, sends nothing
   python bot.py --force      # send the current price once, as a test
   python bot.py              # run forever, checking every 10 minutes
   python bot.py --once       # single check (for cron / GitHub Actions)
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOURCE_URLS = {
    "sharehub": "https://sharehubnepal.com/economy/gold-silver-rates",
}
SOURCES = [s.strip() for s in os.environ.get("SOURCES", "sharehub").split(",") if s.strip()]
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]


# ---------------------------------------------------------------- scraping
def fetch_text(url, attempts=2):
    r = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code < 400:
                break
            print(f"  attempt {attempt}: HTTP {r.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  attempt {attempt}: {e}", file=sys.stderr)
        if attempt < attempts:
            time.sleep(10)
    if r is None:
        raise RuntimeError("no response")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


NUM = r"\d{1,3}(?:,\d{2,3})+"  # numbers with thousands separators: 2,42,600 / 242,600 / 3,065


def _price_after(label, text):
    """Take the first two comma-formatted numbers after `label` (stopping before the
    next 'Gold'/'Silver' label) and return the larger one: a tola is heavier than
    10 g, so the per-tola price is always the bigger of the pair."""
    pattern = label + r"((?:(?!Gold|Silver).){0,160})"
    for m in re.finditer(pattern, text):
        nums = [int(n.replace(",", "")) for n in re.findall(NUM, m.group(1))[:2]]
        if nums:
            return max(nums)
    return None


def parse(text):
    gold = _price_after(r"[Gg]old\s*\(?\s*[Hh]allmark", text)
    silver = _price_after(r"Silver", text)
    m = re.search(r"Last\s*updated:?\s*(.{0,40}?\d{1,2}:\d{2}\s*[ap]m)", text, re.I) \
        or re.search(r"updated on\s+(\d{1,2}\s+\w+\s+\d{4})", text, re.I)
    updated = re.sub(r"\s*\((?:today|yesterday)\)", "", m.group(1), flags=re.I).strip() if m else ""
    if not gold or not silver or gold < silver * 10:  # sanity check
        raise ValueError(f"could not parse prices (gold={gold}, silver={silver}); page starts: {text[:200]!r}")
    return {"gold": gold, "silver": silver, "updated": updated}


def parse_sharehub(text):
    """sharehubnepal.com lays out cards: HALMARK GOLD [1 TOLA Rs. x, 10 GRAMS Rs. y],
    TEJABI GOLD [...], SILVER [...]. Read the '1 TOLA' price inside each card."""
    hall = re.search(r"HAL+MARK GOLD", text)
    if not hall:
        raise ValueError(f"hallmark card not found; page starts: {text[:200]!r}")
    tej = re.search(r"TEJABI GOLD", text[hall.end():])
    tej_end = hall.end() + tej.end() if tej else hall.end()
    silver = re.search(r"SILVER", text[tej_end:])
    if not silver:
        raise ValueError(f"silver card not found; page starts: {text[:200]!r}")
    silver_start = tej_end + silver.start()
    tola = r"1\s*TOLA\D{0,15}?(" + NUM + ")"
    g = re.search(tola, text[hall.end():hall.end() + 400])
    s = re.search(tola, text[silver_start:silver_start + 400])
    gold_v = int(g.group(1).replace(",", "")) if g else None
    silver_v = int(s.group(1).replace(",", "")) if s else None
    m = re.search(r"Updated:\s*(\d{4}-\d{2}-\d{2})", text)
    updated = m.group(1) if m else ""
    if not gold_v or not silver_v or gold_v < silver_v * 10:
        raise ValueError(f"could not parse prices (gold={gold_v}, silver={silver_v}); page starts: {text[:200]!r}")
    return {"gold": gold_v, "silver": silver_v, "updated": updated}


PARSERS = {"sharehub": parse_sharehub}  # everything else uses parse()


def get_prices():
    errors = []
    for name in SOURCES:
        url = SOURCE_URLS.get(name)
        if not url:
            continue
        try:
            print(f"Trying {name} ...")
            cur = PARSERS.get(name, parse)(fetch_text(url))
            cur["source"] = name
            return cur
        except Exception as e:
            print(f"  {name} failed: {e}", file=sys.stderr)
            errors.append(f"{name}: {e}")
    raise RuntimeError("all sources failed -> " + " | ".join(errors))


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
    """Change versus yesterday's price, on its own indented line."""
    if not old:
        return ""
    if new == old:
        return "\n      ▬ same as yesterday"
    arrow = "🔺" if new > old else "🔻"
    pct = (new - old) / old * 100
    return f"\n      {arrow} {npr(abs(new - old))} ({pct:+.1f}%) since yesterday"


def build_message(cur, yday):
    yday = yday or {}
    lines = [
        "🪙 <b>Gold &amp; Silver — Nepal</b>",
        "",
        f"🥇 Gold (Hallmark): <b>Rs {npr(cur['gold'])}</b> /tola{delta(cur['gold'], yday.get('gold'))}",
        f"🥈 Silver: <b>Rs {npr(cur['silver'])}</b> /tola{delta(cur['silver'], yday.get('silver'))}",
    ]
    if cur["updated"]:
        lines += ["", f"🕚 {html.escape(cur['updated'])}"]
    lines.append(f"<i>Source: {html.escape(cur.get('source', ''))}</i>")
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


NPT = timezone(timedelta(hours=5, minutes=45))  # Nepal time


def today_npt():
    return datetime.now(NPT).strftime("%Y-%m-%d")


def yesterday_prices(state):
    """Last price saved on an earlier day (Nepal time) = the previous day's final price."""
    hist = (state or {}).get("history", {})
    earlier = sorted(d for d in hist if d < today_npt())
    return hist[earlier[-1]] if earlier else None


def check(force=False):
    cur = get_prices()
    state = load_state() or {}
    changed = (state.get("gold"), state.get("silver")) != (cur["gold"], cur["silver"])
    if force or changed:
        send(build_message(cur, yesterday_prices(state)))
        hist = state.get("history", {})
        hist[today_npt()] = {"gold": cur["gold"], "silver": cur["silver"]}  # latest price of today
        for d in sorted(hist)[:-14]:  # keep two weeks
            del hist[d]
        cur["history"] = hist
        STATE_FILE.write_text(json.dumps(cur))
        print("Sent update:", cur)
    else:
        print("No change:", cur)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--force", action="store_true", help="send current price even if unchanged")
    ap.add_argument("--debug", action="store_true", help="show what each source returns, send nothing")
    args = ap.parse_args()

    if args.debug:
        for name in SOURCES:
            print(f"\n=== {name} ===")
            try:
                text = fetch_text(SOURCE_URLS[name])
                print("Page text (first 500 chars):", text[:500])
                print("Parsed:", PARSERS.get(name, parse)(text))
            except Exception as e:
                print("FAILED:", e)
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
                send(f"⚠️ Gold bot can't read any price source: {html.escape(str(e)[:500])}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
