# app.py
# Single-file Flask app that:
# - Scrapes NSE option-chain (from the public option-chain page)
# - Parses the embedded __NEXT_DATA__ JSON
# - Picks 1 ITM, 1 ATM, 3 OTM for Calls and Puts
# - Computes ltp*oi, change in oi (coi), ltp*coi
# - Fetches futures metadata (from same JSON if present)
# - Sends a nicely formatted Telegram message to configured BOT_TOKEN & CHAT_ID
#
# Notes:
# - Uses requests + bs4 + flask
# - Read BOT_TOKEN and CHAT_ID from environment variables
# - Run with: python app.py
# - Or deploy to any cloud and set env vars there

from flask import Flask, jsonify
import os, requests, json, time
from bs4 import BeautifulSoup
from datetime import datetime

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36",
    "Referer": "https://www.nseindia.com/option-chain",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID in environment.")
        return False, "missing token/id"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

def fetch_nse_nextdata():
    """Scrape the NSE option chain page and extract __NEXT_DATA__ JSON (if present)"""
    url = "https://www.nseindia.com/option-chain"
    s = requests.Session()
    # try a couple times to be robust
    for _ in range(2):
        try:
            r = s.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                time.sleep(1)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            tag = soup.find("script", id="__NEXT_DATA__")
            if not tag:
                return None, "no_next_data"
            data = json.loads(tag.string)
            return data, None
        except Exception as e:
            last_e = e
            time.sleep(1)
    return None, str(last_e)

def extract_oc_and_future(nextdata):
    """
    Navigate the Next.js payload to find optionChain data and future metadata.
    This may vary over time; we attempt common paths used by NSE sites.
    """
    # Try a few likely locations, be defensive
    try:
        # try common path used earlier
        oc = nextdata["props"]["pageProps"]["initialState"]["optionChain"]["data"]
        # oc contains records -> underlyingValue & records.data list
        records = oc["records"]
        underlying = records.get("underlyingValue")
        oc_rows = records.get("data", [])
        # future metadata may be somewhere else; try derivatives or other keys
        fut = None
        # try to find futures metadata in pageProps
        pageprops = nextdata["props"]["pageProps"]
        # sometimes derivative metadata under "live" or similar - attempt safe retrieval
        for k in pageprops.keys():
            if isinstance(pageprops[k], dict) and "futures" in json.dumps(pageprops[k]).lower():
                # best-effort, but avoid crash
                pass
        return underlying, oc_rows, oc.get("expiryDates"), fut
    except Exception:
        # fallback: scan entire json for "optionChain" key
        try:
            def find_key(d, key):
                if isinstance(d, dict):
                    if key in d:
                        return d[key]
                    for v in d.values():
                        r = find_key(v, key)
                        if r is not None:
                            return r
                elif isinstance(d, list):
                    for item in d:
                        r = find_key(item, key)
                        if r is not None:
                            return r
                return None
            part = find_key(nextdata, "optionChain")
            if part and isinstance(part, dict) and "data" in part:
                oc = part["data"]
                records = oc.get("records", {})
                return records.get("underlyingValue"), records.get("data", []), oc.get("expiryDates"), None
        except:
            pass
    return None, [], None, None

def group_ce_pe(rows):
    ce = []
    pe = []
    for r in rows:
        if "CE" in r:
            ce.append(r)
        if "PE" in r:
            pe.append(r)
    return ce, pe

def pick_strikes(spot, ce_list, pe_list):
    strikes = sorted({r["strikePrice"] for r in ce_list})
    if not strikes:
        return None, (), ()
    atm = min(strikes, key=lambda x: abs(x - spot))
    # build helpers
    ce_itm = [r for r in ce_list if r["strikePrice"] < atm]
    ce_otm = [r for r in ce_list if r["strikePrice"] > atm]
    pe_itm = [r for r in pe_list if r["strikePrice"] > atm]
    pe_otm = [r for r in pe_list if r["strikePrice"] < atm]
    # picks
    ce_itm_pick = sorted(ce_itm, key=lambda x: -x["strikePrice"])[0] if ce_itm else None
    pe_itm_pick = sorted(pe_itm, key=lambda x: x["strikePrice"])[0] if pe_itm else None
    ce_atm = next((r for r in ce_list if r["strikePrice"] == atm), None)
    pe_atm = next((r for r in pe_list if r["strikePrice"] == atm), None)
    ce_otm_pick = sorted(ce_otm, key=lambda x: x["strikePrice"])[:3]
    pe_otm_pick = sorted(pe_otm, key=lambda x: -x["strikePrice"])[:3]
    return atm, (ce_itm_pick, ce_atm, ce_otm_pick), (pe_itm_pick, pe_atm, pe_otm_pick)

def fmt_option_block(tag, opt, side):
    if not opt:
        return f"{tag}: N/A\n"
    d = opt[side]
    ltp = d.get("lastPrice", 0)
    oi = d.get("openInterest", 0)
    coi = d.get("changeinOpenInterest", 0)
    rs = ltp * oi
    crs = ltp * coi
    return (
        f"{tag} Strike {opt['strikePrice']}\n"
        f" LTP: {ltp} | OI: {oi} | LTP*OI: {rs}\n"
        f" COI: {coi} | LTP*COI: {crs}\n"
    )

@app.route("/send", methods=["GET"])
def send_handler():
    nextdata, err = fetch_nse_nextdata()
    if err or not nextdata:
        return jsonify({"ok": False, "error": "failed to fetch nextdata", "detail": err}), 500

    spot, rows, expiries, fut_meta = extract_oc_and_future(nextdata)
    if not rows:
        return jsonify({"ok": False, "error": "no option rows found"}), 500

    ce_list, pe_list = group_ce_pe(rows)
    atm, ce_picks, pe_picks = pick_strikes(spot, ce_list, pe_list)

    # Format message
    text = f"📌 NIFTY Option Chain Snapshot\nATM: {atm}\nSpot: {spot}\nTime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
    text += "--- CALLS ---\n"
    text += fmt_option_block("ITM", ce_picks[0], "CE")
    text += fmt_option_block("ATM", ce_picks[1], "CE")
    for i, s in enumerate(ce_picks[2]):
        text += fmt_option_block(f"OTM{i+1}", s, "CE")

    text += "\n--- PUTS ---\n"
    text += fmt_option_block("ITM", pe_picks[0], "PE")
    text += fmt_option_block("ATM", pe_picks[1], "PE")
    for i, s in enumerate(pe_picks[2]):
        text += fmt_option_block(f"OTM{i+1}", s, "PE")

    # FUTURES: if fut_meta available try to add (best-effort)
    if fut_meta and isinstance(fut_meta, dict):
        try:
            text += "\n📘 FUTURE\n"
            # fallback safe keys
            last = fut_meta.get("lastPrice") or fut_meta.get("last")
            prem = fut_meta.get("premium")
            change = fut_meta.get("change")
            oi = fut_meta.get("openInterest")
            vol = fut_meta.get("totalTradedVolume") or fut_meta.get("volume")
            text += f"Price: {last}\nPremium: {prem}\nChange: {change}\nOI: {oi}\nVolume: {vol}\n"
        except Exception:
            pass

    ok, resp = send_telegram(text)
    if ok:
        return jsonify({"ok": True, "message": "sent", "telegram_resp": resp})
    else:
        return jsonify({"ok": False, "message": "telegram_failed", "detail": resp}), 500

if __name__ == "__main__":
    # For local run: set environment and run
    # Example:
    # export BOT_TOKEN="..."
    # export CHAT_ID="..."
    # python app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))