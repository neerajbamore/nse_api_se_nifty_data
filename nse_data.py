#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NIFTY Option Chain + Futures Monitor for Render + Telegram Alerts
By Neeraj — Modified for Render deployment and Telegram notifications.
Environment variables used:
  - TELEGRAM_BOT_TOKEN (required)
  - TELEGRAM_CHAT_ID  (required)  -> user/chat or channel id (add bot to channel)
  - POLL_SECONDS (optional, default 138)
  - SYMBOL (optional, default NIFTY)
  - STRIKE_STEP, OTM_COUNT (optional)
  - SEND_EVERY_RUN (optional, "1" to send every refresh; default "1")
"""

import os, sys, time, signal, requests, logging
from datetime import datetime, timezone, timedelta
from tabulate import tabulate

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nifty-monitor")

# ===== Settings from env =====
SYMBOL = os.environ.get("SYMBOL", "NIFTY")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "138"))
STRIKE_STEP = int(os.environ.get("STRIKE_STEP", "50"))
OTM_COUNT = int(os.environ.get("OTM_COUNT", "5"))
TIMEZONE = timezone(timedelta(hours=5, minutes=30))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SEND_EVERY_RUN = os.environ.get("SEND_EVERY_RUN", "1") == "1"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Telegram alerts will be disabled.")

# NSE endpoints
OC_URL = f"https://www.nseindia.com/api/option-chain-indices?symbol={SYMBOL}"
DERIV_URL = f"https://www.nseindia.com/api/quote-derivative?symbol={SYMBOL}"

session = requests.Session()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.nseindia.com/",
}

prev_option_snapshot = {}
prev_fut_snapshot = None

# ===== Helpers =====
def warmup():
    for url in ["https://www.nseindia.com", f"https://www.nseindia.com/option-chain?symbol={SYMBOL}"]:
        try:
            session.get(url, headers=HEADERS, timeout=10)
        except Exception:
            pass

def fetch_json(url, timeout=15):
    r = session.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

def fetch_option_chain():
    return fetch_json(OC_URL)

def extract_data(js):
    expiry = js["records"]["expiryDates"][0]
    underlying = js["records"]["underlyingValue"]
    data = [d for d in js["records"]["data"] if d["expiryDate"] == expiry]
    return expiry, underlying, data

def deep_find_futures(js):
    # same recursive search as original to find oi/volume
    def deep_find(obj):
        if isinstance(obj, dict):
            oi = None; vol = None
            for k, v in obj.items():
                lk = k.lower()
                if "open" in lk and "interest" in lk and isinstance(v, (int, float)):
                    oi = v
                if "volume" in lk and isinstance(v, (int, float)):
                    vol = v
            if oi is not None and vol is not None:
                return oi, vol
            for v in obj.values():
                res = deep_find(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = deep_find(item)
                if res:
                    return res
        return None
    return deep_find(js)

def fetch_futures():
    try:
        js = fetch_json(DERIV_URL)
        res = deep_find_futures(js)
        if res:
            return res
        log.warning("Futures OI/Vol fields not found in derivative JSON.")
        return 0,0
    except Exception as e:
        log.error("Futures fetch error: %s", e)
        return 0,0

def round_strike(u): return int(round(u / STRIKE_STEP) * STRIKE_STEP)
def pick_strikes(a):
    return [a - STRIKE_STEP, a] + [a + i*STRIKE_STEP for i in range(1, OTM_COUNT+1)], \
           [a + STRIKE_STEP, a] + [a - i*STRIKE_STEP for i in range(1, OTM_COUNT+1)]

def lookup(data, strike, t):
    for d in data:
        if int(d["strikePrice"]) == strike and t in d:
            leg = d[t]
            return int(leg.get("openInterest",0)), int(leg.get("totalTradedVolume",0)), float(leg.get("impliedVolatility",0.0))
    return 0,0,0.0

def delta(curr, prev):
    if prev is None: return None
    return tuple(a-b for a,b in zip(curr,prev))

def format_number(n):
    return f"{n:,}"

def send_telegram(text, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False

# ===== Main logic =====
def build_message(now, expiry, underlying, atm, sum_call_coi, sum_put_coi, avg_call_iv, avg_put_iv, fut_delta):
    # HTML formatted message for Telegram (bold headings, code for numbers)
    fut_text = f"ΔOI: <b>{format_number(fut_delta[0]) if fut_delta[0] is not None else '—'}</b> | ΔVol: <b>{format_number(fut_delta[1]) if fut_delta[1] is not None else '—'}</b>"
    msg = (
        f"<b>[{now}] {SYMBOL} Option Snapshot</b>%0A"
        f"Expiry: <b>{expiry}</b>%0A"
        f"Underlying: <b>{underlying:.2f}</b> | ATM: <b>{atm}</b>%0A%0A"
        f"<b>Calls</b> Total COI: <code>{format_number(sum_call_coi)}</code> | Avg IV: <code>{avg_call_iv:.2f}</code>%0A"
        f"<b>Puts</b> Total COI: <code>{format_number(sum_put_coi)}</code> | Avg IV: <code>{avg_put_iv:.2f}</code>%0A%0A"
        f"<b>Futures Δ</b> {fut_text}%0A%0A"
        f"<i>Note:</i> Ye summary har refresh pe bheja ja sakta hai. Agar zyada alerts aa rahe ho to POLL_SECONDS badha do ya SEND_EVERY_RUN=0 set karo."
    )
    # decode %0A handled by Telegram if passed raw newlines; we are using URL-encoded newlines here for safety
    # but send_telegram uses requests form data so Telegram will accept plain newlines. We'll replace %0A -> \n
    return msg.replace("%0A", "\n")

def main_loop():
    warmup()
    global prev_option_snapshot, prev_fut_snapshot
    first = True
    while True:
        try:
            expiry, underlying, data = extract_data(fetch_option_chain())
            atm = round_strike(underlying)
            call_strikes, put_strikes = pick_strikes(atm)

            sum_call_coi=sum_put_coi=0
            sum_call_iv=sum_put_iv=0.0
            count_calls=count_puts=0

            # iterate calls
            for s in call_strikes:
                oi,vol,iv = lookup(data, s, "CE")
                prev = prev_option_snapshot.get((s,"CE"))
                d = delta((oi,vol,iv), prev)
                if d is not None:
                    coi,cvol,civ = d
                else:
                    coi=cvol=civ=None
                prev_option_snapshot[(s,"CE")] = (oi,vol,iv)
                sum_call_coi += oi
                sum_call_iv += iv
                count_calls += 1

            # iterate puts
            for s in put_strikes:
                oi,vol,iv = lookup(data, s, "PE")
                prev = prev_option_snapshot.get((s,"PE"))
                d = delta((oi,vol,iv), prev)
                if d is not None:
                    coi,cvol,civ = d
                else:
                    coi=cvol=civ=None
                prev_option_snapshot[(s,"PE")] = (oi,vol,iv)
                sum_put_coi += oi
                sum_put_iv += iv
                count_puts += 1

            avg_call_iv = sum_call_iv / max(1, count_calls)
            avg_put_iv = sum_put_iv / max(1, count_puts)

            fut_oi, fut_vol = fetch_futures()
            fd = delta((fut_oi, fut_vol), prev_fut_snapshot)
            fut_coi, fut_cvol = (None, None) if fd is None else fd
            prev_fut_snapshot = (fut_oi, fut_vol)

            now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            # console output for debugging on Render logs
            log.info("[%s] %s | Underlying %.2f | ATM %d", now, SYMBOL, underlying, atm)
            log.info("Calls COI: %s | Puts COI: %s | AvgIV C/P: %.2f / %.2f", format_number(sum_call_coi), format_number(sum_put_coi), avg_call_iv, avg_put_iv)
            log.info("Futures ΔOI=%s ΔVol=%s", format_number(fut_coi) if fut_coi is not None else "—", format_number(fut_cvol) if fut_cvol is not None else "—")

            # Telegram message (send if enabled)
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and SEND_EVERY_RUN:
                msg = build_message(now, expiry, underlying, atm, sum_call_coi, sum_put_coi, avg_call_iv, avg_put_iv, (fut_coi, fut_cvol))
                ok = send_telegram(msg)
                log.info("Telegram sent: %s", ok)

            if first:
                log.info("First run completed. Subsequent runs will show deltas.")
                first = False

        except Exception as e:
            log.exception("Main loop error: %s", e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s,f: sys.exit(0))
    main_loop()
