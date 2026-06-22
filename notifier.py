"""
多渠道通知系统：Server酱 / Telegram / 钉钉
"""
import json
import time
import requests
from datetime import datetime


class Notifier:
    def __init__(self, config):
        self.cfg = config
        self.n = config.get("notifications", {})
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "CryptoSignalBot/2.0"
        })

    def _build_message(self, sym, score, info, results):
        """构建信号消息文本"""
        lev = results["levels"]
        ri = results["rsi"]
        hist = results["macd_hist"]
        changes = results["changes"]
        price = results["price"]
        ema9 = results.get("ema9", "--")
        ema21 = results.get("ema21", "--")
        atr_val = results.get("atr", "--")

        if score >= 75:
            emoji = "🟢"
            tag = "极度看多"
        else:
            emoji = "🔴"
            tag = "极度看空"

        return f"""{emoji} **{sym} {tag}信号** | 评分 {score}/100

💵 当前价格: ${price:,.{info['decimals']}f}
📊 信号评分: {score}/100 ({tag})

=== 操作建议 ===
📌 方向: {lev['direction']}
🎯 入场区间: {lev['entry_zone']}
🛑 止损位: {lev['stop_loss']}
✅ 止盈一(保守): {lev['take_profit_1']}
✅ 止盈二(激进): {lev['take_profit_2']}
📐 盈亏比: {lev['risk_reward_1']}

=== 技术指标 ===
📈 RSI(14): {ri:.1f}
📉 MACD柱: {hist:.4f}
📊 EMA9/21: ${ema9:,.2f} / ${ema21:,.2f}
📏 ATR(14): ${atr_val:,.2f}
🕐 24H涨跌: {changes.get('24h', 0):+.2f}%
📅 7D涨跌: {changes.get('7d', 0):+.2f}%

⏰ 信号时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

    # ==================== Server酱 ====================
    def send_serverchan(self, title, content):
        sc = self.n.get("serverchan", {})
        if not sc.get("enabled") or not sc.get("sendkey"):
            return None
        try:
            resp = self.session.post(
                f"https://sctapi.ftqq.com/{sc['sendkey']}.send",
                data={"title": title, "desp": content},
                timeout=10,
            )
            return resp.json()
        except Exception as e:
            print(f"  [Server酱] 推送失败: {e}")
            return None

    # ==================== Telegram ====================
    def send_telegram(self, text):
        tg = self.n.get("telegram", {})
        if not tg.get("enabled") or not tg.get("bot_token") or not tg.get("chat_id"):
            return None
        try:
            url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
            resp = self.session.post(url, json={
                "chat_id": tg["chat_id"],
                "text": text,
                "parse_mode": "Markdown",
            }, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"  [Telegram] 推送失败: {e}")
            return None

    # ==================== 钉钉 ====================
    def send_dingtalk(self, title, text):
        dt = self.n.get("dingtalk", {})
        if not dt.get("enabled") or not dt.get("webhook_url"):
            return None
        try:
            # 钉钉 Markdown 消息格式
            md_text = f"## {title}\n\n{text.replace('**', '**')}"
            resp = self.session.post(dt["webhook_url"], json={
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": md_text.replace("\n", "  \n"),
                },
            }, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"  [钉钉] 推送失败: {e}")
            return None

    # ==================== 统一推送入口 ====================
    def broadcast(self, sym, score, info, results):
        """广播到所有已启用的渠道"""
        title = f"{'🟢' if score >= 75 else '🔴'} {sym} {'做多' if score >= 75 else '做空'}信号 | 评分 {score}/100"
        text = self._build_message(sym, score, info, results)

        channels = []
        if self.send_serverchan(title, text):
            channels.append("Server酱")
        if self.send_telegram(text):
            channels.append("Telegram")
        if self.send_dingtalk(title, text):
            channels.append("钉钉")

        return channels if channels else None
