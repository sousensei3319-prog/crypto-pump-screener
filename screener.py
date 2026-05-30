import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

OKX_BASE = "https://www.okx.com/api/v5"
OKX_TICKERS_URL = f"{OKX_BASE}/market/tickers?instType=SWAP"
OKX_CANDLES_URL = f"{OKX_BASE}/market/candles"
OKX_FUNDING_URL = f"{OKX_BASE}/public/funding-rate"

PUMP_THRESHOLD_1H = 10.0
PUMP_THRESHOLD_1H_URGENT = 20.0
MIN_VOLUME_24H = 2_000_000

JST = timezone(timedelta(hours=9))


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pump-screener/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_all_tickers():
    data = fetch_json(OKX_TICKERS_URL)
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data.get('msg')}")
    return [t for t in data["data"] if t["instId"].endswith("-USDT-SWAP")]


def get_1h_change(inst_id):
    url = f"{OKX_CANDLES_URL}?instId={inst_id}&bar=1H&limit=2"
    data = fetch_json(url)
    if data.get("code") != "0":
        return 0.0
    candles = data.get("data", [])
    if len(candles) < 2:
        return 0.0
    prev_open = float(candles[1][1])
    current_close = float(candles[0][4])
    if prev_open == 0:
        return 0.0
    return ((current_close - prev_open) / prev_open) * 100


def get_funding_rate(inst_id):
    url = f"{OKX_FUNDING_URL}?instId={inst_id}"
    data = fetch_json(url)
    if data.get("code") != "0":
        return 0.0
    items = data.get("data", [])
    if not items:
        return 0.0
    return float(items[0].get("fundingRate", 0))


def send_discord(embeds):
    payload = json.dumps({"embeds": embeds}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pump-screener/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()}")


def make_embed(inst_id, chg_1h, price, vol_24h, funding, level):
    symbol = inst_id.replace("-USDT-SWAP", "USDT")
    coin = inst_id.replace("-USDT-SWAP", "")

    if level == "urgent":
        color = 0xFF0000
        title = f"\U0001f534 URGENT: {symbol}"
    else:
        color = 0xFFFF00
        title = f"\U0001f7e1 WATCH: {symbol}"

    fr_pct = funding * 100
    fr_annualized = fr_pct * 3 * 365

    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    fields = [
        {"name": "Price", "value": f"${price:,.4f}" if price < 1 else f"${price:,.2f}", "inline": True},
        {"name": "1h Change", "value": f"{chg_1h:+.2f}%", "inline": True},
        {"name": "24h Volume", "value": f"${vol_24h:,.0f}", "inline": True},
        {"name": "Funding Rate", "value": f"{fr_pct:+.4f}% ({fr_annualized:+.1f}%/yr)", "inline": True},
    ]

    action_lines = []
    if chg_1h >= PUMP_THRESHOLD_1H_URGENT:
        action_lines.append("**Massive pump detected**")
    if fr_pct > 0.05:
        action_lines.append(f"FR overheated ({fr_pct:.4f}%) - short carry profitable")
    if chg_1h >= PUMP_THRESHOLD_1H and fr_pct > 0.03:
        action_lines.append("Pump + elevated FR = potential short opportunity")

    action_lines.append(f"[CoinGlass Detail](https://www.coinglass.com/ja/currencies/{coin})")
    action_lines.append(f"[Orion Terminal](https://screener.orionterminal.com/)")

    fields.append({"name": "Action", "value": "\n".join(action_lines), "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"Pump Screener (OKX) | {now_jst}"},
    }


def main():
    print(f"Starting scan at {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")

    tickers = get_all_tickers()
    print(f"Scanning {len(tickers)} USDT-SWAP pairs (OKX Perpetuals)...")

    candidates = []
    for t in tickers:
        try:
            vol_24h = float(t.get("volCcy24h", 0)) * float(t.get("last", 0))
        except (TypeError, ValueError):
            continue

        if vol_24h < MIN_VOLUME_24H:
            continue

        try:
            last = float(t["last"])
            open_24h = float(t.get("open24h", last))
            if open_24h == 0:
                continue
            chg_24h = ((last - open_24h) / open_24h) * 100
        except (TypeError, ValueError, KeyError):
            continue

        if abs(chg_24h) < 5:
            continue

        inst_id = t["instId"]

        try:
            chg_1h = get_1h_change(inst_id)
        except Exception as e:
            print(f"  Skipping {inst_id} (candle error: {e})")
            continue

        if abs(chg_1h) < PUMP_THRESHOLD_1H:
            continue

        try:
            funding = get_funding_rate(inst_id)
        except Exception:
            funding = 0.0

        candidates.append({
            "inst_id": inst_id,
            "price": last,
            "chg_1h": chg_1h,
            "vol_24h": vol_24h,
            "funding": funding,
        })

    candidates.sort(key=lambda x: abs(x["chg_1h"]), reverse=True)

    if not candidates:
        print("No pumps detected. All quiet.")
        return

    print(f"FOUND {len(candidates)} pump(s)!")

    embeds = []
    for c in candidates[:10]:
        level = "urgent" if abs(c["chg_1h"]) >= PUMP_THRESHOLD_1H_URGENT else "watch"
        print(f"  {c['inst_id']}: {c['chg_1h']:+.2f}% (1h), FR: {c['funding']*100:.4f}%, Vol: ${c['vol_24h']:,.0f}")

        embed = make_embed(c["inst_id"], c["chg_1h"], c["price"], c["vol_24h"], c["funding"], level)
        embeds.append(embed)

        if len(embeds) == 10:
            send_discord(embeds)
            embeds = []

    if embeds:
        send_discord(embeds)

    print(f"Sent {len(candidates)} alert(s) to Discord.")


if __name__ == "__main__":
    main()
