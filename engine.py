"""
CryptoSig Engine — 六因子评分 + 入场/止损/止盈计算
纯 requests 数据源: OKX → Yahoo Finance → CoinGecko → Binance 备选
所有请求强制 10 秒超时, 绝不卡死
OKX 全球可用无需代理，作为首选数据源
"""
import time as time_module
import json
import math
import traceback
import requests
from datetime import datetime

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})

TIMEOUT = 10  # 所有请求统一 10 秒超时
CONNECT_TIMEOUT = 4  # 连接握手超时（快速失败避免卡死）

# ============================================================
# 缓存 (历史数据缓存60分钟)
# ============================================================
_ohlc_cache = {}
_CACHE_TTL = 3600

# OKX 符号映射
OKX_SYMBOLS = {
    "BTC-USDT": "BTC-USDT", "ETH-USDT": "ETH-USDT", "BNB-USDT": "BNB-USDT",
    "SOL-USDT": "SOL-USDT", "ZEC-USDT": "ZEC-USDT", "TRX-USDT": "TRX-USDT",
    "DOGE-USDT": "DOGE-USDT", "XRP-USDT": "XRP-USDT",
}
# Yahoo Finance 符号
YF_SYMBOLS = {
    "BTC-USDT": "BTC-USD", "ETH-USDT": "ETH-USD", "BNB-USDT": "BNB-USD",
    "SOL-USDT": "SOL-USD", "ZEC-USDT": "ZEC-USD", "TRX-USDT": "TRX-USD",
    "TAO-USDT": "TAO22941-USD", "DOGE-USDT": "DOGE-USD", "XRP-USDT": "XRP-USD",
}
# CoinGecko ID
CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin", "SOL": "solana",
    "ZEC": "zcash", "TRX": "tron", "TAO": "bittensor", "DOGE": "dogecoin", "XRP": "ripple",
}
# Binance 符号
BN_SYMBOLS = {
    "BTC-USDT": "BTCUSDT", "ETH-USDT": "ETHUSDT", "BNB-USDT": "BNBUSDT",
    "SOL-USDT": "SOLUSDT", "ZEC-USDT": "ZECUSDT", "TRX-USDT": "TRXUSDT",
    "TAO-USDT": "TAOUSDT", "DOGE-USDT": "DOGEUSDT", "XRP-USDT": "XRPUSDT",
}


def http_get(url, timeout=TIMEOUT):
    """强制超时的 GET 请求 (connect, read 分别超时)"""
    if isinstance(timeout, (int, float)):
        timeout = (CONNECT_TIMEOUT, timeout)
    resp = SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 数据源 0: OKX (全球可用，无需代理，首选)
# ============================================================
def fetch_all_okx(coins):
    """OKX API 批量获取价格和K线"""
    market_list = []
    history = {}
    now = time_module.time()

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        okx_sym = OKX_SYMBOLS.get(inst_id, inst_id)

        try:
            # 实时 ticker
            tick = http_get(f"https://www.okx.com/api/v5/market/ticker?instId={okx_sym}", timeout=TIMEOUT)
            if tick.get("code") != "0" or not tick.get("data"):
                raise Exception(f"OKX ticker error: {tick.get('msg', 'no data')}")
            t = tick["data"][0]
            price = float(t["last"])
            open24h = float(t.get("open24h", price))
            ch24h = (price - open24h) / open24h * 100 if open24h > 0 else 0

            # K线 (90天日线)
            kline = http_get(
                f"https://www.okx.com/api/v5/market/history-candles?instId={okx_sym}&bar=1D&limit=90",
                timeout=TIMEOUT,
            )
            if kline.get("code") != "0" or not kline.get("data"):
                raise Exception(f"OKX kline error: {kline.get('msg', 'no data')}")
            candles = kline["data"][::-1]  # OKX返回倒序，翻转让最早在前面
            closes = [round(float(c[4]), 6) for c in candles]
            volumes = [float(c[6]) for c in candles]

            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            _ohlc_cache[sym_name] = {"closes": closes, "volumes": volumes, "ts": now}
            history[sym_name] = {"prices": closes, "volumes": volumes}
            market_list.append({
                "id": sym_name, "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            })
            print(f"  [{sym_name}] OKX ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}% | K线: {len(closes)}条")

        except Exception as e:
            print(f"  [{sym_name}] OKX 失败: {e}")
            history[sym_name] = {"prices": [], "volumes": []}

    if not market_list:
        raise Exception("OKX 全部失败")
    return market_list, history


# ============================================================
# 数据源 1: Yahoo Finance v8 chart API (备选)
# ============================================================
def fetch_yahoo_single(yf_sym):
    """下载单个币种 90 天日线, 返回 (closes, volumes, price, ch24h)"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?range=90d&interval=1d"
    data = http_get(url, timeout=TIMEOUT)
    result = data["chart"]["result"][0]
    meta = result["meta"]
    quotes = result["indicators"]["quote"][0]

    closes = [round(float(v), 6) for v in quotes["close"] if v is not None]
    volumes = [float(v) if v else 0 for v in quotes.get("volume", [])]
    price = meta.get("regularMarketPrice", closes[-1] if closes else 0)
    prev_close = meta.get("chartPreviousClose", closes[-2] if len(closes) >= 2 else price)

    ch24h = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0
    return closes, volumes, price, ch24h


def fetch_all_yahoo(coins):
    """Yahoo Finance v8 API 批量获取"""
    market_list = []
    history = {}
    now = time_module.time()

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        yfs = YF_SYMBOLS.get(inst_id, inst_id.replace("-", "") + "-USD")

        try:
            closes, volumes, price, ch24h = fetch_yahoo_single(yfs)

            if not closes or price <= 0:
                raise Exception("Yahoo 返回空数据/价格为0")

            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            _ohlc_cache[sym_name] = {"closes": closes, "volumes": volumes, "ts": now}
            history[sym_name] = {"prices": closes, "volumes": volumes}
            market_list.append({
                "id": sym_name, "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            })
            print(f"  [{sym_name}] Yahoo ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}% | K线: {len(closes)}条")

        except Exception as e:
            print(f"  [{sym_name}] Yahoo 失败: {e}")
            history[sym_name] = {"prices": [], "volumes": []}

    if not market_list:
        raise Exception("Yahoo Finance 全部失败")
    return market_list, history


# ============================================================
# 数据源 2: CoinGecko (备选)
# ============================================================
def fetch_all_coingecko(coins):
    market_list = []
    history = {}
    now = time_module.time()

    # 批量拉价格
    ids = ",".join(CG_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_7d_change=true"
    price_data = http_get(url, timeout=TIMEOUT)

    for sym_name, info in coins.items():
        cg_id = CG_IDS.get(sym_name)
        pd = price_data.get(cg_id, {})
        price = pd.get("usd", 0)
        if price <= 0:
            history[sym_name] = {"prices": [], "volumes": []}
            continue

        ch24h = pd.get("usd_24h_change", 0) or 0
        ch7d = pd.get("usd_7d_change", 0) or 0

        # 拉取历史K线
        try:
            hist_url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days=90&interval=daily"
            hist_data = http_get(hist_url, timeout=TIMEOUT)
            closes = [round(p[1], 6) for p in hist_data.get("prices", [])]
            volumes = [v[1] for v in hist_data.get("total_volumes", [])]
        except Exception:
            closes = [price]
            volumes = [0]

        _ohlc_cache[sym_name] = {"closes": closes, "volumes": volumes, "ts": now}
        history[sym_name] = {"prices": closes, "volumes": volumes}
        market_list.append({
            "id": sym_name, "symbol": sym_name,
            "current_price": price,
            "price_change_percentage_1h_in_currency": 0,
            "price_change_percentage_24h": ch24h,
            "price_change_percentage_7d_in_currency": ch7d,
        })
        print(f"  [{sym_name}] CoinGecko ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}%")

    if not market_list:
        raise Exception("CoinGecko 全部失败")
    return market_list, history


# ============================================================
# 数据源 3: Binance (备选)
# ============================================================
def fetch_all_binance(coins):
    market_list = []
    history = {}
    now = time_module.time()

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        bn_sym = BN_SYMBOLS.get(inst_id, inst_id)

        try:
            # 价格
            tick = http_get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={bn_sym}", timeout=TIMEOUT)
            price = float(tick["lastPrice"])
            ch24h = float(tick.get("priceChangePercent", 0))

            # K线
            kline = http_get(f"https://api.binance.com/api/v3/klines?symbol={bn_sym}&interval=1d&limit=90", timeout=TIMEOUT)
            closes = [round(float(k[4]), 6) for k in kline]
            volumes = [float(k[5]) for k in kline]

            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            _ohlc_cache[sym_name] = {"closes": closes, "volumes": volumes, "ts": now}
            history[sym_name] = {"prices": closes, "volumes": volumes}
            market_list.append({
                "id": sym_name, "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            })
            print(f"  [{sym_name}] Binance ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}%")

        except Exception as e:
            print(f"  [{sym_name}] Binance 失败: {e}")
            history[sym_name] = {"prices": [], "volumes": []}

    if not market_list:
        raise Exception("Binance 全部失败")
    return market_list, history


# ============================================================
# 数据入口: 多源自动切换 (OKX > Yahoo > CoinGecko > Binance)
# OKX 全球可用无需代理，优先使用；TAO 等不在 OKX 的币种走 CoinGecko
# ============================================================
def fetch_all_data(coins):
    # 分离 OKX 可用的币种和需要 CoinGecko 的币种
    okx_coins = {k: v for k, v in coins.items() if v.get("symbol") in OKX_SYMBOLS}
    cg_only_coins = {k: v for k, v in coins.items() if k not in okx_coins}

    # 策略1: OKX + CoinGecko 补充（最可靠）
    try:
        print(f"  尝试: OKX ({len(okx_coins)}币种)...")
        okx_market, okx_history = fetch_all_okx(okx_coins)
        print(f"  ✓ OKX 成功 ({len(okx_market)}币种)")

        # 补充 CoinGecko 专属币种（如 TAO）
        if cg_only_coins:
            try:
                print(f"  尝试: CoinGecko 补充 ({len(cg_only_coins)}币种)...")
                cg_market, cg_history = fetch_all_coingecko(cg_only_coins)
                okx_market.extend(cg_market)
                okx_history.update(cg_history)
                print(f"  ✓ CoinGecko 补充成功")
            except Exception as e:
                print(f"  ✗ CoinGecko 补充失败: {e}")

        return okx_market, okx_history
    except Exception as e:
        print(f"  ✗ OKX 全部失败: {e}")

    # 策略2: 全量回退到其他数据源
    fallback_sources = [
        ("Yahoo Finance", fetch_all_yahoo),
        ("CoinGecko",     fetch_all_coingecko),
        ("Binance",       fetch_all_binance),
    ]

    for name, func in fallback_sources:
        try:
            print(f"  尝试: {name}...")
            result = func(coins)
            print(f"  ✓ {name} 成功")
            return result
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    raise Exception("所有数据源均失败: OKX / Yahoo / CoinGecko / Binance")


# ============================================================
# 技术指标
# ============================================================
def ema(data, period):
    if len(data) < period:
        return sum(data) / len(data) if data else 0
    k = 2 / (period + 1)
    val = sum(data[:period]) / period
    for x in data[period:]:
        val = x * k + val * (1 - k)
    return val


def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd_histogram(prices):
    if len(prices) < 26:
        return 0, 0, 0
    ema12_v = ema(prices, 12)
    ema26_v = ema(prices, 26)
    macd_line = ema12_v - ema26_v
    signal_line = 0.15 * macd_line
    return macd_line, signal_line, macd_line - signal_line


def atr(prices, period=14):
    if len(prices) < period + 1:
        return 0
    tr_list = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return sum(tr_list[-period:]) / period


def bollinger(prices, period=20):
    if len(prices) < period:
        mid = sum(prices) / len(prices) if prices else 0
        return mid, mid, mid
    recent = prices[-period:]
    mid = sum(recent) / period
    var = sum((x - mid) ** 2 for x in recent) / period
    std = math.sqrt(var)
    return mid + 2 * std, mid, mid - 2 * std


# ============================================================
# 六因子评分引擎 (0-100)
# ============================================================
def calculate_signal(price, prices, volumes, changes):
    if len(prices) < 30:
        return 50.0

    ri = rsi(prices)
    mac, sig, hist = macd_histogram(prices)
    bb_upper, bb_mid, bb_lower = bollinger(prices)
    ema9_v = ema(prices, 9)
    ema21_v = ema(prices, 21)
    ema50_v = ema(prices, 50) if len(prices) >= 50 else ema21_v

    # 因子1: RSI (权重0.22)
    if ri <= 25:
        f1 = 90 + (25 - ri) * 0.4
    elif ri <= 45:
        f1 = 50 + (45 - ri) * 2.0
    elif ri <= 55:
        f1 = 50
    elif ri <= 70:
        f1 = 50 - (ri - 55) * 1.5
    else:
        f1 = max(0, 27.5 - (ri - 70) * 1.5)

    # 因子2: EMA趋势 (权重0.28)
    if price > ema9_v > ema21_v:
        f2 = 100 if ema21_v > ema50_v else 80
    elif price > ema21_v and ema9_v > ema21_v:
        f2 = 72
    elif price > ema21_v and ema9_v < ema21_v:
        f2 = 58
    elif abs(price - ema21_v) / max(ema21_v, 1) < 0.015:
        f2 = 50
    elif price < ema9_v < ema21_v:
        f2 = 0 if ema21_v < ema50_v else 20
    elif price < ema21_v:
        f2 = 28
    else:
        f2 = 50

    # 因子3: MACD (权重0.18)
    if mac > 0:
        f3 = 80 if hist > 0 else 60
    else:
        if hist > 0:
            f3 = 45
        else:
            hist_ratio = abs(hist / mac) if mac != 0 else 1
            f3 = 35 if hist_ratio < 0.3 else 20

    # 因子4: 布林带 (权重0.12)
    if bb_upper > bb_lower:
        bb_pos = (price - bb_lower) / (bb_upper - bb_lower)
        f4 = round((1 - bb_pos) * 100)
    else:
        f4 = 50

    # 因子5: 成交量 (权重0.08)
    f5 = 50
    if len(volumes) >= 21:
        vol_recent = sum(volumes[-7:]) / 7 if sum(volumes[-7:]) > 0 else 0
        vol_prior = sum(volumes[-21:-7]) / 14 if sum(volumes[-21:-7]) > 0 else 0
        if vol_prior > 0:
            vr = vol_recent / vol_prior
            ch24 = changes.get("24h", 0)
            if vr > 1.15 and ch24 > 0:
                f5 = 80
            elif vr > 1.15 and ch24 < 0:
                f5 = 30
            elif vr < 0.65:
                f5 = 45
            else:
                f5 = 50 + (vr - 1) * 30

    # 因子6: 近期动量 (权重0.12)
    ch7d = changes.get("7d", 0)
    ch24 = changes.get("24h", 0)
    if ch7d > 5 and ch24 > 0:
        f6 = 70
    elif ch7d > 2 and ch24 > 0:
        f6 = 60
    elif abs(ch7d) <= 2:
        f6 = 50
    elif ch7d < -2 and ch24 < 0:
        f6 = 30
    elif ch7d < -5 and ch24 < 0:
        f6 = 20
    elif ch7d < -10:
        f6 = 25
    elif ch7d < 0 and ch24 > 0:
        f6 = 45
    elif ch7d > 0 and ch24 < 0:
        f6 = 40
    else:
        f6 = 50

    score = f1 * 0.22 + f2 * 0.28 + f3 * 0.18 + f4 * 0.12 + f5 * 0.08 + f6 * 0.12
    return max(0, min(100, round(score, 1)))


# ============================================================
# 入场/止损/止盈点位
# ============================================================
def calculate_levels(price, prices, atr_val, signal_score, bb_upper, bb_lower, ema21_v):
    if signal_score >= 65:
        entry_zone = (round(price * 0.995, 2), round(price, 2))
        sl_val = max(bb_lower, ema21_v - atr_val * 0.5) if bb_lower > 0 else price - atr_val * 2
        tp1 = round(bb_upper, 2)
        tp2 = round(bb_upper + atr_val * 0.5, 2)
        rr1 = round((tp1 - price) / (price - sl_val), 2) if price > sl_val else 0
        return "long", {
            "direction": "做多 LONG",
            "entry_zone": f"${entry_zone[0]} ~ ${entry_zone[1]}",
            "stop_loss": f"${round(sl_val, 2)}",
            "take_profit_1": f"${tp1}",
            "take_profit_2": f"${tp2}",
            "risk_reward_1": f"1:{rr1}",
        }
    elif signal_score <= 35:
        entry_zone = (round(price, 2), round(price * 1.005, 2))
        sl_val = min(bb_upper, ema21_v + atr_val * 0.5) if bb_upper > 0 else price + atr_val * 2
        tp1 = round(bb_lower, 2)
        tp2 = round(bb_lower - atr_val * 0.5, 2)
        rr1 = round((price - tp1) / (sl_val - price), 2) if sl_val > price else 0
        return "short", {
            "direction": "做空 SHORT",
            "entry_zone": f"${entry_zone[0]} ~ ${entry_zone[1]}",
            "stop_loss": f"${round(sl_val, 2)}",
            "take_profit_1": f"${tp1}",
            "take_profit_2": f"${tp2}",
            "risk_reward_1": f"1:{rr1}",
        }
    else:
        return "wait", {
            "direction": "观望 WAIT",
            "entry_zone": "—",
            "stop_loss": "—",
            "take_profit_1": "—",
            "take_profit_2": "—",
            "risk_reward_1": "—",
        }
