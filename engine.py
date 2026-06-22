"""
CryptoSig Engine — 六因子评分 + 入场/止损/止盈计算
多数据源自动切换: CoinGecko → Coinbase → OKX → Binance
"""
import time as time_module
import json
import math
import requests
from datetime import datetime

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoSignalEngine/2.0"})
REQUEST_TIMEOUT = 8  # 短超时，快速切换数据源

# ============================================================
# 缓存 (历史数据缓存1小时)
# ============================================================
_ohlc_cache = {}
_ohlc_cache_time = {}
_CACHE_TTL = 3600

# 符号映射
COIN_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
}


def _get(url, timeout=None):
    """统一 HTTP GET，短超时"""
    t = timeout or REQUEST_TIMEOUT
    resp = requests.get(url, timeout=t)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 数据源一: CoinGecko (全球)
# ============================================================
def fetch_coingecko_prices():
    ids = "bitcoin,ethereum,binancecoin,solana"
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd"
           f"&include_24hr_change=true&include_24hr_vol=true")
    return _get(url)


def fetch_coingecko_history(coin_id):
    now = time_module.time()
    if coin_id in _ohlc_cache and now - _ohlc_cache_time.get(coin_id, 0) < _CACHE_TTL:
        return _ohlc_cache[coin_id]
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=90"
    data = _get(url, timeout=15)
    result = {
        "prices": [p[1] for p in data.get("prices", [])],
        "volumes": [v[1] for v in data.get("total_volumes", [])],
    }
    _ohlc_cache[coin_id] = result
    _ohlc_cache_time[coin_id] = now
    return result


# ============================================================
# 符号映射 (Coinbase / Binance)
# ============================================================
CB_SYMBOLS = {
    "BTC-USDT": "BTC-USD",
    "ETH-USDT": "ETH-USD",
    "SOL-USDT": "SOL-USD",
    # BNB 不在 Coinbase 上
}


def fetch_coinbase_all(coins):
    """Coinbase: 只返回支持的币种 (BTC/ETH/SOL), 不支持的返回 None"""
    market_list = []
    history = {}
    missing = []

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        cb_sym = CB_SYMBOLS.get(inst_id)
        if not cb_sym:
            missing.append(sym_name)
            continue

        try:
            # 当前价
            spot = _get(f"https://api.coinbase.com/v2/prices/{cb_sym}/spot")
            price = float(spot["data"]["amount"])

            # 日K线 (最多90天)
            candles = _get(
                f"https://api.exchange.coinbase.com/products/{cb_sym}/candles"
                f"?granularity=86400",
                timeout=12,
            )
            # 每条: [time, low, high, open, close, volume]
            candles_sorted = sorted(candles, key=lambda x: x[0])
            closes = [float(c[4]) for c in candles_sorted]
            volumes = [float(c[5]) for c in candles_sorted]

            ch24h = 0
            if len(closes) >= 2 and closes[-2] > 0:
                ch24h = (price - closes[-2]) / closes[-2] * 100

            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            history[sym_name] = {"prices": closes, "volumes": volumes}
            market_list.append({
                "id": sym_name, "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            })
            print(f"  [{sym_name}] Coinbase ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}%")

        except Exception as e:
            print(f"  [{sym_name}] Coinbase 失败: {e}")
            raise

    return market_list, history


# ============================================================
# 数据源三: OKX
# ============================================================
def fetch_okx_all(coins):
    market_list = []
    history = {}

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        try:
            tick = _get(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}")
            if tick.get("code") != "0" or not tick.get("data"):
                raise Exception(f"OKX ticker 异常: {tick}")
            t = tick["data"][0]
            price = float(t["last"])
            open24h = float(t.get("open24h", 0))
            ch24h = (price - open24h) / open24h * 100 if open24h > 0 else 0

            kdata = _get(
                f"https://www.okx.com/api/v5/market/history-candles"
                f"?instId={inst_id}&bar=1D&limit=90",
                timeout=12,
            )
            if kdata.get("code") != "0" or not kdata.get("data"):
                raise Exception(f"OKX kline 异常")
            klines = kdata["data"]
            closes = [float(k[4]) for k in klines[::-1]]
            volumes = [float(k[6]) for k in klines[::-1]]

            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            history[sym_name] = {"prices": closes, "volumes": volumes}
            market_list.append({
                "id": sym_name, "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            })
            print(f"  [{sym_name}] OKX ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}%")
        except Exception as e:
            print(f"  [{sym_name}] OKX 失败: {e}")
            raise
    return market_list, history


# ============================================================
# 数据源四: Binance
# ============================================================
BINANCE_MIRRORS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.us",
]
SYMBOL_MAP = {
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "BNB-USDT": "BNBUSDT",
    "SOL-USDT": "SOLUSDT",
}


def _binance_get(endpoint):
    for mirror in BINANCE_MIRRORS:
        try:
            url = f"{mirror}{endpoint}"
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            continue
    raise Exception("所有 Binance 镜像不可用")


def fetch_binance_all(coins):
    market_list = []
    history = {}

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        bin_sym = SYMBOL_MAP.get(inst_id, inst_id.replace("-", ""))
        try:
            tick = _binance_get(f"/api/v3/ticker/24hr?symbol={bin_sym}")
            price = float(tick["lastPrice"])
            open24h = float(tick["openPrice"])
            ch24h = (price - open24h) / open24h * 100 if open24h > 0 else 0

            kdata = _binance_get(f"/api/v3/klines?symbol={bin_sym}&interval=1d&limit=90")
            closes = [float(k[4]) for k in kdata]
            volumes = [float(k[5]) for k in kdata]

            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

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
            raise
    return market_list, history


# ============================================================
# 数据入口: 按币种混合 (BTC/ETH/SOL→Coinbase, BNB→OKX/Binance)
# ============================================================
def _try_single_coin(sym_name, info):
    """尝试所有数据源获取单个币种, 返回 (market, history) 或异常"""
    inst_id = info["symbol"]

    # 1) Coinbase (美国原生, 最可靠) - BTC/ETH/SOL
    cb_sym = CB_SYMBOLS.get(inst_id)
    if cb_sym:
        try:
            spot = _get(f"https://api.coinbase.com/v2/prices/{cb_sym}/spot")
            price = float(spot["data"]["amount"])
            candles = _get(
                f"https://api.exchange.coinbase.com/products/{cb_sym}/candles?granularity=86400",
                timeout=12,
            )
            candles_sorted = sorted(candles, key=lambda x: x[0])
            closes = [float(c[4]) for c in candles_sorted]
            volumes = [float(c[5]) for c in candles_sorted]

            ch24h = 0
            if len(closes) >= 2 and closes[-2] > 0:
                ch24h = (price - closes[-2]) / closes[-2] * 100
            ch7d = 0
            if len(closes) >= 8 and closes[-8] > 0:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            return {
                "id": sym_name, "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            }, {"prices": closes, "volumes": volumes}, "Coinbase"
        except Exception as e:
            print(f"  [{sym_name}] Coinbase 失败: {e}")

    # 2) CoinGecko
    try:
        coin_id = COIN_ID_MAP[sym_name]
        pd = _get(
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        )
        pd = pd.get(coin_id, {})
        price = pd.get("usd", 0)
        if price <= 0:
            raise Exception("价格异常")
        ch24h = pd.get("usd_24h_change", 0) or 0

        hist = fetch_coingecko_history(coin_id)
        closes = hist["prices"]
        volumes = hist["volumes"]

        ch7d = 0
        if len(closes) >= 8 and closes[-8] > 0:
            ch7d = (price - closes[-8]) / closes[-8] * 100

        return {
            "id": sym_name, "symbol": sym_name,
            "current_price": price,
            "price_change_percentage_1h_in_currency": 0,
            "price_change_percentage_24h": ch24h,
            "price_change_percentage_7d_in_currency": ch7d,
        }, {"prices": closes, "volumes": volumes}, "CoinGecko"
    except Exception as e:
        print(f"  [{sym_name}] CoinGecko 失败: {e}")

    # 3) OKX
    try:
        tick = _get(f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}")
        if tick.get("code") != "0" or not tick.get("data"):
            raise Exception("OKX ticker 异常")
        t = tick["data"][0]
        price = float(t["last"])
        open24h = float(t.get("open24h", 0))
        ch24h = (price - open24h) / open24h * 100 if open24h > 0 else 0

        kdata = _get(
            f"https://www.okx.com/api/v5/market/history-candles"
            f"?instId={inst_id}&bar=1D&limit=90",
            timeout=12,
        )
        if kdata.get("code") != "0" or not kdata.get("data"):
            raise Exception("OKX kline 异常")
        klines = kdata["data"]
        closes = [float(k[4]) for k in klines[::-1]]
        volumes = [float(k[6]) for k in klines[::-1]]

        ch7d = 0
        if len(closes) >= 8 and closes[-8] > 0:
            ch7d = (price - closes[-8]) / closes[-8] * 100

        return {
            "id": sym_name, "symbol": sym_name,
            "current_price": price,
            "price_change_percentage_1h_in_currency": 0,
            "price_change_percentage_24h": ch24h,
            "price_change_percentage_7d_in_currency": ch7d,
        }, {"prices": closes, "volumes": volumes}, "OKX"
    except Exception as e:
        print(f"  [{sym_name}] OKX 失败: {e}")

    # 4) Binance (最后备选)
    bin_sym = SYMBOL_MAP.get(inst_id, inst_id.replace("-", ""))
    tick = _binance_get(f"/api/v3/ticker/24hr?symbol={bin_sym}")
    price = float(tick["lastPrice"])
    open24h = float(tick["openPrice"])
    ch24h = (price - open24h) / open24h * 100 if open24h > 0 else 0
    kdata = _binance_get(f"/api/v3/klines?symbol={bin_sym}&interval=1d&limit=90")
    closes = [float(k[4]) for k in kdata]
    volumes = [float(k[5]) for k in kdata]
    ch7d = 0
    if len(closes) >= 8 and closes[-8] > 0:
        ch7d = (price - closes[-8]) / closes[-8] * 100

    return {
        "id": sym_name, "symbol": sym_name,
        "current_price": price,
        "price_change_percentage_1h_in_currency": 0,
        "price_change_percentage_24h": ch24h,
        "price_change_percentage_7d_in_currency": ch7d,
    }, {"prices": closes, "volumes": volumes}, "Binance"


def fetch_all_data(coins):
    """按币种维度混合数据源"""
    market_list = []
    history = {}

    for sym_name, info in coins.items():
        try:
            mkt, hist, src = _try_single_coin(sym_name, info)
            market_list.append(mkt)
            history[sym_name] = hist
            print(f"  [{sym_name}] {src} ${mkt['current_price']:,.{info['decimals']}f} | 24h {mkt['price_change_percentage_24h']:+.2f}%")
        except Exception as e:
            print(f"  [{sym_name}] 所有数据源均失败: {e}")
            history[sym_name] = {"prices": [], "volumes": []}

    if not market_list:
        raise Exception("所有币种在所有数据源均获取失败")

    return market_list, history


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
            "direction": "\u505a\u591a LONG",
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
            "direction": "\u505a\u7a7a SHORT",
            "entry_zone": f"${entry_zone[0]} ~ ${entry_zone[1]}",
            "stop_loss": f"${round(sl_val, 2)}",
            "take_profit_1": f"${tp1}",
            "take_profit_2": f"${tp2}",
            "risk_reward_1": f"1:{rr1}",
        }
    else:
        return "wait", {
            "direction": "\u89c2\u671b WAIT",
            "entry_zone": "\u2014",
            "stop_loss": "\u2014",
            "take_profit_1": "\u2014",
            "take_profit_2": "\u2014",
            "risk_reward_1": "\u2014",
        }
