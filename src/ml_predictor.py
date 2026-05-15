"""短線勝率預測模型(RandomForestClassifier)。

特徵抽取 + label 構造 + 訓練 + 推論。模型 pkl 存 models/short_pick.pkl,
streamlit / 推播 reuse `predict_short_pick_winrate` 顯示「🎯 AI 勝率 N%」。

Label 定義:**進場後 5 個交易日內,high 是否 ≥ 進場 close + 1.5 × ATR(14)**
(觸到目標即算 win,實務貼近 stop-limit 賣單;比純看 5 日後 close 更貼近真實
短線交易行為)

特徵(v3 = 16 個):

v2 base(11 個):
- kd_k / kd_d              KD9 K 值 / D 值
- macd_dif / macd_osc      MACD 12-26-9 DIF / OSC
- ma_alignment             MA5/MA20/MA60 排列 score(2=多頭/0=空頭/1=糾結)
- bb_position              close 在 BB 通道中的位置(0=下軌,1=上軌)
- vol_ratio                今日量 / 5 日均量
- bias_pct                 close vs MA20 乖離率 %
- atr_normalized           ATR(14) / close × 100(波動率)
- inst_5d / inst_10d       法人 5/10 日累計(張),沒資料 → 0

v3 高階特徵(5 個,2026-05-14 加,缺資料一律 fallback 0.0,不會 drop row):
- holders_delta_w_zscore   千張戶週變化 z-score(per-sid 滾動 4 週)
                           對齊 big_holder_inflow Phase 2 邏輯,讓 ML 學「相對突破」
- inst_5d_zscore           法人 5 日累計 z-score(per-sid 滾動 20 天 5d-sum 分布)
- regime_dummy             大盤 regime ordinal:bull=2 / weak_bull=1 / sideways=-1
                           / bear=-2 / unknown=0(RandomForest 不需 one-hot)
- holders_pct_change_4w    千張戶占比 vs 4 週前的相對變化率
- is_theme_member          是否在 data/themes/*.yaml union 內(0/1)

新舊模型相容性:**新特徵一律 append 到 FEATURE_NAMES 尾部**,搭配
`_aligned_feature_names(model)` slicing shim — 舊 v2 模型(n_features_in_=11)
仍能用前 11 欄推論,新模型用全部 16 欄。Phase 1 commit 後即使 Phase 2 重訓
失敗 rollback,production 不會壞。

任何資料不足(< 45 天歷史)→ extract_features 回 None。v3 features 任一
SQL/算術失敗 → 該 feature fallback 0.0(不 drop row)。

訓練 universe:給 build_training_dataset 傳 stock_ids 控制(預設 TW_TOP_50);
不跑全市場避免訓練時間爆量。
"""
from __future__ import annotations

import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from src import database as db, indicators as ind

logger = logging.getLogger(__name__)


MODEL_VERSION = "v3"

# v2 base(11) + v3 高階特徵(5,append 在尾部供 backward-compat slicing)
FEATURE_NAMES = [
    # === v2 base(舊 model n_features_in_=11 仍能用) ===
    "kd_k", "kd_d", "macd_dif", "macd_osc", "ma_alignment",
    "bb_position", "vol_ratio", "bias_pct", "atr_normalized",
    "inst_5d", "inst_10d",
    # === v3 新增(每個都有 try/except → 0.0 fallback) ===
    "holders_delta_w_zscore",
    "inst_5d_zscore",
    "regime_dummy",
    "holders_pct_change_4w",
    "is_theme_member",
]

# regime → ordinal 映射(RandomForest split 對單調 ordinal 友善,不需 one-hot)
_REGIME_ORDINAL: dict[str, float] = {
    "bull": 2.0,
    "weak_bull": 1.0,
    "sideways": -1.0,
    "bear": -2.0,
    "unknown": 0.0,
}

# 模組內快取(target_date × db_path 一次 query 即可,避免 predict_batch 對
# 2400 sids 各打一次 TAIEX 60-row SQL)
_REGIME_CACHE: dict[tuple[str, str], float] = {}
_THEME_MEMBER_SIDS: frozenset[str] | None = None


def _aligned_feature_names(model) -> list[str]:
    """根據 model.n_features_in_ slice FEATURE_NAMES 前 N 欄 — 讓舊 v2 pkl(11 feat)
    在新 code(16 feat FEATURE_NAMES)下仍能 predict。

    新特徵一律 append 在尾部,所以 FEATURE_NAMES[:11] 完全等同 v2 model 訓練時的
    feature 順序,直接打到 model.predict_proba 不會有 column 對不齊問題。
    """
    n = getattr(model, "n_features_in_", None)
    if n is not None and n < len(FEATURE_NAMES):
        return FEATURE_NAMES[:n]
    return FEATURE_NAMES


def _load_theme_member_sids() -> frozenset[str]:
    """讀 data/themes/*.yaml union 出來的 sids,first call 後 cache。

    YAML 缺檔 / parse 失敗 → 回空 set(is_theme_member 全部 fallback 0.0)。
    """
    global _THEME_MEMBER_SIDS
    if _THEME_MEMBER_SIDS is not None:
        return _THEME_MEMBER_SIDS
    try:
        import yaml
        from src import config as _config

        themes_dir = Path(_config.PROJECT_ROOT) / "data" / "themes"
        sids: set[str] = set()
        for p in sorted(themes_dir.glob("*.yaml")):
            with open(p, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            for s in (data.get("sids") or []):
                s = str(s).strip()
                if s:
                    sids.add(s)
        _THEME_MEMBER_SIDS = frozenset(sids)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ML] _load_theme_member_sids 失敗:%s", e)
        _THEME_MEMBER_SIDS = frozenset()
    return _THEME_MEMBER_SIDS


def _load_holders_weeks(
    stock_id: str,
    target_date: str,
    weeks: int = 5,
    db_path: str | Path | None = None,
) -> list[dict]:
    """撈該 sid 在 target_date 之前最近 N 週 shareholder_concentration。

    回 list[dict],asc 排序(index 0 為最舊,-1 為最新)。SQL 失敗或表不存在
    → 回空 list,caller 各自走 fallback。
    """
    import sqlite3

    try:
        with db.get_conn(db_path) as conn:
            rows = conn.execute(
                "SELECT week_end, holders_delta_w, holders_pct "
                "FROM shareholder_concentration "
                "WHERE sid=? AND week_end<=? "
                "ORDER BY week_end DESC LIMIT ?",
                (stock_id, target_date, weeks),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return list(reversed([dict(r) for r in rows]))


def _compute_holders_delta_w_zscore(rows: list[dict]) -> float:
    """latest holders_delta_w 對前 4 週 distribution 的 z-score(per-sid 滾動)。

    對齊 big_holder_inflow Phase 2:前 rolling_weeks=4 週算 μ + σ(ddof=1),
    回 (latest - μ) / σ。前期 ≥ 4 非 NULL → 正常算,否則 0.0;σ ≤ 0 → 0.0。
    """
    if len(rows) < 5:  # 前 4 + 本週
        return 0.0
    latest = rows[-1].get("holders_delta_w")
    if latest is None:
        return 0.0
    prior = [
        r.get("holders_delta_w") for r in rows[:-1]
        if r.get("holders_delta_w") is not None
    ]
    if len(prior) < 4:
        return 0.0
    s = pd.Series(prior, dtype=float)
    mu = float(s.mean())
    sigma = float(s.std(ddof=1))
    if pd.isna(sigma) or sigma <= 0:
        return 0.0
    return (float(latest) - mu) / sigma


def _compute_holders_pct_change_4w(rows: list[dict]) -> float:
    """latest holders_pct vs 4 週前的相對變化率((latest - old) / old)。

    需要至少 5 週資料(index 0=4 週前,-1=latest)。任一缺值或 old<=0 → 0.0。
    """
    if len(rows) < 5:
        return 0.0
    latest = rows[-1].get("holders_pct")
    old = rows[0].get("holders_pct")
    if latest is None or old is None or old <= 0:
        return 0.0
    return (float(latest) - float(old)) / float(old)


def _compute_inst_5d_zscore(inst_total: pd.Series) -> float:
    """法人 5 日累計 z-score:用最近 20 天的 5-day-rolling-sum 分布算 (latest_5d - μ) / σ。

    inst_total: asc 排序的「每日法人三大買賣超總和(張)」序列。長度 < 20 →
    rolling 5-sum 樣本太少 → 0.0;σ ≤ 0 → 0.0。
    """
    if len(inst_total) < 20:
        return 0.0
    rolling5 = inst_total.rolling(5).sum()
    last20 = rolling5.dropna().tail(20)
    if len(last20) < 5:
        return 0.0
    latest_5d = float(inst_total.tail(5).sum())
    mu = float(last20.mean())
    sigma = float(last20.std(ddof=1))
    if pd.isna(sigma) or sigma <= 0:
        return 0.0
    return (latest_5d - mu) / sigma


def _compute_regime_dummy(
    target_date: str,
    db_path: str | Path | None = None,
) -> float:
    """大盤 regime ordinal(bull=2 ... bear=-2,unknown=0)。

    第一次呼叫 cache 結果在 _REGIME_CACHE[(target_date, db_path)],後續同
    target_date predict_batch 不再打 TAIEX SQL。TAIEX 不足 60 天 → unknown=0。
    """
    key = (target_date, str(db_path) if db_path else "")
    if key in _REGIME_CACHE:
        return _REGIME_CACHE[key]
    try:
        from src.market_regime import compute_regime

        info = compute_regime(target_date, db_path=db_path)
        v = float(_REGIME_ORDINAL.get(info["regime"], 0.0))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[ML] _compute_regime_dummy %s fallback:%s", target_date, e,
        )
        v = 0.0
    _REGIME_CACHE[key] = v
    return v

# Label 設定
LABEL_LOOKAHEAD_DAYS = 5
LABEL_ATR_MULT = 1.5

# 訓練 lookback(每筆 sample 需 N 天歷史 + LABEL_LOOKAHEAD_DAYS 後續)
# 雲端全市場 2401 檔中位數 55 trading days(FinMind 90 calendar days 漏抓部分),
# 只 6 檔 ≥60 天 → 60 天門檻幾乎所有 picks 都過不了。降 45:MACD(26+9=35)剛
# 好夠 + 緩衝,ma_alignment 也改用 MA5/MA20(拿掉 MA60 因 60 天才能算)。
MIN_HISTORY_DAYS = 45


def _load_history(
    stock_id: str,
    end_date: str,
    days: int = 90,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """SQL 撈該股 end_date 之前 N 天 daily_prices(含 end_date)。"""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume "
            "FROM daily_prices "
            "WHERE stock_id=? AND date <= ? "
            "ORDER BY date DESC LIMIT ?",
            (stock_id, end_date, days),
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.sort_values("date").reset_index(drop=True)


def _load_inst(
    stock_id: str,
    end_date: str,
    days: int = 30,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    """SQL 撈該股 end_date 之前 N 天 institutional。空 → 空 DF。"""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT date, foreign_buy_sell, trust_buy_sell, dealer_buy_sell "
            "FROM institutional "
            "WHERE stock_id=? AND date <= ? "
            "ORDER BY date DESC LIMIT ?",
            (stock_id, end_date, days),
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.sort_values("date").reset_index(drop=True)


def extract_features(
    stock_id: str,
    target_date: str,
    db_path: str | Path | None = None,
    verbose: bool = False,
) -> dict[str, float] | None:
    """抽 11 維 features 給訓練 / 推論用。資料不足 / NaN → 回 None。

    寬鬆 anchor:用「該股 daily_prices ≤ target_date 內最後可得一天」當基準
    (不嚴格要求 df.iloc[-1].date == target_date)。雲端 preload 的某些 pick
    在 target_date 那天 cache 缺(停牌 / 漏抓 / backfill 沒這檔最新一天)
    時仍能算 features,只是 anchor 往前 1-N 天。

    訓練時 sliding window 永遠用 in-cache date,等同 strict;只有推論時這個
    寬鬆才生效 — 不影響 model 訓練 distribution。

    verbose=True 印各失敗點原因(給 build_training_dataset 訓練診斷用)。
    """
    df = _load_history(stock_id, target_date, days=90, db_path=db_path)
    if len(df) < MIN_HISTORY_DAYS:
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} skip: "
                f"history={len(df)} < {MIN_HISTORY_DAYS}",
                flush=True,
            )
        return None

    close_last = float(df["close"].iloc[-1])
    if close_last <= 0:
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} skip: "
                f"close_last={close_last} ≤ 0",
                flush=True,
            )
        return None

    # KD
    kd = ind.kd(df, n=9)
    kd_k = float(kd["K"].iloc[-1]) if not kd.empty else float("nan")
    kd_d = float(kd["D"].iloc[-1]) if not kd.empty else float("nan")

    # MACD
    macd = ind.macd(df, fast=12, slow=26, signal=9)
    macd_dif = float(macd["DIF"].iloc[-1]) if not macd.empty else float("nan")
    macd_hist = float(macd["HIST"].iloc[-1]) if not macd.empty else float("nan")

    # MA 排列 score:2=多頭(ma5 > ma20)/ 0=空頭 / 1=糾結
    # 拿掉 MA60(需 60 天,雲端 95% picks 歷史不足)。短期 ma5/ma20 排列仍能
    # 反映 trend 方向,給 RandomForest 學就好。
    ma5 = ind.sma(df, 5).iloc[-1]
    ma20 = ind.sma(df, 20).iloc[-1]
    if pd.isna(ma5) or pd.isna(ma20):
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} skip: "
                f"MA NaN(ma5={ma5}, ma20={ma20})",
                flush=True,
            )
        return None
    if ma5 > ma20:
        ma_alignment = 2.0
    elif ma5 < ma20:
        ma_alignment = 0.0
    else:
        ma_alignment = 1.0

    # BB 位置(0=下軌, 1=上軌)
    bb = ind.bollinger(df, period=20, num_std=2.0)
    bb_upper = float(bb["upper"].iloc[-1])
    bb_lower = float(bb["lower"].iloc[-1])
    bb_range = bb_upper - bb_lower
    bb_position = (
        (close_last - bb_lower) / bb_range if bb_range > 0 else 0.5
    )
    bb_position = max(0.0, min(1.0, bb_position))

    # 量比
    vol_last = float(df["volume"].iloc[-1])
    vol_ma5 = float(df["volume"].tail(5).mean())
    vol_ratio = vol_last / vol_ma5 if vol_ma5 > 0 else 1.0

    # 乖離率 vs MA20
    bias_pct = (close_last - ma20) / ma20 * 100 if ma20 > 0 else 0.0

    # ATR 正規化
    atr14 = ind.atr(df, period=14).iloc[-1]
    atr_normalized = (
        float(atr14) / close_last * 100 if not pd.isna(atr14) and close_last > 0
        else 0.0
    )

    # 法人 5/10 日累計(張)— institutional 沒資料 → 0
    inst_df = _load_inst(stock_id, target_date, days=30, db_path=db_path)
    inst_total: pd.Series = pd.Series([], dtype=float)
    if not inst_df.empty:
        inst_total = (
            inst_df["foreign_buy_sell"].fillna(0)
            + inst_df["trust_buy_sell"].fillna(0)
            + inst_df["dealer_buy_sell"].fillna(0)
        ) / 1000  # 股 → 張
        inst_5d = float(inst_total.tail(5).sum())
        inst_10d = float(inst_total.tail(10).sum())
    else:
        inst_5d = 0.0
        inst_10d = 0.0

    # === v3 高階特徵(每個獨立 try/except → 0.0 fallback,不 drop row) ===
    try:
        holders_rows = _load_holders_weeks(
            stock_id, target_date, weeks=5, db_path=db_path,
        )
        holders_delta_w_zscore = _compute_holders_delta_w_zscore(holders_rows)
        holders_pct_change_4w = _compute_holders_pct_change_4w(holders_rows)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} holders fallback:{e}",
                flush=True,
            )
        holders_delta_w_zscore = 0.0
        holders_pct_change_4w = 0.0

    try:
        inst_5d_zscore = (
            _compute_inst_5d_zscore(inst_total) if not inst_total.empty else 0.0
        )
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} inst_zscore fallback:"
                f"{e}",
                flush=True,
            )
        inst_5d_zscore = 0.0

    try:
        regime_dummy = _compute_regime_dummy(target_date, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} regime fallback:{e}",
                flush=True,
            )
        regime_dummy = 0.0

    try:
        is_theme_member = (
            1.0 if stock_id in _load_theme_member_sids() else 0.0
        )
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} theme fallback:{e}",
                flush=True,
            )
        is_theme_member = 0.0

    feats = {
        "kd_k": kd_k, "kd_d": kd_d,
        "macd_dif": macd_dif, "macd_osc": macd_hist,
        "ma_alignment": ma_alignment,
        "bb_position": bb_position,
        "vol_ratio": vol_ratio,
        "bias_pct": bias_pct,
        "atr_normalized": atr_normalized,
        "inst_5d": inst_5d,
        "inst_10d": inst_10d,
        # v3
        "holders_delta_w_zscore": holders_delta_w_zscore,
        "inst_5d_zscore": inst_5d_zscore,
        "regime_dummy": regime_dummy,
        "holders_pct_change_4w": holders_pct_change_4w,
        "is_theme_member": is_theme_member,
    }
    # v2 base 11 個保持嚴格 NaN/Inf check(歷史行為);v3 5 個內部已 fallback 0.0,
    # 不可能 NaN/Inf,所以只檢 v2 那 11 個
    v2_keys = FEATURE_NAMES[:11]
    bad_keys = [
        k for k in v2_keys if pd.isna(feats[k]) or np.isinf(feats[k])
    ]
    if bad_keys:
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} skip: "
                f"NaN/Inf in {bad_keys}",
                flush=True,
            )
        return None
    return feats


def compute_label(
    stock_id: str,
    target_date: str,
    lookahead_days: int = LABEL_LOOKAHEAD_DAYS,
    atr_mult: float = LABEL_ATR_MULT,
    db_path: str | Path | None = None,
) -> int | None:
    """Label:進場後 lookahead_days 內,high 是否 ≥ 進場 close + atr_mult × ATR。

    1 = win(觸到目標),0 = loss,None = 資料不足。
    """
    # 進場日歷史(算 ATR)
    df_entry = _load_history(stock_id, target_date, days=90, db_path=db_path)
    if len(df_entry) < 15:
        return None
    if df_entry["date"].iloc[-1] != target_date:
        return None

    entry_close = float(df_entry["close"].iloc[-1])
    atr14 = ind.atr(df_entry, period=14).iloc[-1]
    if pd.isna(atr14) or atr14 <= 0 or entry_close <= 0:
        return None
    target_price = entry_close + atr_mult * atr14

    # 後續 N 天 high
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT high FROM daily_prices "
            "WHERE stock_id=? AND date > ? "
            "ORDER BY date ASC LIMIT ?",
            (stock_id, target_date, lookahead_days),
        ).fetchall()
    if len(rows) < lookahead_days:
        return None  # 資料不夠 lookahead → 不訓練此 sample

    max_high = max(
        float(r["high"]) for r in rows if r["high"] is not None
    )
    return 1 if max_high >= target_price else 0


def build_training_dataset(
    stock_ids: list[str] | None = None,
    db_path: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """對每檔 stock_id 跑 sliding window,構造 (X, y) 訓練資料。

    對每個合法 trade_date(該檔有 ≥60 天歷史 + 後 5 天有資料):
        extract_features → X row
        compute_label → y row

    沒傳 stock_ids → 用 TW_TOP_50(限制訓練時間)。
    """
    from src.universe import TW_TOP_50

    if stock_ids is None:
        stock_ids = [s for s, _ in TW_TOP_50]

    feats_list: list[dict] = []
    labels: list[int] = []
    for sid in stock_ids:
        # 拿該檔所有 dates 來 sliding window
        with db.get_conn(db_path) as conn:
            dates = [
                r["date"] for r in conn.execute(
                    "SELECT date FROM daily_prices WHERE stock_id=? "
                    "ORDER BY date ASC",
                    (sid,),
                ).fetchall()
            ]
        if len(dates) < MIN_HISTORY_DAYS + LABEL_LOOKAHEAD_DAYS:
            continue
        # 跳過前 MIN_HISTORY_DAYS - 1 個(features 不足) +
        # 最後 LABEL_LOOKAHEAD_DAYS 個(label 沒未來資料)
        for d in dates[MIN_HISTORY_DAYS - 1: -LABEL_LOOKAHEAD_DAYS]:
            f = extract_features(sid, d, db_path=db_path)
            if f is None:
                continue
            y = compute_label(sid, d, db_path=db_path)
            if y is None:
                continue
            feats_list.append(f)
            labels.append(y)

    if not feats_list:
        return pd.DataFrame(columns=FEATURE_NAMES), pd.Series([], dtype=int)
    X = pd.DataFrame(feats_list)[FEATURE_NAMES]
    y = pd.Series(labels)
    return X, y


def train_short_pick_model(X: pd.DataFrame, y: pd.Series):
    """訓練 RandomForestClassifier(n_estimators=100, max_depth=5)。

    回 (model, metrics_dict)。metrics 含 accuracy / precision / recall / f1
    (用 train/test 8:2 split 評估)。

    sklearn lazy import 避免 src/ 其他 module(notifier 等)被 transitive
    import 拖累 cold-start。
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import (
        accuracy_score, precision_score, recall_score, f1_score,
    )

    if len(X) < 50:
        raise ValueError(
            f"訓練資料太少({len(X)} samples),最少需 50 筆才能 train"
        )

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y if len(set(y)) > 1 else None,
    )
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=5,
        min_samples_leaf=5,
        class_weight="balanced",  # 應對 win/loss 不平衡
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)
    metrics = {
        "n_train": len(X_train),
        "n_test": len(X_test),
        "win_rate_overall": float(y.mean()),
        "accuracy": float(accuracy_score(y_test, y_pred)),
        "precision": float(precision_score(y_test, y_pred, zero_division=0)),
        "recall": float(recall_score(y_test, y_pred, zero_division=0)),
        "f1": float(f1_score(y_test, y_pred, zero_division=0)),
    }
    return model, metrics


def predict_short_pick_winrate(
    model,
    stock_id: str,
    target_date: str,
    db_path: str | Path | None = None,
    calibrator=None,
) -> float | None:
    """對單檔單日預測 win 機率(0-1)。資料不足 → None。

    calibrator 不為 None 且 ML_CALIBRATION_ENABLED 開啟 → raw_prob 經
    calibrator.transform 才回傳;否則維持 raw_prob(完全 backward-compat)。

    print(flush=True) 取代 logger.warning,確保 GitHub Actions / streamlit cloud
    log 看得到 sklearn 端失敗(predict_proba shape / dtype / class mismatch)。
    """
    feats = extract_features(stock_id, target_date, db_path=db_path)
    if feats is None:
        return None
    X = pd.DataFrame([feats])[_aligned_feature_names(model)]
    try:
        proba = model.predict_proba(X)[0]
        # class 1 = win 的機率(predict_proba 的 columns 是 model.classes_ 順序)
        if 1 in model.classes_:
            idx = list(model.classes_).index(1)
            raw = float(proba[idx])
        else:
            # weird case:model 訓練時沒看到 win class(全 loss)
            print(
                f"[ML/predict] {stock_id}@{target_date} model.classes_="
                f"{list(model.classes_)} 沒包含 1,回 0.0",
                flush=True,
            )
            return 0.0
        # 套校正
        if calibrator is not None:
            from src import ml_calibration

            calibrated = ml_calibration.apply_calibration(
                model, calibrator, np.array([raw]),
            )
            return float(calibrated[0])
        return raw
    except Exception as e:  # noqa: BLE001
        # 雲端 log:logger.warning 不一定 capture,改 print 確保看得到
        print(
            f"[ML/predict] {stock_id}@{target_date} sklearn predict_proba 失敗:"
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        logger.warning("[ML] predict 失敗:%s", e)
        return None


def predict_batch(
    model,
    stock_ids: list[str],
    target_date: str,
    db_path: str | Path | None = None,
    calibrator=None,
) -> dict[str, float | None]:
    """對一批 sids 預測 win 機率,一次 model.predict_proba 而非 N 次。

    calibrator 不為 None 且 ML_CALIBRATION_ENABLED 開啟 → raw_prob 經
    calibrator.transform 才回傳;否則維持 raw_prob(完全 backward-compat,
    既有 callers 沒傳 calibrator 行為一致)。

    回 {sid: prob_up | None};資料不足(extract_features 回 None)的 sid
    在 dict 內仍出現但值為 None。

    比 predict_short_pick_winrate × N 快 — sklearn 一次 batch 呼叫省 N-1
    次 Python ↔ C 邊界。
    """
    if model is None or not stock_ids:
        return {}

    # extract_features 個別跑(沒 batch 版本,SQL 已經是 per-sid 90-day pull)
    rows: list[dict] = []
    sids_with_features: list[str] = []
    for sid in stock_ids:
        feats = extract_features(sid, target_date, db_path=db_path)
        if feats is None:
            continue
        rows.append(feats)
        sids_with_features.append(sid)

    out: dict[str, float | None] = {sid: None for sid in stock_ids}
    if not rows:
        return out

    X = pd.DataFrame(rows)[_aligned_feature_names(model)]
    try:
        probas = model.predict_proba(X)
        if 1 in model.classes_:
            idx = list(model.classes_).index(1)
            raw_probs = np.asarray([float(r[idx]) for r in probas])
            if calibrator is not None:
                from src import ml_calibration

                calibrated = ml_calibration.apply_calibration(
                    model, calibrator, raw_probs,
                )
                for sid, p in zip(sids_with_features, calibrated):
                    out[sid] = float(p)
            else:
                for sid, p in zip(sids_with_features, raw_probs):
                    out[sid] = float(p)
        else:
            # 罕見:訓練資料全 loss class
            print(
                "[ML/predict_batch] model.classes_ 沒包含 1,全回 0.0",
                flush=True,
            )
            for sid in sids_with_features:
                out[sid] = 0.0
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML/predict_batch] sklearn predict_proba 失敗 "
            f"({len(sids_with_features)} sids): {type(e).__name__}: {e}",
            flush=True,
        )
        logger.warning("[ML] predict_batch 失敗:%s", e)
        # 失敗時 dict 內全 None(對齊 caller fallback 行為)
    return out


def save_model(model, path: str | Path) -> None:
    """joblib dump 到 path。parent 不存在會建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, p)


def _meta_path(model_path: str | Path) -> Path:
    """sidecar metadata 路徑:把 .pkl 換成 .meta.json。"""
    p = Path(model_path)
    return p.with_suffix(".meta.json")


def dump_model_meta(
    model_path: str | Path,
    metrics: dict,
    feature_names: list[str] | None = None,
    model_type: str = "RandomForestClassifier",
    version: str = MODEL_VERSION,
    min_history_days: int = MIN_HISTORY_DAYS,
) -> Path:
    """訓練完成後 dump metadata 到 .meta.json sidecar。

    給「⚙️ 系統」頁顯示模型 metrics 用。回寫入路徑。
    """
    import json
    from datetime import datetime, timezone

    p = _meta_path(model_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "samples": int(metrics.get("n_train", 0) + metrics.get("n_test", 0)),
        "n_train": int(metrics.get("n_train", 0)),
        "n_test": int(metrics.get("n_test", 0)),
        "features_count": len(feature_names) if feature_names else len(FEATURE_NAMES),
        "feature_names": feature_names or FEATURE_NAMES,
        "min_history_days": int(min_history_days),
        "metrics": {
            "base_win_rate": float(metrics.get("win_rate_overall", 0.0)),
            "accuracy": float(metrics.get("accuracy", 0.0)),
            "precision": float(metrics.get("precision", 0.0)),
            "recall": float(metrics.get("recall", 0.0)),
            "f1": float(metrics.get("f1", 0.0)),
        },
        "model_type": model_type,
        "version": version,
    }
    # calibration block(若 train flow 有跑 calibration)— 給 system_brief 讀
    cal = metrics.get("calibration")
    if cal:
        meta["calibration"] = {
            "method": cal.get("method"),
            "n_holdout": int(cal.get("n_holdout", 0)),
            "raw_brier": float(cal.get("raw_brier", 0.0)),
            "calibrated_brier": float(cal.get("calibrated_brier", 0.0)),
        }
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_model_meta(model_path: str | Path) -> dict | None:
    """讀 .meta.json sidecar。檔不存在 / parse 失敗 → 回 None。"""
    import json

    p = _meta_path(model_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[ML] load_model_meta 失敗(%s):%s", p, e)
        return None


def load_model(path: str | Path):
    """joblib load。檔不存在或 load 失敗 → 回 None。

    print 診斷 log 排查雲端找不到 / sklearn 版本不相容的根因(streamlit cloud
    的 logger.warning 不一定看得到,改用 print(flush=True))。
    """
    p = Path(path)
    if not p.exists():
        print(f"[ML] load_model: 檔不存在 {p}", flush=True)
        return None
    try:
        m = joblib.load(p)
        print(f"[ML] load_model: OK 從 {p} 載入 {type(m).__name__}", flush=True)
        return m
    except Exception as e:  # noqa: BLE001
        print(
            f"[ML] load_model 失敗(可能 sklearn 版本不相容)"
            f":{type(e).__name__}: {e}",
            flush=True,
        )
        logger.warning("[ML] load_model 失敗(%s):%s", path, e)
        return None


def per_strategy_model_path(strategy_name: str) -> Path:
    """返回 models/per_strategy/<strategy>.pkl 絕對路徑(不檢查存不存在)。"""
    from src import config
    return Path(config.PROJECT_ROOT) / "models" / "per_strategy" / f"{strategy_name}.pkl"


def load_strategy_model(strategy_name: str):
    """載入 models/per_strategy/<strategy>.pkl;檔不存在 / load 失敗 → 回 None。

    Stage 2B inference 路由用 — caller(predict_for_strategy)拿到 None 自動
    fallback 到通用模型。

    刻意不 cache(streamlit 端在 _get_strategy_ml_model 包 cache_resource;
    backtest CLI 端則由 caller 自行 dict 暫存,避免每 strategy × 每 D 重
    load)。joblib.load 一次約 50-100ms,可承受。
    """
    return load_model(per_strategy_model_path(strategy_name))


def load_strategy_calibrator(strategy_name: str):
    """載入 models/calibrators/<strategy>.pkl;檔不存在 → 回 None。

    Calibration kill-switch / fallback graceful:caller 直接拿 None 傳給
    predict_for_strategy,該函式內部會走 raw prob。
    """
    from src import ml_calibration
    return ml_calibration.load_calibrator(strategy_name)


def load_short_pick_calibrator():
    """載入 models/calibrators/short_pick.pkl;不存在 → None。"""
    from src import ml_calibration
    return ml_calibration.load_calibrator("short_pick")


def predict_for_strategy(
    strategy_name: str | None,
    stock_ids: list[str],
    target_date: str,
    fallback_model=None,
    db_path: str | Path | None = None,
    strategy_model=None,
    strategy_calibrator=None,
    fallback_calibrator=None,
) -> dict[str, float | None]:
    """Stage 2B per-strategy inference 路由 — 優先用 per-strategy model,沒就用
    fallback(通用)model。

    **Caller 要自己預載 per-strategy model(直接傳 strategy_model;若 None 則
    本函式 fallback 直接用 fallback_model,不再 disk load)** — 這樣 backtest
    每 D × 每 strategy 不會反覆 read pkl(N×N 次 IO)。strategy_name 仍保留
    當 metadata,讓 log 看得出哪個策略在預測。

    校正:同樣由 caller 預載 calibrator 傳入。chosen=strategy_model 時用
    strategy_calibrator,否則用 fallback_calibrator。calibrator 為 None
    時直接 raw prob(完全 backward-compat,既有 callers 行為不變)。

    Args:
        strategy_name: 紀錄用 strategy name(可以 None);**已不再觸發 disk
            load**。caller 想路由 per-strategy 必須自己傳 strategy_model。
        stock_ids: 要預測的 sids list
        target_date: 'YYYY-MM-DD'
        fallback_model: 通用模型;strategy_model 沒給時用。None 時兩個都沒
            → 全 None dict 回傳。
        strategy_model: 已載入的 per-strategy model;優先於 fallback。
        strategy_calibrator: 對應 strategy_model 的 calibrator(可以 None)
        fallback_calibrator: 對應 fallback_model 的 calibrator(可以 None)

    Returns:
        {sid: prob | None};沒任何 model + sids 非空 → {sid: None}
    """
    if not stock_ids:
        return {}

    use_strategy = strategy_model is not None
    chosen = strategy_model if use_strategy else fallback_model
    chosen_cal = strategy_calibrator if use_strategy else fallback_calibrator
    if chosen is None:
        return {sid: None for sid in stock_ids}

    return predict_batch(
        chosen, stock_ids, target_date,
        db_path=db_path, calibrator=chosen_cal,
    )


def train_with_calibration(
    X: pd.DataFrame,
    y: pd.Series,
    dates: pd.Series | None = None,
    *,
    holdout_frac: float = 0.2,
    calibration_method: str = "isotonic",
    rf_kwargs: dict | None = None,
) -> tuple:
    """Train base RF on first (1-holdout) samples + fit calibrator on holdout。

    時序 split:把 X / y 按 dates(沒給就假設 caller 已排好)排序,**最後**
    holdout_frac 比例的樣本做 holdout(不能 random shuffle — 時間序列 random
    split 會 leak 未來資訊回 train)。

    args:
        X / y:訓練資料(features + label)
        dates:對應每 row 的 date(YYYY-MM-DD 字串);None → 假設 X 已時序排好
        holdout_frac:後段 holdout 比例(default 0.2)
        calibration_method:'isotonic'(預設)/ 'platt';樣本 < 500 自動 fallback platt
        rf_kwargs:覆寫 RandomForest 預設 hyperparams

    returns:
        (base_model, calibrator, metrics) — metrics 含 holdout / train sample 數、
        raw + calibrated brier score、calibration method used、win_rate 等。
        樣本 < 50 或 holdout 全同類 → raise ValueError(對齊 train_short_pick_model)。
    """
    from sklearn.ensemble import RandomForestClassifier
    from src import ml_calibration

    if len(X) < 50:
        raise ValueError(
            f"訓練資料太少({len(X)} samples),最少 50 筆才能 train"
        )
    if len(X) != len(y):
        raise ValueError(f"X / y 長度不一致:{len(X)} vs {len(y)}")

    # 時序排序:dates 給的話按其排,否則假設 X 已排好
    if dates is not None:
        if len(dates) != len(X):
            raise ValueError(
                f"dates / X 長度不一致:{len(dates)} vs {len(X)}"
            )
        order = pd.Series(list(dates)).reset_index(drop=True).argsort(
            kind="stable",
        )
        X_sorted = X.iloc[order].reset_index(drop=True)
        y_sorted = pd.Series(y).iloc[order].reset_index(drop=True)
    else:
        X_sorted = X.reset_index(drop=True)
        y_sorted = pd.Series(y).reset_index(drop=True)

    # 後段 holdout
    n = len(X_sorted)
    holdout_n = max(1, int(round(n * holdout_frac)))
    train_n = n - holdout_n
    if train_n < 30:
        raise ValueError(
            f"扣 holdout 後訓練集太少({train_n} < 30),holdout_frac 太大或資料不足"
        )
    X_train = X_sorted.iloc[:train_n]
    y_train = y_sorted.iloc[:train_n].astype(int)
    X_holdout = X_sorted.iloc[train_n:]
    y_holdout = y_sorted.iloc[train_n:].astype(int)

    if len(set(y_train.tolist())) < 2:
        raise ValueError(
            "train 段全同類(time-based split 偏掉了),無法 train 二元分類"
        )
    if len(set(y_holdout.tolist())) < 2:
        raise ValueError(
            "holdout 段全同類,無法 fit calibrator(需 win + loss 都有)"
        )

    kwargs = dict(
        n_estimators=100,
        max_depth=5,
        min_samples_leaf=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    if rf_kwargs:
        kwargs.update(rf_kwargs)
    model = RandomForestClassifier(**kwargs)
    model.fit(X_train, y_train)

    # raw probs on holdout for calibration + brier
    raw_holdout = ml_calibration._extract_positive_proba(model, X_holdout)
    raw_metrics = ml_calibration.compute_calibration_metrics(
        np.asarray(y_holdout), raw_holdout,
    )

    calibrator = ml_calibration.fit_calibrator(
        model, X_holdout, y_holdout, method=calibration_method,
    )
    calibrated_holdout = calibrator.transform(raw_holdout)
    cal_metrics = ml_calibration.compute_calibration_metrics(
        np.asarray(y_holdout), calibrated_holdout,
    )

    metrics = {
        "n_train": int(train_n),
        "n_holdout": int(holdout_n),
        "n_total": int(n),
        "win_rate_overall": float(np.mean(y_sorted)),
        "calibration_method": calibrator.method,
        "raw_brier": raw_metrics["brier_score"],
        "calibrated_brier": cal_metrics["brier_score"],
        "raw_reliability_bins": raw_metrics["reliability_bins"],
        "calibrated_reliability_bins": cal_metrics["reliability_bins"],
    }
    return model, calibrator, metrics


__all__ = [
    "FEATURE_NAMES",
    "MODEL_VERSION",
    "MIN_HISTORY_DAYS",
    "extract_features",
    "compute_label",
    "build_training_dataset",
    "train_short_pick_model",
    "train_with_calibration",
    "predict_short_pick_winrate",
    "predict_batch",
    "save_model",
    "load_model",
    "load_strategy_model",
    "load_strategy_calibrator",
    "load_short_pick_calibrator",
    "predict_for_strategy",
    "per_strategy_model_path",
    "dump_model_meta",
    "load_model_meta",
]
