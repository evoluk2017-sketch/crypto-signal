"""
CryptoSignal Cloud — Flask 主应用
7x24 后台引擎 + Web API + 仪表盘
"""
import os
import sys
import json
import time
import re
import threading
import feedparser
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, request

# 将当前目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import fetch_all_data, calculate_signal, calculate_levels
from engine import rsi, macd_histogram, atr, bollinger, ema
from notifier import Notifier

app = Flask(__name__)

# ============================================================
# CORS — 允许跨域访问（前端仪表盘在不同域名）
# ============================================================
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

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
    "market_indicators": {},  # {fear_greed, btc_dominance, bull_bear, market_trend}
    "daily_events": {},       # {date, events: [{title, summary, link}], updated_at}
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
# 市场情绪指标获取 (本地引擎曾负责，云端引擎自给自足)
# ============================================================
def fetch_market_indicators():
    """获取三大市场情绪指标：恐慌贪婪、牛熊多空、BTC占比/趋势"""
    indicators = {}
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    # 1. 恐慌贪婪指数
    try:
        data = s.get("https://api.alternative.me/fng/?limit=7", timeout=8).json()
        items = data.get("data", [])
        current = items[0] if items else None
        if current:
            indicators["fear_greed"] = {
                "current": {"value": int(current["value"]), "classification": current["value_classification"], "timestamp": current["timestamp"]},
                "history": [{"value": int(i["value"]), "classification": i["value_classification"], "timestamp": i["timestamp"]} for i in items],
            }
            print(f"  [指标] 恐慌贪婪: {current['value']} ({current['value_classification']})")
    except Exception as e:
        print(f"  [指标] 恐慌贪婪失败: {e}")

    # 2. BTC市值占比 + 24h趋势
    try:
        data = s.get("https://api.coingecko.com/api/v3/global", timeout=8).json()["data"]
        btc_dom = data["market_cap_percentage"]["btc"]
        mcap_change = data.get("market_cap_change_percentage_24h_usd", 0)
        total_mcap = data["total_market_cap"]["usd"]
        indicators["btc_dominance"] = {"value": round(btc_dom, 1), "total_mcap_t": round(total_mcap / 1e12, 2)}
        indicators["market_trend"] = {"change_24h": round(mcap_change, 2)}
        print(f"  [指标] BTC占比: {btc_dom:.1f}% | 24h市值: {mcap_change:+.2f}%")
    except Exception as e:
        print(f"  [指标] CoinGecko Global失败: {e}")

    # 3. 牛熊多空比 (Binance futures)
    try:
        d = s.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1", timeout=8).json()
        if d:
            long_pct = float(d[0]["longAccount"]) * 100
            short_pct = float(d[0]["shortAccount"]) * 100
            indicators["bull_bear"] = {"long": round(long_pct, 1), "short": round(short_pct, 1), "ratio": round(float(d[0]["longShortRatio"]), 2)}
            print(f"  [指标] 牛熊多空: 多{long_pct:.1f}% / 空{short_pct:.1f}%")
    except Exception as e:
        print(f"  [指标] Binance多空比失败: {e}")

    return indicators


# ============================================================
# 每日币圈大事件 (RSS抓取 + 翻译 + 存储)
# ============================================================
RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("CryptoNews", "https://cryptonews.com/news/feed/"),
]

def translate_to_chinese(text):
    """Google Translate 免费 API 英译中"""
    if not text:
        return text
    try:
        import urllib.parse
        encoded = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q={encoded}"
        resp = requests.get(url, timeout=8)
        data = resp.json()
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0]) if data and data[0] else text
        return translated.strip() or text
    except Exception:
        return text

def fetch_daily_events():
    """从 RSS 源抓取今天币圈大事件，翻译后返回 top 8 条"""
    events = []
    seen_titles = set()
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    for source_name, url in RSS_FEEDS:
        try:
            resp = s.get(url, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                if not title or len(title) < 10:
                    continue
                title_key = title[:30].lower()
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
                summary = ""
                raw = entry.get("summary") or entry.get("description") or ""
                summary = re.sub(r"<[^>]+>", "", raw).strip()
                if len(summary) > 120:
                    summary = summary[:120] + "..."
                events.append({
                    "title": title, "title_cn": "", "summary": summary,
                    "link": entry.get("link", ""), "source": source_name,
                })
            print(f"  [大事件] {source_name}: {len(feed.entries)}条")
        except Exception as e:
            print(f"  [大事件] {source_name} 失败: {e}")

    selected = events[:8]
    if selected:
        print(f"  [翻译] {len(selected)}条标题翻译中...")
        for e in selected:
            e["title_cn"] = translate_to_chinese(e["title"])
    return selected


# ============================================================
# 数据拉取总超时包装（防止请求卡死冻结引擎）
# ============================================================
def _fetch_with_total_timeout(coins, total_timeout=120):
    """在子线程中拉取数据，设置总超时。超时则强制放弃，保证引擎不死"""
    result = [None]
    error = [None]

    def _run():
        try:
            result[0] = fetch_all_data(coins)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=total_timeout)

    if t.is_alive():
        raise Exception(f"fetch_all_data 总超时({total_timeout}s)，强制放弃（子线程残留，引擎继续下一轮）")
    if error[0]:
        raise error[0]
    return result[0]


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

    _sync_fresh_seconds = POLL_INTERVAL * 3  # 3个周期内有本地推送则跳过自行拉取
    _last_daily_events_date = ""  # 追踪今日是否已推送大事件

    while True:
        try:
            # 每个周期从文件重新加载状态（多 worker / 重启后恢复同步数据）
            try:
                with open(STATE_FILE, "r") as f:
                    file_state = json.load(f)
                with state_lock:
                    if file_state.get("engine_status") == "synced" and file_state.get("results"):
                        state["results"] = file_state["results"]
                        state["last_update"] = file_state["last_update"]
                        state["engine_status"] = "synced"
                        state["market_indicators"] = file_state.get("market_indicators", {})
                        if file_state.get("history"):
                            state["history"] = file_state["history"]
                        if file_state.get("daily_events"):
                            state["daily_events"] = file_state["daily_events"]
            except FileNotFoundError:
                pass
            except Exception:
                pass

            # 如果有本地推送的数据且较新，跳过自行拉取（避免覆盖）
            with state_lock:
                if state["engine_status"] == "synced" and state["results"] and state["last_update"]:
                    try:
                        last_dt = datetime.fromisoformat(state["last_update"])
                        elapsed = (datetime.now() - last_dt).total_seconds()
                        if elapsed < _sync_fresh_seconds:
                            time.sleep(POLL_INTERVAL)
                            continue
                    except Exception:
                        pass
                state["engine_status"] = "fetching"

            market, history = _fetch_with_total_timeout(COINS)

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
                # 只有拉到数据才覆盖，否则保留本地引擎推送的数据
                if results:
                    state["results"] = results
                    state["last_update"] = now.isoformat()
                    state["engine_status"] = "running"
                elif state["engine_status"] == "synced":
                    # 本地已推送数据，保持不动
                    pass
                else:
                    state["engine_status"] = "running"

            # ========== 市场情绪指标 (每轮更新) ==========
            indicators = fetch_market_indicators()
            if indicators:
                with state_lock:
                    state["market_indicators"] = indicators

            # ========== 每日大事件 (08:00 自动抓取) ==========
            today_str = now.strftime("%Y-%m-%d")
            if _last_daily_events_date != today_str and now.hour == 8 and now.minute < 10:
                print(f"\n  📰 开始抓取今日币圈大事件...")
                events = fetch_daily_events()
                if events:
                    _last_daily_events_date = today_str
                    with state_lock:
                        state["daily_events"] = {
                            "date": today_str,
                            "events": events,
                            "updated_at": now.isoformat(),
                        }
                    print(f"  📰 今日大事件已更新: {len(events)}条")
                else:
                    print(f"  📰 今日未抓取到大事件")

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
            "market_indicators": state.get("market_indicators", {}),
            "daily_events": state.get("daily_events", {}),
            "config": {
                "coins": list(COINS.keys()),
                "poll_interval": POLL_INTERVAL,
                "thresholds": {"long": THRESHOLD_LONG, "short": THRESHOLD_SHORT},
                "alert_cooldown_minutes": CONFIG["alert_cooldown_minutes"],
                "channels": {
                    "serverchan": CONFIG.get("notifications", {}).get("serverchan", {}).get("enabled", False),
                    "telegram": CONFIG.get("notifications", {}).get("telegram", {}).get("enabled", False),
                    "dingtalk": CONFIG.get("notifications", {}).get("dingtalk", {}).get("enabled", False),
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


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """本地引擎数据同步接口"""
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400

    with state_lock:
        state["results"] = data.get("results", {})
        state["last_update"] = data.get("last_update", datetime.now().isoformat())
        state["engine_status"] = "synced"

        # 市场情绪指标
        indicators = data.get("market_indicators")
        if indicators:
            state["market_indicators"] = indicators

        # 每日大事件
        daily_events = data.get("daily_events")
        if daily_events and daily_events.get("events"):
            state["daily_events"] = daily_events

        # 合并历史记录
        new_history = data.get("history", [])
        for record in new_history:
            exists = any(
                h.get("time") == record.get("time") and h.get("symbol") == record.get("symbol")
                for h in state["history"]
            )
            if not exists:
                state["history"].insert(0, record)
        if len(state["history"]) > 50:
            state["history"] = state["history"][:50]

    print(f"[{datetime.now():%H:%M:%S}] 收到本地引擎同步: {len(data.get('results', {}))}个币种")
    save_state()  # 持久化到文件，防止多 worker 不一致
    return jsonify({"ok": True, "updated": len(data.get("results", {}))})


@app.route("/api/daily_events", methods=["POST"])
def api_daily_events():
    """接收每日币圈大事件（由定时任务推送）"""
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400
    with state_lock:
        state["daily_events"] = {
            "date": data.get("date", datetime.now().strftime("%Y-%m-%d")),
            "events": data.get("events", []),
            "updated_at": datetime.now().isoformat(),
        }
    save_state()  # 持久化，防止重启丢失
    print(f"[{datetime.now():%H:%M:%S}] 收到每日大事件: {len(data.get('events', []))}条 ({data.get('date', '?')})")
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ============================================================
# 后台引擎自动启动 (gunicorn 和 python app.py 均适用)
# ============================================================
_engine_started = False


def _start_engine():
    global _engine_started
    if _engine_started:
        return
    _engine_started = True
    load_state()
    engine_thread = threading.Thread(target=engine_loop, daemon=True)
    engine_thread.start()
    port = int(os.environ.get("PORT", 5000))
    print(f"[{datetime.now():%H:%M:%S}] 后台引擎线程已启动 → 监听端口 {port}")


# gunicorn 加载时触发（若是直接 python app.py 则在下方触发）
_start_engine()


# ============================================================
# 启动（直接运行）
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[{datetime.now():%H:%M:%S}] Web 服务启动: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
