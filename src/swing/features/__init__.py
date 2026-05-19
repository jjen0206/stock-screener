"""Swing 模組 feature helpers — 純函式,輸入 DataFrame,輸出 Series / dict / 標量。

設計原則(對齊 `src/indicators.py` 慣例):
- 不打 API、不查 DB(I/O 由 caller 在 `strategy.py` 處做完餵進來)
- 資料不足回 NaN / None(對 scalar) / 空 DataFrame(對 series),不拋例外
- 不 in-place 改輸入
- 完全獨立寫,不污染 `src/indicators.py`(`docs/swing_implementation_plan.md` § 1.4)
- 僅可 import `src/indicators.py` 純數學 helper(`sma`/`ema`/`rsi`)
"""
