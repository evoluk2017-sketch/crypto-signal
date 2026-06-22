"""
CryptoSig Engine — 六因子评分 + 入场/止损/止盈计算
OKX API 数据源（云端原生，无需翻墙）
"""
import time
import json
import math
import requests
from datetime import datetime

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoSignalEngine/2.0"})


# ============================================================
# 数据获取 (OKX)
# ============================================================
def fetch_json(url, max_retries=2, timeout=15):
    for attempt in range(max_retries):
        try:
            resp = SESSION.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise


def fetch_okx_price(inst_id):
    url = f"https://www.okx.com/api/v5/market/ticker?instId={inst_id}"
    data = fetch_json(url, timeout=10)
    if data.get("code") == "0" and data.get("data"):
        t = data["data"][0]
        return {
            "price": float(t["last"]),
            "ch24h": float(t.get("open24h", 0)),
            "open24h": float(t.get("open24h", 0)),
            "vol24h": float(t.get("vol24h", 0)) if t.get("vol24h") else 0,
        }
    raise Exception(f"OKX ticker error: {data}")


def fetch_okx_kline(inst_id, bar="1D", limit=90):
    url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst_id}&bar={bar}&limit={limit}"
    data = fetch_json(url, timeout=10)
    if data.get("code") == "0" and data.get("data"):
        klines = data["data"]
        closes = [float(k[4]) for k in klines[::-1]]
        volumes = [float(k[6]) for k in klines[::-1]]
        return closes, volumes
    raise Exception(f"OKX kline error: {data}")


def fetch_all_data(coins):
    market_list = []
    history = {}

    for sym_name, info in coins.items():
        inst_id = info["symbol"]
        try:
            tick = fetch_okx_price(inst_id)
            price = tick["price"]
            closes, volumes = fetch_okx_kline(inst_id)

            ch24h = 0
            if tick["open24h"] > 0:
                ch24h = (price - tick["open24h"]) / tick["open24h"] * 100

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
            print(f"  [{sym_name}] OKX ${price:,.{info['decimals']}f} | 24h {ch24h:+.2f}%")

        except Exception as e:
            print(f"  [{sym_name}] 数据获取失败: {type(e).__name__}: {e}")
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
