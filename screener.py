"""
Crypto Pump-Dump Screener v3 (OKX) - Orion-equivalent metrics + chart attachments.

What it does on every run:
  1. Pull all USDT perps from OKX (geo-OK from GitHub Actions).
  2. Pre-filter by 24h move + volume (Big Movers + High Volume).
  3. For each candidate, deeply analyze:
     - Historical reproducibility (past pumps -> dump rate, retrace depth)
     - Confluence: Funding, OI 1h change, retail L/S, CVD 1h, volatility, BTC correlation, category
     - 4h trend state + concrete entry/SL/TP plan (no falling-knife)
  4. Score 0..10, sort, and POST to Discord with @everyone mention + PNG chart.
"""

import io
import json
import math
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# matplotlib is optional - if missing we just skip chart attachment
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    HAS_MPL = True
except Exception:
    HAS_MPL = False

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MENTION_EVERYONE = os.environ.get("MENTION_EVERYONE", "1") == "1"

OKX = "https://www.okx.com/api/v5"

# ---- Detection thresholds (TEST - LOWERED) ----
PUMP_1H = 3.0
PUMP_24H = 8.0
MIN_VOLUME_24H = 2_000_000
EXCLUDE_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP"}

# ---- Historical analysis params ----
HIST_DAYS = 300
PUMP_EVENT_PCT = 15.0
FORWARD_WINDOW = 3
DUMP_RETRACE_MIN = 0.5

# ---- Category lists ----
MEME = {"DOGE","SHIB","PEPE","WIF","BONK","FLOKI","MEME","BOME","MEW","POPCAT","NEIRO",
        "TURBO","BRETT","MOG","PNUT","GOAT","ACT","HIPPO","DOGS","CAT","BABYDOGE","SPX",
        "GIGGLE","FARTCOIN","CHILLGUY","MOODENG","PONKE","RETARDIO","SLERF","MYRO"}
GAMEFI = {"GALA","AXS","SAND","MANA","IMX","PIXEL","BIGTIME","GMT","MAGIC","PYR","ILV",
          "APE","GODS","NAKA","XAI","ACE","PORTAL","ZBCN"}
AI = {"FET","AGIX","RNDR","RENDER","TAO","WLD","AI","ARKM","NMR","OCEAN","GRT","PHA",
      "AIOZ","NFP","VIRTUAL","AIXBT","ZEREBRO","GRIFFAIN","AI16Z","ARC","SWARMS"}

JST = timezone(timedelta(hours=9))


# ============================================================
# HTTP
# ============================================================
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pump-screener/3.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def okx_get(path):
    d = fetch_json(f"{OKX}{path}")
    if d.get("code") != "0":
        raise RuntimeError(f"OKX {d.get('code')}: {d.get('msg')}")
    return d["data"]


# ============================================================
# Data fetchers
# ============================================================
def get_all_tickers():
    d = okx_get("/market/tickers?instType=SWAP")
    return [t for t in d if t["instId"].endswith("-USDT-SWAP")]


def get_candles(inst_id, bar, limit):
    d = okx_get(f"/market/candles?instId={inst_id}&bar={bar}&limit={limit}")
    rows = []
    for c in d:
        rows.append({
            "ts": int(c[0]),
            "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]),
            "v": float(c[5]) if len(c) > 5 else 0.0,
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
        return fr, (int(nft) if nft else None)
    except Exception:
        return 0.0, None


def get_oi_change_1h(ccy):
    try:
        d = okx_get(f"/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1H")
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
        return float(d[0][1]) if d else None
    except Exception:
        return None


def get_cvd_1h(ccy):
    """OKX taker buy/sell volume -> 1h CVD (USD-denominated taker delta)."""
    try:
        d = okx_get(f"/rubik/stat/taker-volume?ccy={ccy}&instType=SWAP&period=5m")
        # newest first: [ts, sellVol, buyVol]
        if not d:
            return None
        bars = d[:12]  # last 12 * 5m = 1h
        delta = sum(float(b[2]) - float(b[1]) for b in bars)
        return delta
    except Exception:
        return None


# ============================================================
# Reproducibility (the core edge)
# ============================================================
def analyze_history(daily):
    n = len(daily)
    if n < 30:
        return None
    events = []
    for i in range(1, n - 1):
        prev_close = daily[i - 1]["c"]
        if prev_close <= 0:
            continue
        peak = daily[i]["h"]
        pump_pct = (peak - prev_close) / prev_close * 100
        if pump_pct < PUMP_EVENT_PCT:
            continue
        baseline = prev_close
        fwd = daily[i + 1: i + 1 + FORWARD_WINDOW]
        if not fwd:
            continue
        trough = min(b["l"] for b in fwd)
        days_to_trough = 1 + min(range(len(fwd)), key=lambda k: fwd[k]["l"])
        denom = peak - baseline
        if denom <= 0:
            continue
        retrace_frac = max(0.0, (peak - trough) / denom)
        events.append({"pump_pct": pump_pct, "retrace_frac": retrace_frac,
                       "days_to_trough": days_to_trough})

    if not events:
        return None

    dumped = [e for e in events if e["retrace_frac"] >= DUMP_RETRACE_MIN]
    N = len(events)
    dump_rate = len(dumped) / N
    avg_retrace = sum(e["retrace_frac"] for e in dumped) / len(dumped) if dumped else 0.0
    avg_days = sum(e["days_to_trough"] for e in dumped) / len(dumped) if dumped else 0.0
    sizes = sorted(e["pump_pct"] for e in events)
    typ_lo = sizes[len(sizes) // 4]
    typ_hi = sizes[(len(sizes) * 3) // 4]

    if N >= 15 and dump_rate >= 0.80:
        tier, tier_score = "極高", 4
    elif N >= 8 and dump_rate >= 0.70:
        tier, tier_score = "高", 3
    elif N >= 4 and dump_rate >= 0.60:
        tier, tier_score = "中", 2
    else:
        tier, tier_score = "低", 0

    if avg_retrace >= 0.85:
        tp_tendency = "ほぼ全戻し傾向"
    elif avg_retrace >= 0.5:
        tp_tendency = "半値〜全戻し傾向"
    else:
        tp_tendency = "浅めの戻し"

    return {"N": N, "dump_count": len(dumped), "dump_rate": dump_rate,
            "avg_retrace": avg_retrace, "avg_days": avg_days,
            "typ_lo": typ_lo, "typ_hi": typ_hi,
            "tier": tier, "tier_score": tier_score, "tp_tendency": tp_tendency,
            "hist_days": n}


# ============================================================
# 4h trend + downtrend trigger
# ============================================================
def ema(values, period):
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def swing_low_1h(c1h):
    """直近6時間の1h足最安値 = 早期エントリートリガー（速いシグナル）"""
    if len(c1h) < 7:
        return None
    return min(b["l"] for b in c1h[-7:-1])


def trend_state_4h(c4h):
    """4h EMA20と直近24時間の最安値（下落トレンド転換確認用）"""
    if len(c4h) < 25:
        return None
    closes = [b["c"] for b in c4h]
    e20 = ema(closes, 20)
    price = closes[-1]
    recent = c4h[-7:-1]
    swing_low = min(b["l"] for b in recent) if recent else c4h[-1]["l"]
    above_ema = price > e20
    if above_ema:
        state = "4h EMA20の上(まだ上昇中) → ブレイクダウン待ち"
    elif price < swing_low:
        state = "4h EMA20下+直近安値割れ → 下落トレンド確定"
    else:
        state = "4h EMA20の下(失速中) → 直近安値を監視"
    return {"ema20": e20, "swing_low": swing_low, "above_ema": above_ema, "state": state}


def volatility_pct(c1h_24):
    """Average True Range as % over last 24 hourly bars."""
    if len(c1h_24) < 2:
        return None
    trs = []
    for i in range(1, len(c1h_24)):
        h = c1h_24[i]["h"]; l = c1h_24[i]["l"]; pc = c1h_24[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    return atr / c1h_24[-1]["c"] * 100


def correlation_with_btc(asset_closes, btc_closes):
    n = min(len(asset_closes), len(btc_closes))
    if n < 10:
        return None
    a = asset_closes[-n:]; b = btc_closes[-n:]
    # log returns
    def rets(s):
        return [math.log(s[i] / s[i - 1]) for i in range(1, len(s)) if s[i - 1] > 0 and s[i] > 0]
    ra = rets(a); rb = rets(b)
    n = min(len(ra), len(rb))
    if n < 5:
        return None
    ra = ra[-n:]; rb = rb[-n:]
    ma = sum(ra) / n; mb = sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n)) / n
    va = sum((x - ma) ** 2 for x in ra) / n
    vb = sum((x - mb) ** 2 for x in rb) / n
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


# ============================================================
# Helpers
# ============================================================
def fmt_price(p):
    if p >= 100: return f"${p:,.2f}"
    if p >= 1:   return f"${p:,.3f}"
    if p >= 0.01: return f"${p:.4f}"
    return f"${p:.6f}"


def category_of(coin):
    cats = []
    if coin in MEME:   cats.append("MEME")
    if coin in GAMEFI: cats.append("GameFi")
    if coin in AI:     cats.append("AI")
    return cats


def minutes_until(ms_ts):
    if not ms_ts: return None
    now = datetime.now(timezone.utc).timestamp() * 1000
    return int((ms_ts - now) / 60000)


def fmt_money(x):
    if x is None: return "n/a"
    a = abs(x)
    if a >= 1e9: return f"${x/1e9:.2f}B"
    if a >= 1e6: return f"${x/1e6:.2f}M"
    if a >= 1e3: return f"${x/1e3:.2f}K"
    return f"${x:.0f}"


# ============================================================
# Scoring  (Orion's quick-filter buttons rolled into one score)
# ============================================================
def score_candidate(hist, funding, oi_chg, ls_ratio, cvd, vlt, cats):
    score = 0
    reasons = []
    if hist:
        score += hist["tier_score"]
        if hist["tier_score"] > 0:
            reasons.append(f"再現性{hist['tier']}(+{hist['tier_score']})")

    fr_pct = funding * 100
    if fr_pct > 0.1:
        score += 2; reasons.append("FR非常に高い(+2)")
    elif fr_pct > 0.05:
        score += 1; reasons.append("FR高め(+1)")

    if oi_chg is not None:
        if oi_chg > 10:
            score += 2; reasons.append("OI急増(+2)")
        elif oi_chg > 5:
            score += 1; reasons.append("OI上昇中(+1)")

    if ls_ratio is not None:
        if ls_ratio > 2.0:
            score += 2; reasons.append("個人ロング過熱(+2)")
        elif ls_ratio > 1.5:
            score += 1; reasons.append("個人ロング偏重(+1)")

    if cvd is not None and cvd < 0:
        score += 1; reasons.append("売り優勢CVD(+1)")

    if vlt is not None and vlt > 3.0:
        score += 1; reasons.append("高ボラ(+1)")

    if cats:
        score += 1; reasons.append(f"{'/'.join(cats)}(+1)")

    return min(score, 10), reasons


# ============================================================
# Chart rendering (matplotlib)
# ============================================================
def render_chart_png(coin, c1h, c4h, plan_levels):
    if not HAS_MPL or not c1h:
        return None
    try:
        fig, axes = plt.subplots(2, 1, figsize=(8, 6), dpi=110,
                                 gridspec_kw={"height_ratios": [3, 1]})
        ax, axv = axes
        bars = c1h[-72:]  # last 72h
        xs = list(range(len(bars)))
        widths = 0.7
        for i, b in enumerate(bars):
            up = b["c"] >= b["o"]
            color = "#26a69a" if up else "#ef5350"
            # wick
            ax.plot([i, i], [b["l"], b["h"]], color=color, linewidth=0.7)
            # body
            top = max(b["o"], b["c"]); bot = min(b["o"], b["c"])
            height = max(top - bot, (bars[-1]["c"] * 0.0005))
            ax.add_patch(Rectangle((i - widths / 2, bot), widths, height,
                                   facecolor=color, edgecolor=color))
            axv.bar(i, b["v"], width=widths, color=color, alpha=0.7)

        # Plan lines
        sl = plan_levels.get("sl")
        tp1 = plan_levels.get("tp1")
        tp2 = plan_levels.get("tp2")
        early = plan_levels.get("early_entry")
        brk = plan_levels.get("trend_break")
        if sl:    ax.axhline(sl,    color="#ff5252", linestyle="--", linewidth=0.9, label=f"SL {sl:.6g}")
        if early: ax.axhline(early, color="#ffeb3b", linestyle="--", linewidth=0.9, label=f"1h早期 {early:.6g}")
        if tp1:   ax.axhline(tp1,   color="#66bb6a", linestyle="--", linewidth=0.9, label=f"TP1半値 {tp1:.6g}")
        if brk:   ax.axhline(brk,   color="#ff9800", linestyle="--", linewidth=0.9, label=f"4hブレイク {brk:.6g}")
        if tp2:   ax.axhline(tp2,   color="#1e88e5", linestyle="--", linewidth=0.9, label=f"TP2全戻し {tp2:.6g}")

        ax.set_title(f"{coin} - 1h (last 72h)  |  OKX", color="#eeeeee", fontsize=11)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, alpha=0.2)
        axv.grid(True, alpha=0.2)
        for a in (ax, axv):
            a.set_facecolor("#1e222d")
            a.tick_params(colors="#aaaaaa", labelsize=7)
            for spine in a.spines.values():
                spine.set_color("#444444")
        fig.patch.set_facecolor("#131722")
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"  chart render failed: {e}")
        return None


# ============================================================
# Discord
# ============================================================
def discord_post(payload_json, attachments=None):
    """attachments = list of (filename, bytes). If None -> JSON post."""
    boundary = f"----pump{uuid.uuid4().hex}"
    if not attachments:
        data = json.dumps(payload_json).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "pump-screener/3.0"},
        )
    else:
        body = bytearray()
        # payload_json part
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="payload_json"\r\n'
        body += b"Content-Type: application/json\r\n\r\n"
        body += json.dumps(payload_json).encode()
        body += b"\r\n"
        for idx, (fname, content) in enumerate(attachments):
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="files[{idx}]"; filename="{fname}"\r\n'.encode()
            body += b"Content-Type: image/png\r\n\r\n"
            body += content
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": "pump-screener/3.0"},
        )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def build_embed(cand):
    coin = cand["coin"]; inst = cand["inst_id"]
    price = cand["price"]; score = cand["score"]
    hist = cand["hist"]; tr = cand["trend"]

    if score >= 7:   color, tier_icon = 0xFF0000, "🔴 強推奨"
    elif score >= 4: color, tier_icon = 0xFFAA00, "🟡 監視"
    else:            color, tier_icon = 0x888888, "⚪ 弱"

    repro = hist["tier"] if hist else "N/A"
    title = f"{tier_icon}: {coin}USDT  [スコア {score}/10 | 再現性 {repro}]"

    now_jst = datetime.now(JST).strftime("%m-%d %H:%M JST")
    cat_str = "/".join(cand["cats"]) if cand["cats"] else "-"

    fr_pct = cand["funding"] * 100
    nft_min = minutes_until(cand["next_funding"])
    fr_str = f"{fr_pct:+.4f}%"
    if nft_min is not None:
        fr_str += f" (次回 {nft_min}分後)"
    oi_str = f"{cand['oi_chg']:+.1f}%/1h" if cand["oi_chg"] is not None else "n/a"
    ls_str = f"{cand['ls_ratio']:.2f}" if cand["ls_ratio"] is not None else "n/a"
    cvd_str = fmt_money(cand["cvd"]) if cand["cvd"] is not None else "n/a"
    vlt_str = f"{cand['vlt']:.2f}%" if cand["vlt"] is not None else "n/a"
    cor_str = f"{cand['btc_cor']:+.2f}" if cand["btc_cor"] is not None else "n/a"

    fields = [
        {"name": "値動き",
         "value": f"{fmt_price(price)} | 1時間 {cand['chg_1h']:+.1f}% | 24時間 {cand['chg_24h']:+.1f}%",
         "inline": False},
        {"name": "シグナル根拠",
         "value": (f"FR {fr_str}\n"
                   f"OI変化 {oi_str} | L/S比 {ls_str} | 1h売買差 {cvd_str}\n"
                   f"24h出来高 {fmt_money(cand['vol_24h'])} | ボラ {vlt_str} | BTC相関 {cor_str} | カテゴリ {cat_str}"),
         "inline": False},
    ]

    if hist:
        hist_val = (
            f"過去{int(PUMP_EVENT_PCT)}%超の急騰 {hist['N']}回 → {hist['dump_count']}回ダンプ "
            f"({hist['dump_rate']*100:.0f}%)\n"
            f"平均戻し幅 {hist['avg_retrace']*100:.0f}% / 約{hist['avg_days']:.1f}日 "
            f"→ {hist['tp_tendency']}\n"
            f"過去の典型的な急騰幅: +{hist['typ_lo']:.0f}〜{hist['typ_hi']:.0f}% "
            f"(今回 +{cand['pump_ref']:.0f}%)"
        )
    else:
        hist_val = "履歴不足（新規上場?）- 信頼度低め"
    fields.append({"name": f"過去パターン ({hist['hist_days'] if hist else 0}日)",
                   "value": hist_val, "inline": False})

    baseline = cand["baseline"]; peak = cand["day_high"]
    half_tp = peak - 0.5 * (peak - baseline)
    full_tp = baseline
    sl = peak * 1.02
    early_entry = cand.get("swing_low_1h")           # 1h 早期エントリー
    trend_break = tr["swing_low"] if tr else None    # 4h 追加ショート/確認

    lines = []
    if tr:
        lines.append(f"4時間足: {tr['state']}")
    if early_entry:
        lines.append(f"🟡 早期エントリー: 1h終値が {fmt_price(early_entry)} を下抜け")
    if trend_break:
        lines.append(f"🟠 追加ショート: 4h終値が {fmt_price(trend_break)} を下抜け(下落トレンド確認)")
    lines.append(f"🔴 損切 > {fmt_price(sl)} (当日高値+2%)")
    lines.append(f"🟢 利確1 {fmt_price(half_tp)} (半値戻し) ・ 🔵 利確2 {fmt_price(full_tp)} (全戻し)")
    fields.append({"name": "トレンド / エントリープラン", "value": "\n".join(lines), "inline": False})

    fields.append({
        "name": "リンク",
        "value": (f"[CoinGlass](https://www.coinglass.com/ja/currencies/{coin}) ・ "
                  f"[Orion](https://screener.orionterminal.com/) ・ "
                  f"[OKX](https://www.okx.com/trade-swap/{inst.lower()})"),
        "inline": False,
    })

    embed = {"title": title, "color": color, "fields": fields,
             "footer": {"text": f"暴騰ダンプ・スクリーナー v3 | {now_jst}"}}

    plan_levels = {"sl": sl, "tp1": half_tp, "tp2": full_tp,
                   "early_entry": early_entry, "trend_break": trend_break}
    return embed, plan_levels


# ============================================================
# Main
# ============================================================
def main():
    print(f"Scan v3 start {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    tickers = get_all_tickers()
    print(f"Fetched {len(tickers)} USDT-SWAP tickers (matplotlib={HAS_MPL})")

    # BTC daily closes for correlation
    btc_d = []
    try:
        btc_d = [b["c"] for b in get_candles("BTC-USDT-SWAP", "1D", 60)]
    except Exception as e:
        print(f"  BTC reference fetch failed: {e}")

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
        if chg_24h < PUMP_24H and chg_24h < 5:
            continue
        prelim.append({"inst_id": t["instId"], "coin": coin, "price": last,
                       "open24": open24, "vol_24h": vol, "chg_24h": chg_24h})
    print(f"Stage-1 prelim: {len(prelim)}")

    # ---- Stage 2: deep analysis ----
    candidates = []
    for p in prelim:
        inst = p["inst_id"]; coin = p["coin"]
        try:
            c1h = get_candles(inst, "1H", 72)
            chg_1h = 0.0
            if len(c1h) >= 2 and c1h[-2]["o"] > 0:
                chg_1h = (c1h[-1]["c"] - c1h[-2]["o"]) / c1h[-2]["o"] * 100
        except Exception:
            c1h = []; chg_1h = 0.0

        if not (abs(chg_1h) >= PUMP_1H or p["chg_24h"] >= PUMP_24H):
            continue

        try:
            daily = get_candles(inst, "1D", HIST_DAYS)
        except Exception:
            daily = []
        try:
            c4h = get_candles(inst, "4H", 50)
        except Exception:
            c4h = []

        hist = analyze_history(daily) if daily else None
        trend = trend_state_4h(c4h) if c4h else None
        funding, nft = get_funding(inst)
        oi_chg = get_oi_change_1h(coin)
        ls = get_ls_ratio(coin)
        cvd = get_cvd_1h(coin)
        vlt = volatility_pct(c1h[-25:]) if len(c1h) >= 25 else None
        btc_cor = None
        if btc_d and daily:
            asset_closes = [b["c"] for b in daily[-30:]]
            btc_closes = btc_d[-30:]
            btc_cor = correlation_with_btc(asset_closes, btc_closes)
        cats = category_of(coin)

        score, reasons = score_candidate(hist, funding, oi_chg, ls, cvd, vlt, cats)

        baseline = p["open24"]
        day_high = daily[-1]["h"] if daily else max(p["price"], p["open24"])
        pump_ref = (day_high - baseline) / baseline * 100 if baseline else p["chg_24h"]

        sw1h = swing_low_1h(c1h) if c1h else None

        candidates.append({
            "inst_id": inst, "coin": coin, "price": p["price"],
            "chg_1h": chg_1h, "chg_24h": p["chg_24h"], "vol_24h": p["vol_24h"],
            "funding": funding, "next_funding": nft, "oi_chg": oi_chg,
            "ls_ratio": ls, "cvd": cvd, "vlt": vlt, "btc_cor": btc_cor,
            "cats": cats, "hist": hist, "trend": trend, "swing_low_1h": sw1h,
            "score": score, "reasons": reasons,
            "baseline": baseline, "day_high": day_high, "pump_ref": pump_ref,
            "_c1h": c1h, "_c4h": c4h,
        })
        print(f"  {coin}: score {score}/10 repro {hist['tier'] if hist else 'N/A'} "
              f"| 1h {chg_1h:+.1f}% 24h {p['chg_24h']:+.1f}% | {', '.join(reasons)}")

    if not candidates:
        print("No qualifying candidates. All quiet.")
        return

    candidates.sort(key=lambda x: x["score"], reverse=True)

    # ---- Discord: one message per candidate so we can attach a chart per signal ----
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}

    sent = 0
    for c in candidates[:10]:
        embed, plan_levels = build_embed(c)
        png = render_chart_png(c["coin"], c["_c1h"], c["_c4h"], plan_levels)
        if png:
            fname = f"{c['coin']}_chart.png"
            embed["image"] = {"url": f"attachment://{fname}"}
            payload = {"content": mention if sent == 0 else "",
                       "embeds": [embed], "allowed_mentions": allowed}
            discord_post(payload, attachments=[(fname, png)])
        else:
            payload = {"content": mention if sent == 0 else "",
                       "embeds": [embed], "allowed_mentions": allowed}
            discord_post(payload)
        sent += 1

    print(f"Sent {sent} signal(s) to Discord.")


if __name__ == "__main__":
    main()

