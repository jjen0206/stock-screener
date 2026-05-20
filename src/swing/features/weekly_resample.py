"""把日線 OHLCV 重採樣成週線 OHLCV(對齊台股慣例,週收盤 = 週五)。

輸入欄位對齊 `daily_prices` schema(`src/database.py:130-142`):
    stock_id (TEXT,可缺) / date (TEXT YYYY-MM-DD)
    / open / high / low / close / volume

輸出:DataFrame indexed by 週五日期(pd.Timestamp),欄位 open/high/low/close/volume。

agg 約定:
- open  = 該週第一個交易日 open
- high  = 該週 high 最大
- low   = 該週 low 最小
- close = 該週最後交易日 close
- volume = 該週 volume 總和

邊界:
- 空 DataFrame → 空 DataFrame(同 schema)
- 全 NaN close → 仍 resample,close 留 NaN
- 缺 OHLCV 欄位 → KeyError(明確比 silent fail 好,守 `feedback_warn_dont_hide`)
- 國定假日空週(該週無交易)→ 該週列被 drop(用 volume > 0 或 close not NaN 過濾)
"""
from __future__ import annotations

import pandas as pd


_REQUIRED_COLS = ("date", "open", "high", "low", "close", "volume")
_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def resample_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """把日線 OHLCV resample 成週線(週五收盤對齊)。

    參數:
        daily_df: pd.DataFrame,需含 `date` + OHLCV 欄位。
                  `date` 接受 str 'YYYY-MM-DD' 或 datetime。

    回傳:
        pd.DataFrame,index = pd.Timestamp(週五),欄位 open/high/low/close/volume。
        無交易週(整週 NaN close)會被 drop。

    例外:
        KeyError: 缺必要欄位(`date` 或 OHLCV)。

    範例:
        >>> wk = resample_to_weekly(daily_df)
        >>> wk["close"].iloc[-1]  # 最近一週收盤
    """
    missing = [c for c in _REQUIRED_COLS if c not in daily_df.columns]
    if missing:
        raise KeyError(f"resample_to_weekly 缺必要欄位: {missing}")

    if len(daily_df) == 0:
        return pd.DataFrame(columns=list(_OHLCV_AGG.keys()))

    df = daily_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    # W-FRI:closed='right', label='right' 是預設,週五當「右端 label」,
    # 包含週六到下週五(但週六日無交易,實質 Mon-Fri)
    weekly = df.resample("W-FRI").agg(_OHLCV_AGG)

    # 整週無 close(國定連假整週空)→ drop
    weekly = weekly.dropna(subset=["close"])
    return weekly
