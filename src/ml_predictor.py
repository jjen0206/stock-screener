"""短線勝率預測模型(RandomForestClassifier)。

特徵抽取 + label 構造 + 訓練 + 推論。模型 pkl 存 models/short_pick.pkl,
streamlit / 推播 reuse `predict_short_pick_winrate` 顯示「🎯 AI 勝率 N%」。

Label 定義:**進場後 5 個交易日內,high 是否 ≥ 進場 close + 1.5 × ATR(14)**
(觸到目標即算 win,實務貼近 stop-limit 賣單;比純看 5 日後 close 更貼近真實
短線交易行為)

特徵(11 個):
- kd_k / kd_d              KD9 K 值 / D 值
- macd_dif / macd_osc      MACD 12-26-9 DIF / OSC
- ma_alignment             MA5/MA20/MA60 排列 score(2=多頭/0=空頭/1=糾結)
- bb_position              close 在 BB 通道中的位置(0=下軌,1=上軌)
- vol_ratio                今日量 / 5 日均量
- bias_pct                 close vs MA20 乖離率 %
- atr_normalized           ATR(14) / close × 100(波動率)
- inst_5d / inst_10d       法人 5/10 日累計(張),沒資料 → 0

任何資料不足(< 60 天歷史)→ extract_features 回 None。

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


FEATURE_NAMES = [
    "kd_k", "kd_d", "macd_dif", "macd_osc", "ma_alignment",
    "bb_position", "vol_ratio", "bias_pct", "atr_normalized",
    "inst_5d", "inst_10d",
]

# Label 設定
LABEL_LOOKAHEAD_DAYS = 5
LABEL_ATR_MULT = 1.5

# 訓練 lookback(每筆 sample 需 60 天歷史 + LABEL_LOOKAHEAD_DAYS 後續)
MIN_HISTORY_DAYS = 60


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

    # MA 排列(score: 2=多頭排列,0=空頭,1=糾結)
    ma5 = ind.sma(df, 5).iloc[-1]
    ma20 = ind.sma(df, 20).iloc[-1]
    ma60 = ind.sma(df, 60).iloc[-1]
    if pd.isna(ma5) or pd.isna(ma20) or pd.isna(ma60):
        if verbose:
            print(
                f"[ML/extract] {stock_id}@{target_date} skip: "
                f"MA NaN(ma5={ma5}, ma20={ma20}, ma60={ma60})",
                flush=True,
            )
        return None
    if ma5 > ma20 > ma60:
        ma_alignment = 2.0
    elif ma5 < ma20 < ma60:
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
    }
    bad_keys = [k for k, v in feats.items() if pd.isna(v) or np.isinf(v)]
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
) -> float | None:
    """對單檔單日預測 win 機率(0-1)。資料不足 → None。

    print(flush=True) 取代 logger.warning,確保 GitHub Actions / streamlit cloud
    log 看得到 sklearn 端失敗(predict_proba shape / dtype / class mismatch)。
    """
    feats = extract_features(stock_id, target_date, db_path=db_path)
    if feats is None:
        return None
    X = pd.DataFrame([feats])[FEATURE_NAMES]
    try:
        proba = model.predict_proba(X)[0]
        # class 1 = win 的機率(predict_proba 的 columns 是 model.classes_ 順序)
        if 1 in model.classes_:
            idx = list(model.classes_).index(1)
            return float(proba[idx])
        # weird case:model 訓練時沒看到 win class(全 loss)
        print(
            f"[ML/predict] {stock_id}@{target_date} model.classes_="
            f"{list(model.classes_)} 沒包含 1,回 0.0",
            flush=True,
        )
        return 0.0
    except Exception as e:  # noqa: BLE001
        # 雲端 log:logger.warning 不一定 capture,改 print 確保看得到
        print(
            f"[ML/predict] {stock_id}@{target_date} sklearn predict_proba 失敗:"
            f"{type(e).__name__}: {e}",
            flush=True,
        )
        logger.warning("[ML] predict 失敗:%s", e)
        return None


def save_model(model, path: str | Path) -> None:
    """joblib dump 到 path。parent 不存在會建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, p)


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


__all__ = [
    "FEATURE_NAMES",
    "extract_features",
    "compute_label",
    "build_training_dataset",
    "train_short_pick_model",
    "predict_short_pick_winrate",
    "save_model",
    "load_model",
]
