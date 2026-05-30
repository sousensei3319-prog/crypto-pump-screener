import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

BYBIT_BASE = "https://api.bybit.com/v5"
BYBIT_TICKERS_URL = f"{BYBIT_BASE}/market/tickers?category=linear"
BYBIT_KLINES_URL = f"{BYBIT_BASE}/market/kline"

PUMP_THRESHOLD_1H = 10.0
PUMP_THRESHOLD_1H_URGENT = 20.0
MIN_VOLUME_24H = 2_000_000

JST = timezone(timedelta(hours=9))


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pump-screener/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_all_tickers():
    data = fetch_json(BYBIT_TICKERS_URL)
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")
    return data["result"]["list"]


def get_1h_change(symbol):
    url = f"{BYBIT_KLINES_URL}?category=linear&symbol={symbol}&interval=60&limit=2"
    data = fetch_json(url)
    if data.get("retCode") != 0:
        return 0.0
    klines = data["result"]["list"]
    if len(klines) < 2:
        return 0.0
    prev_open = float(klines[1][1])
    current_close = float(klines[0][4])
    if prev_open == 0:
        return 0.0
    return ((current_close - prev_open) / prev_open) * 100


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


def make_embed(symbol, chg_1h, price, vol_24h, funding, level):
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

    coin = symbol.replace("USDT", "")
    action_lines.append(f"[CoinGlass Detail](https://www.coinglass.com/ja/currencies/{coin})")
    action_lines.append(f"[Orion Terminal](https://screener.orionterminal.com/)")

    fields.append({"name": "Action", "value": "\n".join(action_lines), "inline": False})

    return {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": f"Pump Screener | {now_jst}"},
    }


def main():
    print(f"Starting scan at {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")

    tickers = get_all_tickers()
    usdt_tickers = [t for t in tickers if t["symbol"].endswith("USDT")]
    print(f"Scanning {len(usdt_tickers)} USDT pairs (Bybit Linear Perpetuals)...")

    candidates = []
    for t in usdt_tickers:
        try:
            vol_24h = float(t.get("turnover24h", 0))
        except (TypeError, ValueError):
            continue

        if vol_24h < MIN_VOLUME_24H:
            continue

        try:
            price_change_24h = float(t.get("price24hPcnt", 0)) * 100
        except (TypeError, ValueError):
            continue

        if abs(price_change_24h) < 5:
            continue

        symbol = t["symbol"]
        try:
            price = float(t["lastPrice"])
        except (TypeError, ValueError, KeyError):
            continue

        try:
            chg_1h = get_1h_change(symbol)
        except Exception as e:
            print(f"  Skipping {symbol} (kline error: {e})")
            continue

        if abs(chg_1h) < PUMP_THRESHOLD_1H:
            continue

        try:
            funding = float(t.get("fundingRate", 0))
        except (TypeError, ValueError):
            funding = 0.0

        candidates.append({
            "symbol": symbol,
            "price": price,
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
        print(f"  {c['symbol']}: {c['chg_1h']:+.2f}% (1h), FR: {c['funding']*100:.4f}%, Vol: ${c['vol_24h']:,.0f}")

        embed = make_embed(c["symbol"], c["chg_1h"], c["price"], c["vol_24h"], c["funding"], level)
        embeds.append(embed)

        if len(embeds) == 10:
            send_discord(embeds)
            embeds = []

    if embeds:
        send_discord(embeds)

    print(f"Sent {len(candidates)} alert(s) to Discord.")


if __name__ == "__main__":
    main()
