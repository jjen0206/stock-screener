"""
技術指標模組(自刻,不依賴 ta library)。

設計原則:
- 輸入: pd.DataFrame,需含 'close',部分指標另需 'high'/'low'
- 輸出: pd.Series(單軌指標)或 pd.DataFrame(多軌指標)
- 不 in-place 修改輸入
- 資料不足時回 NaN(不拋例外)
- 公式採台股 / 主流券商常見初始化(KD 9-3-3 起始 50、RSI Wilder、EMA adjust=False)

提供:
- sma(df, period)            簡單移動平均
- ema(df, period)            指數移動平均
- kd(df, n=9)                KD 隨機指標(回 K, D 二軌)
- macd(df, 12, 26, 9)        MACD(回 DIF, DEA, HIST 三軌)
- rsi(df, period=14)         RSI(Wilder 平滑)
- bollinger(df, 20, 2)       布林通道(回 mid, upper, lower 三軌)

對拍說明:
- KD 採台股 9-3-3:RSV = (C - L9) / (H9 - L9) × 100
                  K = 2/3·前K + 1/3·RSV;D = 2/3·前D + 1/3·K
                  K、D 起始值 50(首次能算 RSV 的當日,前一期視為 50)。
- RSI Wilder 平滑:首期用 SMA(period),之後 avg = (前avg·(n-1) + 當期) / n。
- MACD HIST 採台股慣例「× 2」放大柱體。
- Bollinger σ 用 ddof=0(母體標準差),對齊 TA-Lib / TradingView。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# === 內部小工具 ===

def _need_close(df: pd.DataFrame) -> pd.Series:
    if "close" not in df.columns:
        raise KeyError("DataFrame 必須有 'close' 欄位")
    return df["close"].astype(float)


def _need_hlc(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    missing = [c for c in ("high", "low", "close") if c not in df.columns]
    if missing:
        raise KeyError(f"缺少欄位: {missing}")
    return (
        df["high"].astype(float),
        df["low"].astype(float),
        df["close"].astype(float),
    )


# === SMA ===

def sma(df: pd.DataFrame, period: int) -> pd.Series:
    """簡單移動平均(Simple Moving Average)。

    公式: SMA_t = mean(close_{t-period+1} ... close_t)
    資料不足 period 筆時回 NaN。

    範例:
        >>> sma(df, 5)   # 5 日均線
    """
    if period <= 0:
        raise ValueError("period 必須 > 0")
    return _need_close(df).rolling(window=period, min_periods=period).mean()


# === EMA ===

def ema(df: pd.DataFrame, period: int) -> pd.Series:
    """指數移動平均(Exponential Moving Average)。

    公式: α = 2 / (period + 1)
          EMA_0 = close_0
          EMA_t = α·close_t + (1 − α)·EMA_{t−1}

    註: 用 pandas .ewm(span, adjust=False),從第 1 筆就有值
        (與大多數券商 App 一致,而非 SMA-seeded EMA)。

    範例:
        >>> ema(df, 12)  # 12 日 EMA
    """
    if period <= 0:
        raise ValueError("period 必須 > 0")
    return _need_close(df).ewm(span=period, adjust=False).mean()


# === KD (台股 9-3-3 傳統算法) ===

def kd(df: pd.DataFrame, n: int = 9) -> pd.DataFrame:
    """KD 隨機指標(台股 9-3-3 傳統算法)。

    公式:
        RSV_t = (close_t − min(low, n)) / (max(high, n) − min(low, n)) × 100
        K_t   = 2/3 · K_{t−1} + 1/3 · RSV_t
        D_t   = 2/3 · D_{t−1} + 1/3 · K_t
        起始: 首次能算 RSV 的當日,K_{prev} = D_{prev} = 50

    特殊情況:
        - 前 (n−1) 日 RSV 與 K、D 皆為 NaN
        - 當 max(high) == min(low)(無波動)時 RSV 視為 0

    回傳:
        DataFrame[K, D],index 與輸入相同。

    範例:
        >>> kd_df = kd(df, n=9)
        >>> kd_df.tail(3)
    """
    if n <= 0:
        raise ValueError("n 必須 > 0")

    high, low, close = _need_hlc(df)
    rolling_high = high.rolling(window=n, min_periods=n).max()
    rolling_low = low.rolling(window=n, min_periods=n).min()
    denom = (rolling_high - rolling_low).to_numpy()
    diff = (close - rolling_low).to_numpy()
    rsv = np.where(denom > 0, diff / np.where(denom > 0, denom, 1) * 100.0, 0.0)
    rsv = np.where(np.isnan(rolling_high.to_numpy()), np.nan, rsv)

    n_rows = len(close)
    k_arr = np.full(n_rows, np.nan)
    d_arr = np.full(n_rows, np.nan)

    prev_k = 50.0
    prev_d = 50.0
    started = False
    for i in range(n_rows):
        rsv_i = rsv[i]
        if np.isnan(rsv_i):
            continue
        if not started:
            prev_k = 50.0
            prev_d = 50.0
            started = True
        curr_k = (2.0 / 3.0) * prev_k + (1.0 / 3.0) * rsv_i
        curr_d = (2.0 / 3.0) * prev_d + (1.0 / 3.0) * curr_k
        k_arr[i] = curr_k
        d_arr[i] = curr_d
        prev_k = curr_k
        prev_d = curr_d

    return pd.DataFrame({"K": k_arr, "D": d_arr}, index=close.index)


# === MACD ===

def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD(Moving Average Convergence Divergence)。

    公式:
        DIF  = EMA(close, fast) − EMA(close, slow)
        DEA  = EMA(DIF, signal)             # 訊號線(亦稱 MACD line)
        HIST = (DIF − DEA) × 2              # 台股慣例放大柱體

    回傳:
        DataFrame[DIF, DEA, HIST]

    範例:
        >>> m = macd(df)            # 12-26-9
        >>> m[["DIF", "DEA", "HIST"]].tail(3)
    """
    if not (fast > 0 and slow > 0 and signal > 0):
        raise ValueError("fast/slow/signal 都必須 > 0")
    if fast >= slow:
        raise ValueError("fast 必須 < slow")
    close = _need_close(df)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2.0
    return pd.DataFrame({"DIF": dif, "DEA": dea, "HIST": hist}, index=close.index)


# === RSI (Wilder 平滑) ===

def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """RSI(Relative Strength Index, Wilder 平滑)。

    公式:
        diff = close.diff()
        gain = max(diff, 0);  loss = max(−diff, 0)
        首期(第 period+1 日):
            avg_gain = mean(gain[1..period])    # 跳過第 0 日的 NaN
            avg_loss = mean(loss[1..period])
        之後:
            avg_gain_t = (avg_gain_{t−1} · (period−1) + gain_t) / period
            avg_loss_t = (avg_loss_{t−1} · (period−1) + loss_t) / period
        RS  = avg_gain / avg_loss
        RSI = 100 − 100 / (1 + RS)

    特殊情況:
        - 資料筆數 ≤ period 全回 NaN
        - avg_loss == 0 且 avg_gain > 0 → RSI = 100
        - avg_loss == 0 且 avg_gain == 0 → RSI = 50(平盤,慣例值)

    範例:
        >>> rsi14 = rsi(df, 14)
    """
    if period <= 0:
        raise ValueError("period 必須 > 0")
    close = _need_close(df)
    n = period
    n_rows = len(close)
    out = np.full(n_rows, np.nan)

    if n_rows <= n:
        return pd.Series(out, index=close.index, name="RSI")

    diff = close.diff().to_numpy()
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    # 第 0 個 diff 是 NaN,np.where 會把它變成 0 — 不影響(因為我們從 index=1 開始抓)
    gain[0] = 0.0
    loss[0] = 0.0

    avg_g = float(np.mean(gain[1 : n + 1]))
    avg_l = float(np.mean(loss[1 : n + 1]))

    def _rsi_value(g: float, loss_val: float) -> float:
        if loss_val == 0.0 and g == 0.0:
            return 50.0
        if loss_val == 0.0:
            return 100.0
        rs = g / loss_val
        return 100.0 - 100.0 / (1.0 + rs)

    out[n] = _rsi_value(avg_g, avg_l)
    for i in range(n + 1, n_rows):
        avg_g = (avg_g * (n - 1) + gain[i]) / n
        avg_l = (avg_l * (n - 1) + loss[i]) / n
        out[i] = _rsi_value(avg_g, avg_l)

    return pd.Series(out, index=close.index, name="RSI")


# === Bollinger Bands ===

def bollinger(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """布林通道(Bollinger Bands)。

    公式:
        mid_t   = SMA(close, period)
        std_t   = 移動標準差(close, period, ddof=0)   # 母體標準差
        upper_t = mid + num_std · std
        lower_t = mid − num_std · std

    註: σ 採 ddof=0(母體標準差),對齊 TA-Lib / TradingView / 大多數券商。
        若主公的看盤 App 用樣本標準差(ddof=1),把這裡改 ddof=1 即可。

    回傳:
        DataFrame[mid, upper, lower]

    範例:
        >>> bb = bollinger(df, 20, 2)
        >>> bb[["lower", "mid", "upper"]].tail(3)
    """
    if period <= 0:
        raise ValueError("period 必須 > 0")
    close = _need_close(df)
    mid = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower}, index=close.index
    )


# === ATR (Average True Range) ===

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR(平均真實波幅,Wilder 平滑)。

    公式:
        TR_t = max(
            high_t − low_t,
            |high_t − close_{t−1}|,
            |low_t  − close_{t−1}|
        )
        首期(第 period 日,index=period):
            ATR = mean(TR[1..period])           # 用 SMA seed,跳過 TR[0] 的 NaN
        之後:
            ATR_t = (ATR_{t−1}·(period−1) + TR_t) / period

    用途:衡量股價日波動度,可推估「合理目標價」與「停損點」。
    例如: stop_loss = close − 1.5·ATR;target = close + 1.5·ATR (約 1 週合理漲幅)

    資料不足(< period+1 筆)→ 全回 NaN。

    範例:
        >>> a = atr(df, 14)
    """
    if period <= 0:
        raise ValueError("period 必須 > 0")
    high, low, close = _need_hlc(df)
    n = period
    n_rows = len(close)
    out = np.full(n_rows, np.nan)
    if n_rows < n + 1:
        return pd.Series(out, index=close.index, name="ATR")

    prev_close = close.shift(1)
    tr_df = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1)
    tr_arr = tr_df.max(axis=1).to_numpy()
    # tr_arr[0] 因為 prev_close 是 NaN 而是 NaN,這裡跳過

    first_atr = float(np.nanmean(tr_arr[1 : n + 1]))
    out[n] = first_atr
    for i in range(n + 1, n_rows):
        out[i] = (out[i - 1] * (n - 1) + tr_arr[i]) / n
    return pd.Series(out, index=close.index, name="ATR")


__all__ = ["sma", "ema", "kd", "macd", "rsi", "bollinger", "atr"]
