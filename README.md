# CryptoSignal — 加密货币智能信号系统

7×24 实时监控 BTC/ETH/BNB/SOL，六因子多维度评分（0-100），精准做多/做空信号 + 入场点位 + 止盈止损。

## 架构

```
OKX API → 行情引擎 → 六因子评分 → 信号判定 → 多渠道推送
                    ↓
              Flask REST API → 前端仪表盘
```

## 评分系统

| 因子 | 权重 | 说明 |
|------|------|------|
| RSI 超买超卖 | 18% | 30 以下超卖做多，70 以上超买做空 |
| EMA 趋势排列 | 22% | EMA9/21/50 多头/空头排列 |
| MACD 金叉死叉 | 18% | MACD 柱状线方向 + 动能 |
| 布林带位置 | 14% | 下轨超卖/上轨超买 |
| 成交量趋势 | 10% | 7日 vs 前14日均量比 |
| 近期动量 | 18% | 7日 + 24h 涨跌幅综合 |

### 信号阈值（保守策略）

- **≥75**：强力做多 → 多渠道推送
- **61-74**：偏向做多 → 仅仪表盘显示
- **41-60**：观望
- **26-40**：偏向做空 → 仅仪表盘显示
- **≤25**：强力做空 → 多渠道推送

## 通知渠道

- Server酱 → 微信
- Telegram Bot → 群组
- 钉钉机器人 → 群组

## 部署

### 后端（Render.com 免费计划）

1. Fork 本仓库
2. 在 Render.com 创建 Web Service
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python app.py`
5. 配置环境变量或修改 config.json

### 前端仪表盘

部署到 CloudStudio / Vercel / Netlify 等静态托管平台，修改 `templates/index.html` 中的 `BACKEND_URL` 指向你的 Render 地址。

## 配置

复制 `config.example.json` 为 `config.json` 并填入你的渠道凭证。

## 免责声明

本系统仅供研究参考，不构成投资建议。加密货币市场风险极高，请谨慎决策。
