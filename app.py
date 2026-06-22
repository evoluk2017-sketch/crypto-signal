"""
CryptoSignal Cloud — Flask 主应用
7x24 后台引擎 + Web API + 仪表盘
"""
import os
import sys
import json
import time
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template, request

# 将当前目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import fetch_all_data, calculate_signal, calculate_levels
from engine import rsi, macd_histogram, atr, bollinger, ema
from notifier import Notifier

app = Flask(__name__)

# ============================================================
# 加载配置
# ============================================================
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

COINS = CONFIG["coins"]
ALERT_COOLDOWN = CONFIG["alert_cooldown_minutes"] * 60
POLL_INTERVAL = CONFIG["poll_interval_seconds"]
THRESHOLD_LONG = CONFIG["alert_threshold"]["long"]
THRESHOLD_SHORT = CONFIG["alert_threshold"]["short"]

notifier = Notifier(CONFIG)

# ============================================================
# 全局状态（内存 + 文件持久化）
# ============================================================
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
state = {
    "results": {},       # {BTC: {...}, ETH: {...}, ...}
    "alerts": {},        # {BTC: "2026-06-22T17:00:00", ...}
    "history": [],       # 最近50条信号历史 [{time, symbol, score, direction}, ...]
    "last_update": None,
    "engine_status": "starting",
}
state_lock = threading.Lock()


def save_state():
    with state_lock:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception:
            pass


def load_state():
    global state
    try:
        with open(STATE_FILE, "r") as f:
            loaded = json.load(f)
            with state_lock:
                state.update(loaded)
    except FileNotFoundError:
        pass


# ============================================================
# 后台引擎线程
# ============================================================
def engine_loop():
    global state
    print(f"[{datetime.now():%H:%M:%S}] CryptoSignal Cloud 引擎启动")
    print(f"  监控: {', '.join(COINS.keys())}")
    print(f"  推送阈值: ≤{THRESHOLD_SHORT} / ≥{THRESHOLD_LONG}")
    print(f"  冷却: {CONFIG['alert_cooldown_minutes']}分钟 | 刷新: {POLL_INTERVAL}秒")
    print("-" * 55)

    while True:
        try:
            with state_lock:
                state["engine_status"] = "fetching"

            market, history = fetch_all_data(COINS)

            with state_lock:
                state["engine_status"] = "scoring"

            results = {}
            now = datetime.now()

            for sym, info in COINS.items():
                coin = next((c for c in market if c["id"] == sym), None)
                if not coin:
                    continue

                price = coin["current_price"]
                hdata = history.get(sym, {})
                prices = hdata.get("prices", [])
                volumes = hdata.get("volumes", [])

                changes = {
                    "1h": coin.get("price_change_percentage_1h_in_currency", 0) or 0,
                    "24h": coin.get("price_change_percentage_24h", 0) or 0,
                    "7d": coin.get("price_change_percentage_7d_in_currency", 0) or 0,
                }

                if len(prices) < 30:
                    continue

                score = calculate_signal(price, prices, volumes, changes)
                ri = rsi(prices)
                mac, sig, hist = macd_histogram(prices)
                atr_val = atr(prices)
                bb_upper, bb_mid, bb_lower = bollinger(prices)
                ema9_v = ema(prices, 9)
                ema21_v = ema(prices, 21)

                direction, levels = calculate_levels(
                    price, prices, atr_val, score, bb_upper, bb_lower, ema21_v
                )

                r = {
                    "price": round(price, info["decimals"]),
                    "score": score,
                    "direction": direction,
                    "levels": levels,
                    "rsi": round(ri, 1),
                    "macd_hist": round(hist, 4),
                    "ema9": round(ema9_v, info["decimals"]),
                    "ema21": round(ema21_v, info["decimals"]),
                    "atr": round(atr_val, 2),
                    "bb_upper": round(bb_upper, info["decimals"]),
                    "bb_mid": round(bb_mid, info["decimals"]),
                    "bb_lower": round(bb_lower, info["decimals"]),
                    "changes": changes,
                }
                results[sym] = r

                # 判断是否触发推送
                with state_lock:
                    last_alert_str = state["alerts"].get(sym)
                    should_alert = False
                    if last_alert_str:
                        try:
                            last_alert = datetime.fromisoformat(last_alert_str)
                            elapsed = (now - last_alert).total_seconds()
                            if elapsed >= ALERT_COOLDOWN:
                                should_alert = True
                        except Exception:
                            should_alert = True
                    else:
                        should_alert = True

                    if should_alert and (score >= THRESHOLD_LONG or score <= THRESHOLD_SHORT):
                        channels = notifier.broadcast(sym, score, info, r)
                        state["alerts"][sym] = now.isoformat()
                        record = {
                            "time": now.isoformat(),
                            "symbol": sym,
                            "score": score,
                            "direction": "做多" if score >= THRESHOLD_LONG else "做空",
                            "price": r["price"],
                            "channels": channels or [],
                        }
                        state["history"].insert(0, record)
                        if len(state["history"]) > 50:
                            state["history"] = state["history"][:50]
                        ch_str = ",".join(channels or ["无"])
                        print(f"  [{sym}] 📤 信号推送 → {ch_str} | 评分 {score}/100 | {'做多' if score >= THRESHOLD_LONG else '做空'}")

            with state_lock:
                state["results"] = results
                state["last_update"] = now.isoformat()
                state["engine_status"] = "running"

            # 打印摘要
            print(f"\n[{now:%H:%M:%S}] 刷新完成")
            for sym, r in results.items():
                d_emoji = "🟢" if r["direction"] == "long" else ("🔴" if r["direction"] == "short" else "⚪")
                print(f"  {d_emoji} {sym}: ${r['price']:,} | 评分 {r['score']}/100 | RSI {r['rsi']}")
            print()

            save_state()

        except Exception as e:
            import traceback
            traceback.print_exc()
            with state_lock:
                state["engine_status"] = f"error: {type(e).__name__}"
            print(f"[{datetime.now():%H:%M:%S}] 引擎错误: {e}，30秒后重试")
            time.sleep(30)
            continue

        time.sleep(POLL_INTERVAL)


# ============================================================
# Web API 路由
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify({
            "status": state["engine_status"],
            "last_update": state["last_update"],
            "results": state["results"],
            "history": state["history"],
            "config": {
                "coins": list(COINS.keys()),
                "poll_interval": POLL_INTERVAL,
                "thresholds": {"long": THRESHOLD_LONG, "short": THRESHOLD_SHORT},
                "alert_cooldown_minutes": CONFIG["alert_cooldown_minutes"],
                "channels": {
                    "serverchan": CONFIG["notifications"]["serverchan"]["enabled"],
                    "telegram": CONFIG["notifications"]["telegram"]["enabled"],
                    "dingtalk": CONFIG["notifications"]["dingtalk"]["enabled"],
                }
            }
        })


@app.route("/api/signal/<symbol>")
def api_signal_detail(symbol):
    sym = symbol.upper()
    with state_lock:
        r = state["results"].get(sym)
        if not r:
            return jsonify({"error": f"Unknown symbol: {sym}"}), 404

        # 评分因子分解（用于调试）
        detail = {}
        hdata_key = sym
        return jsonify({
            "symbol": sym,
            **r,
        })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    load_state()

    # 启动后台引擎
    engine_thread = threading.Thread(target=engine_loop, daemon=True)
    engine_thread.start()

    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    print(f"[{datetime.now():%H:%M:%S}] Web 服务启动: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
