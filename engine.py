"""
CryptoSig Engine — 六因子评分 + 入场/止损/止盈计算
Binance API 数据源（全球可用，Render 美国服务器友好）
"""
import time
import json
import math
import requests
from datetime import datetime

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoSignalEngine/2.0"})
# Binance 多镜像自动切换
BINANCE_MIRRORS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]


# ============================================================
# 数据获取 (Binance)
# ============================================================
def _binance_get(endpoint, timeout=12):
    """带镜像切换的 Binance 请求"""
    for mirror in BINANCE_MIRRORS:
        try:
            url = f"{mirror}{endpoint}"
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "CryptoSignalEngine/2.0"})
            resp.raise_for_status()
            return resp.json()
        except Exception:
            continue
    raise Exception("所有 Binance 镜像不可用")


# Binance 符号映射：内部名 -> Binance 交易对
SYMBOL_MAP = {
    "BTC-USDT": "BTCUSDT",
    "ETH-USDT": "ETHUSDT",
    "BNB-USDT": "BNBUSDT",
    "SOL-USDT": "SOLUSDT",
}


def fetch_binance_price(bin_symbol):
    """获取当前价格和24h变化"""
    data = _binance_get(f"/api/v3/ticker/24hr?symbol={bin_symbol}", timeout=10)
    price = float(data["lastPrice"])
    open24h = float(data["openPrice"])
    ch24h = (price - open24h) / open24h * 100 if open24h > 0 else 0
    return {
        "price": price,
        "open24h": open24h,
        "ch24h": ch24h,
        "vol24h": float(data.get("volume", 0)),
    }


def fetch_binance_klines(bin_symbol, limit=90):
    """获取日K线数据"""
    data = _binance_get(f"/api/v3/klines?symbol={bin_symbol}&interval=1d&limit={limit}", timeout=10)
    closes = [float(k[4]) for k in data]  # 收盘价
    volumes = [float(k[5]) for k in data]  # 成交量
    return closes, volumes


def fetch_all_data(coins):
    market_list = []
    history = {}

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        bin_sym = SYMBOL_MAP.get(inst_id, inst_id.replace("-", ""))
        try:
            tick = fetch_binance_price(bin_sym)
            price = tick["price"]
            ch24h = tick["ch24h"]
            closes, volumes = fetch_binance_klines(bin_sym)

            ch7d = 0
            if len(closes) >= 8:
                ch7d = (price - closes[-8]) / closes[-8] * 100

            history[sym_name] = {
                "prices": closes,
                "volumes": volumes,
            }

            market_list.append({
                "id": sym_name,
                "symbol": sym_name,
                "current_price": price,
                "price_change_percentage_1h_in_currency": 0,
                "price_change_percentage_24h": ch24h,
                "price_change_percentage_7d_in_currency": ch7d,
            })
            print(f"  [{sym_name}] Binance ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}%")

        except Exception as e:
            import traceback
            print(f"  [{sym_name}] 数据获取失败: {type(e).__name__}: {e}")
            traceback.print_exc()
            history[sym_name] = {"prices": [], "volumes": []}

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
    ema12 = ema(prices, 12)
    ema26 = ema(prices, 26)
    macd_line = ema12 - ema26
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
