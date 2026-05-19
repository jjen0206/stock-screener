"""Swing(波段)模組 — 林恩如風格 + 基本面 toggle。

完全隔離既有 17 套策略 / `screener_short` / `screener_long` / `paper_trades` 表。
主公拍板「不要跟其他共用」(`docs/swing_implementation_plan.md` § 1.4 / § 8)。

Phase A 內容:
- `features/weekly_resample`:daily → weekly OHLCV
- `features/ma_signals`:20wMA / 20DMA 上穿 / 斜率 / 乖離率
- `features/volume_signal`:週爆量 + OBV + 量價分類
- `features/pattern`:W 底 / M 頭簡化型態識別
- `features/trendline`:swing point + linear regression + 趨勢三分類
- `features/fundamentals`:ROE TTM / 毛利率 / EPS streak / 殖利率穩定度(可選 toggle)

Phase B+ 才寫的:`strategy.py` / `backtest.py` / `paper_trading.py` / `notifier_swing.py` / `ui_cards.py`。
"""
