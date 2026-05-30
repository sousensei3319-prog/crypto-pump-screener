"""
Crypto Pump-Dump Screener (OKX) - with historical reproducibility analysis.

Strategy (per user's discretionary playbook):
  - Find small/mid-cap perps that pumped (1h>=10% OR 24h>=20%)
  - For each candidate, analyze its OWN history: how many times did it pump
    a similar amount and then dump? (reproducibility = the core edge)
  - Measure typical retrace depth (full vs half) -> concrete TP prices
  - Do NOT scream "short now". Report 4h trend state + downtrend trigger,
    so the user enters AFTER a downtrend confirms (no falling-knife).
  - Add confluence: funding, OI surge, retail long/short ratio, category.
"""

import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

OKX = "https://www.okx.com/api/v5"

# ---- Detection thresholds ----
PUMP_1H = 10.0          # 1h % move to flag a fast spike
PUMP_24H = 20.0         # 24h % move to flag a daily pump
MIN_VOLUME_24H = 2_000_000
EXCLUDE_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP"}  # focus on alts/memes, not majors

# ---- Historical analysis params ----
HIST_DAYS = 300                 # daily candles to analyze
PUMP_EVENT_PCT = 15.0           # a past "pump day" = daily high >= +15% over prev close
FORWARD_WINDOW = 3              # days to look forward for the dump
DUMP_RETRACE_MIN = 0.5          # retrace >= 50% of the pump counts as "dumped"

# ---- Category heuristic (激熱: meme / gamefi / AI). Editable. ----
MEME = {"DOGE","SHIB","PEPE","WIF","BONK","FLOKI","MEME","BOME","MEW","POPCAT","NEIRO",
        "TURBO","BRETT","MOG","PNUT","GOAT","ACT","HIPPO","DOGS","CAT","BABYDOGE","SPX",
        "GIGGLE","FARTCOIN","CHILLGUY","MOODENG","PONKE","RETARDIO","SLERF","MYRO"}
GAMEFI = {"GALA","AXS","SAND","MANA","IMX","PIXEL","BIGTIME","GMT","MAGIC","PYR","ILV",
          "APE","GODS","NAKA","XAI","ACE","PORTAL","ZBCN"}
AI = {"FET","AGIX","RNDR","RENDER","TAO","WLD","AI","ARKM","NMR","OCEAN","GRT","PHA",
      "AIOZ","NFP","TURBO","VIRTUAL","AIXBT","ZEREBRO","GRIFFAIN","AI16Z","ARC","SWARMS"}

JST = timezone(timedelta(hours=9))


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pump-screener/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def okx_get(path):
    data = fetch_json(f"{OKX}{path}")
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error {data.get('code')}: {data.get('msg')}")
    return data["data"]


# ----------------------------------------------------------------------
# Data fetchers
# ----------------------------------------------------------------------
def get_all_tickers():
    data = okx_get("/market/tickers?instType=SWAP")
    return [t for t in data if t["instId"].endswith("-USDT-SWAP")]


def get_candles(inst_id, bar, limit):
    # newest first -> we reverse to oldest first
    data = okx_get(f"/market/candles?instId={inst_id}&bar={bar}&limit={limit}")
    rows = []
    for c in data:
        rows.append({
            "ts": int(c[0]),
            "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]),
        })
    rows.reverse()
    return rows


def get_funding(inst_id):
    try:
        d = okx_get(f"/public/funding-rate?instId={inst_id}")
        if not d:
            return 0.0, None
        fr = float(d[0].get("fundingRate", 0))
        nft = d[0].get("nextFundingTime")
        nft = int(nft) if nft else None
        return fr, nft
    except Exception:
        return 0.0, None


def get_oi_change_1h(ccy):
    try:
        d = okx_get(f"/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1H")
        # newest first: [ts, oi, vol]
        if len(d) < 2:
            return None
        oi_now = float(d[0][1])
        oi_prev = float(d[1][1])
        if oi_prev == 0:
            return None
        return (oi_now - oi_prev) / oi_prev * 100
    except Exception:
        return None


def get_ls_ratio(ccy):
    try:
        d = okx_get(f"/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=5m")
        if not d:
            return None
        return float(d[0][1])  # newest first: [ts, ratio]
    except Exception:
        return None


# ----------------------------------------------------------------------
# Historical reproducibility analysis  (the core edge)
# ----------------------------------------------------------------------
def analyze_history(daily):
    """
    Scan daily candles for past pump events and measure the subsequent dump.
    Returns a dict of stats, or None if not enough history.
    """
    n = len(daily)
    if n < 30:
        return None

    events = []  # each: {pump_pct, retrace_frac, days_to_trough}
    for i in range(1, n - 1):
        prev_close = daily[i - 1]["c"]
        if prev_close <= 0:
            continue
        peak = daily[i]["h"]
        pump_pct = (peak - prev_close) / prev_close * 100
        if pump_pct < PUMP_EVENT_PCT:
            continue

        baseline = prev_close
        # look forward for the trough
        fwd = daily[i + 1: i + 1 + FORWARD_WINDOW]
        if not fwd:
            continue
        trough = min(b["l"] for b in fwd)
        # day offset of the trough
        days_to_trough = 1 + min(
            range(len(fwd)), key=lambda k: fwd[k]["l"]
        )
        denom = peak - baseline
        if denom <= 0:
            continue
        retrace_frac = (peak - trough) / denom  # 1.0 = full retrace to baseline
        retrace_frac = max(0.0, retrace_frac)
        events.append({
            "pump_pct": pump_pct,
            "retrace_frac": retrace_frac,
            "days_to_trough": days_to_trough,
        })

    if not events:
        return None

    dumped = [e for e in events if e["retrace_frac"] >= DUMP_RETRACE_MIN]
    N = len(events)
    dump_rate = len(dumped) / N
    avg_retrace = sum(e["retrace_frac"] for e in dumped) / len(dumped) if dumped else 0.0
    avg_days = sum(e["days_to_trough"] for e in dumped) / len(dumped) if dumped else 0.0
    pump_sizes = sorted(e["pump_pct"] for e in events)
    typ_lo = pump_sizes[len(pump_sizes) // 4]
    typ_hi = pump_sizes[(len(pump_sizes) * 3) // 4]

    # reproducibility tier
    if N >= 15 and dump_rate >= 0.80:
        tier, tier_score = "VERY HIGH", 4
    elif N >= 8 and dump_rate >= 0.70:
        tier, tier_score = "HIGH", 3
    elif N >= 4 and dump_rate >= 0.60:
        tier, tier_score = "MODERATE", 2
    else:
        tier, tier_score = "LOW", 0

    # TP tendency
    if avg_retrace >= 0.85:
        tp_tendency = "near-FULL retrace"
    elif avg_retrace >= 0.5:
        tp_tendency = "HALF-to-full retrace"
    else:
        tp_tendency = "shallow"

    return {
        "N": N, "dump_count": len(dumped), "dump_rate": dump_rate,
        "avg_retrace": avg_retrace, "avg_days": avg_days,
        "typ_lo": typ_lo, "typ_hi": typ_hi,
        "tier": tier, "tier_score": tier_score, "tp_tendency": tp_tendency,
        "hist_days": n,
    }


# ----------------------------------------------------------------------
# 4h trend state + downtrend trigger
# ----------------------------------------------------------------------
def ema(values, period):
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def trend_state_4h(c4h):
    if len(c4h) < 25:
        return None
    closes = [b["c"] for b in c4h]
    e20 = ema(closes, 20)
    price = closes[-1]
    # recent swing low (last 6 bars, excluding current) = downtrend trigger
    recent = c4h[-7:-1]
    swing_low = min(b["l"] for b in recent) if recent else c4h[-1]["l"]
    # lower-high check: is recent high below the prior high?
    above_ema = price > e20
    if above_ema:
        state = "still ABOVE 4h EMA20 (pumping) -> WAIT for breakdown"
    elif price < swing_low:
        state = "BELOW 4h EMA20 & broke swing low -> DOWNTREND (entry zone)"
    else:
        state = "BELOW 4h EMA20 (rolling over) -> watch swing low"
    return {"ema20": e20, "swing_low": swing_low, "above_ema": above_ema, "state": state}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def fmt_price(p):
    if p >= 100:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.3f}"
    if p >= 0.01:
        return f"${p:.4f}"
    return f"${p:.6f}"


def category_of(coin):
    cats = []
    if coin in MEME:
        cats.append("MEME")
    if coin in GAMEFI:
        cats.append("GameFi")
    if coin in AI:
        cats.append("AI")
    return cats


def minutes_until(ms_ts):
    if not ms_ts:
        return None
    now = datetime.now(timezone.utc).timestamp() * 1000
    return int((ms_ts - now) / 60000)


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------
def score_candidate(hist, funding, oi_chg, ls_ratio, cats):
    score = 0
    reasons = []

    if hist:
        score += hist["tier_score"]
        if hist["tier_score"] > 0:
            reasons.append(f"reproducibility {hist['tier']} (+{hist['tier_score']})")

    fr_pct = funding * 100
    if fr_pct > 0.1:
        score += 2; reasons.append("FR very high (+2)")
    elif fr_pct > 0.05:
        score += 1; reasons.append("FR elevated (+1)")

    if oi_chg is not None:
        if oi_chg > 10:
            score += 2; reasons.append("OI surge (+2)")
        elif oi_chg > 5:
            score += 1; reasons.append("OI rising (+1)")

    if ls_ratio is not None:
        if ls_ratio > 2.0:
            score += 2; reasons.append("retail very long (+2)")
        elif ls_ratio > 1.5:
            score += 1; reasons.append("retail long-heavy (+1)")

    if cats:
        score += 1; reasons.append(f"{'/'.join(cats)} (+1)")

    return min(score, 10), reasons


# ----------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------
def send_discord(embeds):
    payload = json.dumps({"embeds": embeds}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pump-screener/2.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()}")


def build_embed(cand):
    coin = cand["coin"]
    inst = cand["inst_id"]
    price = cand["price"]
    score = cand["score"]
    hist = cand["hist"]
    tr = cand["trend"]

    if score >= 7:
        color, tier_icon = 0xFF0000, "STRONG"
    elif score >= 4:
        color, tier_icon = 0xFFAA00, "WATCH"
    else:
        color, tier_icon = 0x888888, "WEAK"

    repro = hist["tier"] if hist else "N/A"
    title = f"{tier_icon}: {coin}USDT  [Score {score}/10 | Repro {repro}]"

    now_jst = datetime.now(JST).strftime("%m-%d %H:%M JST")
    cats = cand["cats"]
    cat_str = "/".join(cats) if cats else "-"

    # --- confluence line ---
    fr_pct = cand["funding"] * 100
    nft_min = minutes_until(cand["next_funding"])
    fr_str = f"{fr_pct:+.4f}%"
    if nft_min is not None:
        fr_str += f" (next in {nft_min}m)"
    oi_str = f"{cand['oi_chg']:+.1f}%/1h" if cand["oi_chg"] is not None else "n/a"
    ls_str = f"{cand['ls_ratio']:.2f}" if cand["ls_ratio"] is not None else "n/a"

    fields = [
        {"name": "Move", "value": f"{fmt_price(price)} | 1h {cand['chg_1h']:+.1f}% | 24h {cand['chg_24h']:+.1f}%",
         "inline": False},
        {"name": "Confluence",
         "value": f"FR {fr_str}\nOI {oi_str} | L/S {ls_str} | Vol ${cand['vol_24h']/1e6:.1f}M | Cat {cat_str}",
         "inline": False},
    ]

    # --- historical pattern ---
    if hist:
        hist_val = (
            f"{hist['N']} past pumps >={int(PUMP_EVENT_PCT)}% -> {hist['dump_count']} dumped "
            f"({hist['dump_rate']*100:.0f}%)\n"
            f"Avg retrace {hist['avg_retrace']*100:.0f}% of pump over ~{hist['avg_days']:.1f}d "
            f"-> {hist['tp_tendency']}\n"
            f"Typical past pump: +{hist['typ_lo']:.0f}~{hist['typ_hi']:.0f}% "
            f"(now +{cand['pump_ref']:.0f}%)"
        )
    else:
        hist_val = "Insufficient history (new listing?) - lower confidence"
    fields.append({"name": f"Historical Pattern ({hist['hist_days'] if hist else 0}d)",
                   "value": hist_val, "inline": False})

    # --- trend / entry plan ---
    baseline = cand["baseline"]
    peak = cand["day_high"]
    half_tp = peak - 0.5 * (peak - baseline)
    full_tp = baseline
    sl = peak * 1.02

    if tr:
        if tr["above_ema"]:
            plan = (f"4h: {tr['state']}\n"
                    f"Entry trigger: 4h close below {fmt_price(tr['swing_low'])}\n"
                    f"SL > {fmt_price(sl)} (day high) | "
                    f"TP1 {fmt_price(half_tp)} (half) · TP2 {fmt_price(full_tp)} (full)")
        else:
            plan = (f"4h: {tr['state']}\n"
                    f"Short on bounce. SL > {fmt_price(sl)} | "
                    f"TP1 {fmt_price(half_tp)} (half) · TP2 {fmt_price(full_tp)} (full)")
    else:
        plan = (f"SL > {fmt_price(sl)} | TP1 {fmt_price(half_tp)} (half) · "
                f"TP2 {fmt_price(full_tp)} (full)")
    fields.append({"name": "Trend / Entry Plan", "value": plan, "inline": False})

    fields.append({
        "name": "Links",
        "value": (f"[CoinGlass](https://www.coinglass.com/ja/currencies/{coin}) · "
                  f"[Orion](https://screener.orionterminal.com/) · "
                  f"[OKX](https://www.okx.com/trade-swap/{inst.lower()})"),
        "inline": False,
    })

    return {"title": title, "color": color, "fields": fields,
            "footer": {"text": f"Pump-Dump Screener | {now_jst}"}}


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    print(f"Scan start {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    tickers = get_all_tickers()
    print(f"Fetched {len(tickers)} USDT-SWAP tickers")

    # ---- Stage 1: cheap pump screen ----
    prelim = []
    for t in tickers:
        coin = t["instId"].replace("-USDT-SWAP", "")
        if coin in EXCLUDE_COINS:
            continue
        try:
            last = float(t["last"])
            open24 = float(t.get("open24h", last))
            vol = float(t.get("volCcy24h", 0)) * last
        except (TypeError, ValueError):
            continue
        if vol < MIN_VOLUME_24H or open24 <= 0:
            continue
        chg_24h = (last - open24) / open24 * 100
        if chg_24h < PUMP_24H:
            # may still qualify on 1h; check 1h cheaply only if 24h close-ish
            if chg_24h < 5:
                continue
        prelim.append({"inst_id": t["instId"], "coin": coin, "price": last,
                       "open24": open24, "vol_24h": vol, "chg_24h": chg_24h})

    print(f"Stage-1 prelim candidates: {len(prelim)}")

    # ---- Stage 2: deep analysis per candidate ----
    candidates = []
    for p in prelim:
        inst = p["inst_id"]
        coin = p["coin"]
        try:
            c1h = get_candles(inst, "1H", 2)
            chg_1h = 0.0
            if len(c1h) >= 2 and c1h[0]["o"] > 0:
                chg_1h = (c1h[-1]["c"] - c1h[0]["o"]) / c1h[0]["o"] * 100
        except Exception:
            chg_1h = 0.0

        # qualify: 1h>=PUMP_1H OR 24h>=PUMP_24H
        if not (abs(chg_1h) >= PUMP_1H or p["chg_24h"] >= PUMP_24H):
            continue

        try:
            daily = get_candles(inst, "1D", HIST_DAYS)
        except Exception as e:
            print(f"  {coin}: candle fetch failed ({e})")
            daily = []

        hist = analyze_history(daily) if daily else None

        try:
            c4h = get_candles(inst, "4H", 50)
            trend = trend_state_4h(c4h)
        except Exception:
            trend = None

        funding, nft = get_funding(inst)
        oi_chg = get_oi_change_1h(coin)
        ls = get_ls_ratio(coin)
        cats = category_of(coin)

        score, reasons = score_candidate(hist, funding, oi_chg, ls, cats)

        baseline = p["open24"]
        day_high = daily[-1]["h"] if daily else p["price"]
        pump_ref = (day_high - baseline) / baseline * 100 if baseline else p["chg_24h"]

        candidates.append({
            "inst_id": inst, "coin": coin, "price": p["price"],
            "chg_1h": chg_1h, "chg_24h": p["chg_24h"], "vol_24h": p["vol_24h"],
            "funding": funding, "next_funding": nft, "oi_chg": oi_chg,
            "ls_ratio": ls, "cats": cats, "hist": hist, "trend": trend,
            "score": score, "reasons": reasons,
            "baseline": baseline, "day_high": day_high, "pump_ref": pump_ref,
        })
        print(f"  {coin}: score {score}/10 | repro {hist['tier'] if hist else 'N/A'} "
              f"| 1h {chg_1h:+.1f}% 24h {p['chg_24h']:+.1f}% | {', '.join(reasons)}")

    if not candidates:
        print("No qualifying pump candidates. All quiet.")
        return

    candidates.sort(key=lambda x: x["score"], reverse=True)

    embeds = []
    for c in candidates[:10]:
        embeds.append(build_embed(c))
        if len(embeds) == 10:
            send_discord(embeds)
            embeds = []
    if embeds:
        send_discord(embeds)

    print(f"Sent {len(candidates)} signal(s) to Discord.")


if __name__ == "__main__":
    main()
