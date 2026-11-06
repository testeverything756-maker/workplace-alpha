#!/usr/bin/env python3
"""
BTC 15-minute alert bot  (SELL and BUY side)  ->  Telegram

SELL side: Supertrend RED   + green candle closes -> entry when price breaks BELOW its LOW
BUY  side: Supertrend GREEN + red candle closes   -> entry when price breaks ABOVE its HIGH

Messages sent for each side:
  1) SETUP READY   - setup candle closed (shows candle size)
  2) CROSSED OPEN  - early warning: price crossed the setup candle's OPEN
  3) ENTRY         - price broke the entry level (low for sell, high for buy)

Needs only Python 3.9+ (no extra packages).

Run once   :  python btc_alert.py
Run forever:  python btc_alert.py --loop      (checks every 20 seconds)
Test msg   :  python btc_alert.py --test
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ================= SETTINGS =================
RR = 5.0              # Risk:Reward (trail starts after this)
MAX_SL = 200.0        # Max stoploss in points
CAP_SL = True         # True = cap SL at max, False = skip trade if SL bigger
ATR_LEN = 10          # Supertrend ATR length
FACTOR = 3.0          # Supertrend factor
RECENT_BARS = 6       # ignore events older than 6 candles (1.5 hours)
CHECK_EVERY_SECONDS = 20   # only used with --loop
STATE_FILE = "state.json"
TF_SECONDS = 15 * 60

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
IST = timezone(timedelta(hours=5, minutes=30))


# ================= DATA =================
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def from_binance(url):
    data = http_get(url)
    return [dict(t=int(k[0]) // 1000, o=float(k[1]), h=float(k[2]),
                 l=float(k[3]), c=float(k[4])) for k in data]


def from_kraken(url):
    data = http_get(url)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    rows = [v for k, v in data["result"].items() if k != "last"][0]
    return [dict(t=int(k[0]), o=float(k[1]), h=float(k[2]),
                 l=float(k[3]), c=float(k[4])) for k in rows]


SOURCES = [
    ("Binance", from_binance,
     "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=1000"),
    ("Binance", from_binance,
     "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=1000"),
    ("Kraken", from_kraken,
     "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=15"),
]


def fetch_bars():
    last_err = None
    for name, fn, url in SOURCES:
        try:
            bars = fn(url)
            bars.sort(key=lambda b: b["t"])
            if len(bars) > 100:
                return name, bars
        except Exception as e:  # try next source
            last_err = e
            print(f"[data] {name} failed: {e}")
    raise RuntimeError(f"All data sources failed. Last error: {last_err}")


# ================= SUPERTREND (same maths as TradingView) =================
def supertrend_dir(bars, length, factor):
    """Returns list of direction: 1 = downtrend (RED), -1 = uptrend (GREEN)."""
    n = len(bars)
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    atr = [None] * n
    if n >= length:
        atr[length - 1] = sum(tr[:length]) / length
        for i in range(length, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length

    direction = [1] * n
    upper = [None] * n
    lower = [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        b = bars[i]
        src = (b["h"] + b["l"]) / 2
        ub = src + factor * atr[i]
        lb = src - factor * atr[i]
        if i > 0 and upper[i - 1] is not None:
            pl, pu, pc = lower[i - 1], upper[i - 1], bars[i - 1]["c"]
            lb = lb if (lb > pl or pc < pl) else pl
            ub = ub if (ub < pu or pc > pu) else pu
        lower[i], upper[i] = lb, ub
        if i == 0 or atr[i - 1] is None:
            direction[i] = 1
        elif direction[i - 1] == 1:
            direction[i] = -1 if b["c"] > ub else 1
        else:
            direction[i] = 1 if b["c"] < lb else -1
    return direction


# ================= STRATEGY REPLAY =================
def make_setup(side, b):
    """Build a setup from the setup candle b (green candle for short,
    red candle for long)."""
    raw = b["h"] - b["l"]
    too_big = raw > MAX_SL
    risk = (MAX_SL if CAP_SL else None) if too_big else raw
    if side == "short":
        level = b["l"]
        sl = level + risk if risk is not None else None
        tp = level - risk * RR if risk is not None else None
    else:
        level = b["h"]
        sl = level - risk if risk is not None else None
        tp = level + risk * RR if risk is not None else None
    return dict(side=side, id=f"{side}:{b['t']}", t=b["t"], low=b["l"],
                high=b["h"], open=b["o"], close=b["c"], raw=raw, risk=risk,
                capped=too_big, level=level, sl=sl, tp=tp,
                announced=False, warned=False)


def replay(bars, direction):
    """Walks through closed candles exactly like the strategy and returns
    (events, final_state). One trade at a time, SELL and BUY side."""
    events = []
    pos = False
    pos_side = None
    entry_px = None
    g = None                    # current setup
    pending = False
    sl_px = exit_stop = trail_stop = None
    trail_on = False

    for j, b in enumerate(bars):
        closed_trade = False

        # 1) exit of an open trade (stop or trailing stop)
        if pos:
            if pos_side == "short":
                hit = b["h"] >= exit_stop
            else:
                hit = b["l"] <= exit_stop
            if hit:
                pos = False
                closed_trade = True

        # 2) early warning (price crosses setup candle OPEN) and entry
        if (not pos) and pending and g is not None:
            if g["side"] == "short":
                warn_hit = b["l"] <= g["open"]
                entry_hit = b["l"] <= g["level"]
            else:
                warn_hit = b["h"] >= g["open"]
                entry_hit = b["h"] >= g["level"]
            if warn_hit and not g["warned"]:
                g["warned"] = True
                events.append(dict(type="warn", id="warn:" + g["id"], t=b["t"],
                                   setup=g, live=False))
            if entry_hit:
                if g["side"] == "short":
                    entry_px = min(b["o"], g["level"])
                else:
                    entry_px = max(b["o"], g["level"])
                pos = True
                pos_side = g["side"]
                trail_on = False
                trail_stop = None
                exit_stop = sl_px
                events.append(dict(type="entry", id="entry:" + g["id"], t=b["t"],
                                   setup=g, price=entry_px, live=False))
                # stopped out in the very same candle
                if (pos_side == "short" and b["h"] >= sl_px) or \
                   (pos_side == "long" and b["l"] <= sl_px):
                    pos = False
                    closed_trade = True

        # 3) logic at candle close
        is_red = direction[j] > 0
        want = "short" if is_red else "long"
        if closed_trade:
            g = None
        if not pos:
            trail_on = False
            trail_stop = None
            if g is not None and g["side"] != want:     # Supertrend colour flipped
                g = None
            is_setup_candle = (b["c"] > b["o"]) if want == "short" else (b["c"] < b["o"])
            if is_setup_candle:
                g = make_setup(want, b)                 # newest candle replaces old

            valid = False
            if g is not None and g["risk"] is not None:
                if g["side"] == "short":
                    valid = b["c"] > g["level"]
                else:
                    valid = b["c"] < g["level"]
            if valid:
                sl_px = g["sl"]
                pending = True
                if not g["announced"]:
                    g["announced"] = True
                    events.append(dict(type="setup", id="setup:" + g["id"],
                                       t=b["t"], setup=g))
            else:
                pending = False
        else:
            pending = False
            if pos_side == "short":
                risk_real = sl_px - entry_px
                tp_px = entry_px - risk_real * RR
                if (not trail_on) and b["l"] <= tp_px:
                    trail_on = True
                if trail_on:
                    trail_stop = min(trail_stop if trail_stop is not None else sl_px, b["h"])
                    exit_stop = trail_stop
                else:
                    exit_stop = sl_px
            else:
                risk_real = entry_px - sl_px
                tp_px = entry_px + risk_real * RR
                if (not trail_on) and b["h"] >= tp_px:
                    trail_on = True
                if trail_on:
                    trail_stop = max(trail_stop if trail_stop is not None else sl_px, b["l"])
                    exit_stop = trail_stop
                else:
                    exit_stop = sl_px

    return events, dict(pos=pos, pending=pending, setup=g)


# ================= MESSAGES =================
def pts(x):
    return f"{x:,.1f}"


def ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M")


def lines_common(s):
    cap = f" - candle was {pts(s['raw'])}, SL capped at {MAX_SL:.0f}" if s["capped"] else ""
    return (f"Stoploss: {pts(s['sl'])}  (risk {pts(s['risk'])} pts{cap})\n"
            f"1:{RR:g} level: {pts(s['tp'])}")


def msg_setup(ev):
    s = ev["setup"]
    body = abs(s["close"] - s["open"])
    if s["side"] == "short":
        head = ("\U0001F534 BTC 15m - SELL SETUP READY\n"
                f"Green candle {ist(s['t'])} IST closed while Supertrend is RED\n\n")
        trig = f"SELL trigger (candle low): {pts(s['level'])}\n"
        wait = "You will get a warning when price crosses below the candle open."
        kind = "Green"
    else:
        head = ("\U0001F7E2 BTC 15m - BUY SETUP READY\n"
                f"Red candle {ist(s['t'])} IST closed while Supertrend is GREEN\n\n")
        trig = f"BUY trigger (candle high): {pts(s['level'])}\n"
        wait = "You will get a warning when price crosses above the candle open."
        kind = "Red"
    return (head
            + f"{kind} candle size (High-Low): {pts(s['raw'])} pts\n"
            + f"{kind} candle body (Open-Close): {pts(body)} pts\n\n"
            + f"Early-warning level (candle open): {pts(s['open'])}\n"
            + trig + lines_common(s) + "\n\n" + wait)


def msg_warn(ev):
    s = ev["setup"]
    gap = abs(s["open"] - s["level"])
    if s["side"] == "short":
        head = "\u26A0\uFE0F BTC 15m - SELL: PRICE CROSSED BELOW GREEN CANDLE OPEN\n"
        trig = f"SELL entry (candle low): {pts(s['level'])}  [{pts(gap)} pts below the open]\n"
        kind = "Green"
    else:
        head = "\u26A0\uFE0F BTC 15m - BUY: PRICE CROSSED ABOVE RED CANDLE OPEN\n"
        trig = f"BUY entry (candle high): {pts(s['level'])}  [{pts(gap)} pts above the open]\n"
        kind = "Red"
    return (head
            + f"({ist(ev['t'])} IST candle) Entry level not hit yet - get ready!\n\n"
            + f"Crossed level (candle open): {pts(s['open'])}\n"
            + trig
            + f"{kind} candle size (High-Low): {pts(s['raw'])} pts\n"
            + lines_common(s))


def msg_entry(ev):
    s = ev["setup"]
    if s["side"] == "short":
        head = ("\U0001F6A8 BTC 15m - SELL ENTRY TRIGGERED\n"
                f"Price crossed below green candle low ({ist(ev['t'])} IST candle)\n\n")
        kind = "Green"
    else:
        head = ("\U0001F6A8 BTC 15m - BUY ENTRY TRIGGERED\n"
                f"Price crossed above red candle high ({ist(ev['t'])} IST candle)\n\n")
        kind = "Red"
    return (head
            + f"Entry level: {pts(s['level'])}\n"
            + f"{kind} candle size (High-Low): {pts(s['raw'])} pts\n"
            + lines_common(s))


def send(text):
    if not TOKEN or not CHAT_ID:
        print("Telegram not configured. Message would be:\n" + text)
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        ok = json.loads(r.read().decode()).get("ok", False)
    return ok


# ================= STATE =================
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"sent": []}


def save_state(st):
    st["sent"] = st["sent"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


# ================= MAIN CHECK =================
def run_once():
    state = load_state()
    source, bars = fetch_bars()
    now = time.time()
    closed = [b for b in bars if b["t"] + TF_SECONDS <= now]
    forming = [b for b in bars if b["t"] + TF_SECONDS > now]

    direction = supertrend_dir(closed, ATR_LEN, FACTOR)
    events, fin = replay(closed, direction)

    # live check: has the running (unfinished) candle already crossed a level?
    if fin["pending"] and not fin["pos"] and forming and fin["setup"]:
        f = forming[-1]
        s = fin["setup"]
        if s["side"] == "short":
            warn_hit, entry_hit = f["l"] <= s["open"], f["l"] <= s["level"]
        else:
            warn_hit, entry_hit = f["h"] >= s["open"], f["h"] >= s["level"]
        if warn_hit and not s["warned"]:
            events.append(dict(type="warn", id="warn:" + s["id"], t=f["t"],
                               setup=s, live=True))
        if entry_hit:
            px = min(f["o"], s["level"]) if s["side"] == "short" else max(f["o"], s["level"])
            events.append(dict(type="entry", id="entry:" + s["id"], t=f["t"],
                               setup=s, price=px, live=True))

    rank = {"setup": 0, "warn": 1, "entry": 2}
    events.sort(key=lambda e: (e["t"], rank[e["type"]]))
    cutoff = now - RECENT_BARS * TF_SECONDS
    sent_something = False
    for ev in events:
        if ev["t"] < cutoff or ev["id"] in state["sent"]:
            continue
        text = {"setup": msg_setup, "warn": msg_warn, "entry": msg_entry}[ev["type"]](ev)
        if send(text):
            state["sent"].append(ev["id"])
            sent_something = True
            print(f"[sent] {ev['id']}")
    save_state(state)
    last = closed[-1]
    print(f"[ok] {source} | last closed {ist(last['t'])} IST close {last['c']:.1f} | "
          f"Supertrend {'RED' if direction[-1] > 0 else 'GREEN'} | "
          f"alerts sent: {sent_something}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Sent:", send("\u2705 Test message - your BTC alert bot is connected."))
    elif "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                print("[error]", e)
            time.sleep(CHECK_EVERY_SECONDS)
    else:
        run_once()
