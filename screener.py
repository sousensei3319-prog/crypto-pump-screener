import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

BINANCE_FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_FUTURES_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_FUTURES_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest"

PUMP_THRESHOLD_1H = 10.0
PUMP_THRESHOLD_1H_URGENT = 20.0
MIN_VOLUME_24H = 2_000_000
MIN_FUNDING_RATE = 0.0005

JST = timezone(timedelta(hours=9))


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pump-screener/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_all_tickers():
    return fetch_json(BINANCE_FUTURES_TICKER_URL)


def get_1h_change(symbol):
    url = f"{BINANCE_FUTURES_KLINES_URL}?symbol={symbol}&interval=1h&limit=2"
    klines = fetch_json(url)
    if len(klines) < 2:
        return 0.0
    prev_open = float(klines[0][1])
    current_close = float(klines[1][4])
    if prev_open == 0:
        return 0.0
    return ((current_close - prev_open) / prev_open) * 100


def get_funding_rate(symbol):
    url = f"{BINANCE_FUTURES_PREMIUM_URL}?symbol={symbol}"
    data = fetch_json(url)
    return float(data.get("lastFundingRate", 0))


def get_open_interest(symbol):
    url = f"{BINANCE_FUTURES_OI_URL}?symbol={symbol}"
    data = fetch_json(url)
    return float(data.get("openInterest", 0))


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
        title = f"🔴 URGENT: {symbol}"
    else:
        color = 0xFFFF00
        title = f"🟡 WATCH: {symbol}"

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

    action_lines.append(f"[CoinGlass Detail](https://www.coinglass.com/ja/currencies/{symbol.replace('USDT', '')})")
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
    print(f"Scanning {len(usdt_tickers)} USDT pairs...")

    candidates = []
    for t in usdt_tickers:
        vol_24h = float(t["quoteVolume"])
        if vol_24h < MIN_VOLUME_24H:
            continue

        price_change_24h = float(t["priceChangePercent"])
        if abs(price_change_24h) < 5:
            continue

        symbol = t["symbol"]
        price = float(t["lastPrice"])

        try:
            chg_1h = get_1h_change(symbol)
        except Exception as e:
            print(f"  Skipping {symbol} (kline error: {e})")
            continue

        if abs(chg_1h) < PUMP_THRESHOLD_1H:
            continue

        try:
            funding = get_funding_rate(symbol)
        except Exception:
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

    print(f"\n{'='*60}")
    print(f"FOUND {len(candidates)} pump(s)!")
    print(f"{'='*60}")

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

    print(f"\nSent {len(candidates)} alert(s) to Discord.")


if __name__ == "__main__":
    main()
