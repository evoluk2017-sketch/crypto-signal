"""
多渠道通知系统：Server酱 / Telegram / 钉钉
敏感信息通过环境变量传入，不硬编码在配置文件中
"""
import json
import os
import time
import hmac
import hashlib
import base64
import urllib.parse
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

    def _get(self, section, key, env_name=None):
        """优先从环境变量读取敏感信息，fallback 到 config"""
        if env_name and os.environ.get(env_name):
            return os.environ[env_name]
        return self.n.get(section, {}).get(key, "")

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

{'─' * 30}
📢 **一起来 Aster 交易！**
⏰ 限时福利！注册 Aster 领空投 + 9% 手续费返现
🔗 链接直达：https://www.asterdex.com/en/referral/8A7f17

📢 **一起来币安 Binance 交易！**
✅ 新用户注册，享手续费减免+返佣
🔗 币安合作入口（国内直连）：https://www.bsmkweb.cc/register?ref=BRZL88
🔒 最终跳转币安官方注册，安全有保障，邀请码自动绑定 BRZL88
"""

    # ==================== Server酱 ====================
    def send_serverchan(self, title, content):
        sendkey = self._get("serverchan", "sendkey", "SERVERCHAN_SENDKEY")
        if not sendkey:
            return None
        try:
            resp = self.session.post(
                f"https://sctapi.ftqq.com/{sendkey}.send",
                data={"title": title, "desp": content},
                timeout=10,
            )
            return resp.json()
        except Exception as e:
            print(f"  [Server酱] 推送失败: {e}")
            return None

    # ==================== Telegram ====================
    def send_telegram(self, text):
        bot_token = self._get("telegram", "bot_token", "TG_BOT_TOKEN")
        chat_id = self._get("telegram", "chat_id", "TG_CHAT_ID")
        if not bot_token or not chat_id:
            return None
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            resp = self.session.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"  [Telegram] 推送失败: {e}")
            return None

    # ==================== 钉钉 ====================
    def send_dingtalk(self, title, text):
        webhook_url = self._get("dingtalk", "webhook_url", "DINGTALK_WEBHOOK")
        secret = self._get("dingtalk", "secret", "DINGTALK_SECRET")
        if not webhook_url:
            return None
        try:
            # 钉钉 Markdown 消息格式
            md_text = f"## {title}\n\n{text.replace('**', '**')}"

            # 钉钉加签验证
            if secret:
                # 计算签名
                timestamp = str(round(time.time() * 1000))
                secret_enc = secret.encode('utf-8')
                string_to_sign = f'{timestamp}\n{secret}'
                string_to_sign_enc = string_to_sign.encode('utf-8')
                hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

                # 添加时间戳和签名到 URL
                webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

            resp = self.session.post(webhook_url, json={
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": md_text.replace("\n", "  \n"),
                },
            }, timeout=10)
            result = resp.json()

            # 检查是否成功
            if result.get("errcode") == 0:
                return result
            else:
                print(f"  [钉钉] 推送失败: {result}")
                return None
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
