"""台股精選清單(快速選股範圍用)。

TW_TOP_50:
  約 50 檔台股大型股(參考 0050 ETF 成份股組合),作為「快速選股」的預設範圍,
  避免無 token 模式呼叫 FinMind 抓全市場被頻率限制(無 token 連續打數百次就會被擋)。

load_watchlist():
  讀取使用者自訂關注清單(`data/watchlist.txt`),一行一個股號,# 開頭算註解。

load_theme_universe():
  讀取 `data/themes/*.yaml` 所有 sids 的 union,給 institutional/ shareholder
  concentration backfill 當主題基底用。

get_volume_top_n():
  依最近 N 個交易日的平均成交量,從 daily_prices 算 Top M 檔。

build_institutional_universe():
  Top-volume + theme + TW_TOP_50 + watchlist 的 union,給 daily_fetch 抓
  institutional 用(拓寬到主公拍板的 300-500 檔涵蓋)。
"""
from __future__ import annotations

from pathlib import Path

from src import config, database as db


TW_TOP_50: list[tuple[str, str]] = [
    # === 半導體 / 電子代工 (15 檔) ===
    ("2330", "台積電"),
    ("2317", "鴻海"),
    ("2454", "聯發科"),
    ("2303", "聯電"),
    ("3711", "日月光投控"),
    ("3034", "聯詠"),
    ("3008", "大立光"),
    ("2379", "瑞昱"),
    ("6669", "緯穎"),
    ("3231", "緯創"),
    ("2382", "廣達"),
    ("2357", "華碩"),
    ("2376", "技嘉"),
    ("2308", "台達電"),
    ("2474", "可成"),
    # === 金融 (13 檔) ===
    ("2881", "富邦金"),
    ("2882", "國泰金"),
    ("2883", "開發金"),
    ("2884", "玉山金"),
    ("2885", "元大金"),
    ("2886", "兆豐金"),
    ("2887", "台新金"),
    ("2890", "永豐金"),
    ("2891", "中信金"),
    ("2892", "第一金"),
    ("2880", "華南金"),
    ("2823", "中壽"),
    ("5880", "合庫金"),
    # === 塑化 / 食品 (5 檔) ===
    ("1301", "台塑"),
    ("1303", "南亞"),
    ("1326", "台化"),
    ("6505", "台塑化"),
    ("1216", "統一"),
    # === 鋼鐵 / 水泥 (3 檔) ===
    ("2002", "中鋼"),
    ("1101", "台泥"),
    ("1102", "亞泥"),
    # === 電信 (3 檔) ===
    ("2412", "中華電"),
    ("3045", "台灣大"),
    ("4904", "遠傳"),
    # === 航運 / 運輸 (6 檔) ===
    ("2603", "長榮"),
    ("2609", "陽明"),
    ("2615", "萬海"),
    ("2618", "長榮航"),
    ("2610", "華航"),
    ("2207", "和泰車"),
    # === 零售 / 民生 (1 檔) ===
    ("2912", "統一超"),
    # === 其他 (4 檔) ===
    ("5871", "中租-KY"),
    ("2105", "正新"),
    ("3037", "欣興"),
    ("2356", "英業達"),
]

assert len(TW_TOP_50) == 50, f"TW_TOP_50 應為 50 檔,目前 {len(TW_TOP_50)}"


WATCHLIST_PATH = config.PROJECT_ROOT / "data" / "watchlist.txt"


def load_watchlist() -> list[tuple[str, str]]:
    """讀取 `data/watchlist.txt`,一行一個股號,# 開頭算註解。

    名稱優先從 stocks 表查,查不到用股號當名稱。
    回傳 [(stock_id, name), ...];檔案不存在或內容為空時回 []。
    """
    if not WATCHLIST_PATH.exists():
        return []
    sids: list[str] = []
    for line in WATCHLIST_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            sids.append(s)
    if not sids:
        return []
    placeholders = ",".join(["?"] * len(sids))
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT stock_id, name FROM stocks WHERE stock_id IN ({placeholders})",
            sids,
        ).fetchall()
    name_map = {r["stock_id"]: r["name"] for r in rows}
    return [(s, name_map.get(s, s)) for s in sids]


def get_full_universe(refresh: bool = False) -> list[str]:
    """取全市場 stock_id list (twse 上市 + tpex 上櫃,含 ETF)。

    流程:
      1. 預設從 SQLite stocks 表拿(>= 1000 筆視為已 init)
      2. 不足 / refresh → 打 FinMind TaiwanStockInfo 抓 4093 筆,
         篩出 type in {twse, tpex} 寫入 stocks 表
      3. 回所有有 name 的 stock_id

    回 list[str],約 2360 筆(TWSE 1355 + TPEx 1005,含 ETF)。
    """
    db.init_db()
    if not refresh:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT stock_id FROM stocks "
                "WHERE market='TW' AND name IS NOT NULL AND name != ''"
            ).fetchall()
        if len(rows) >= 1000:
            return [r["stock_id"] for r in rows]

    # 第一次 / refresh:打 FinMind 抓全市場
    from src.data_fetcher import _fetch_all_stock_info
    try:
        all_info = _fetch_all_stock_info()
    except Exception:
        return []

    rows_to_upsert = []
    for sid, raw in all_info.items():
        type_ = (raw.get("type") or "").lower()
        if type_ not in ("twse", "tpex"):
            continue
        name = raw.get("stock_name") or ""
        if not name:
            continue
        rows_to_upsert.append({
            "stock_id": sid,
            "name": name,
            "industry": raw.get("industry_category"),
            "type": type_,
            "market": "TW",
        })
    if rows_to_upsert:
        db.upsert_stocks(rows_to_upsert)
    return [r["stock_id"] for r in rows_to_upsert]


# === 純股票過濾(排除 ETF / 債券 / 槓桿反向 / ETN 等衍生品)===

# 名稱關鍵字黑名單 — 中文比對用 `in name`(原大小寫即可),英文走 lower-case
_NAME_KW_ZH = [
    "美債", "公債", "債券", "投等債", "金融債", "高收債", "可轉債",
    "槓桿", "反向", "正2", "正二", "反1", "反一",
]
_NAME_KW_EN_LOWER = ["etf", "etn"]


def is_pure_stock(stock_id: str, name: str | None) -> bool:
    """判斷是否為「純股票」 — 即非 ETF / 債券 / 槓桿 / ETN 等衍生品。

    短線策略主要在「個股」上找量價籌碼信號,ETF 是組合產品(貝塔不純),
    債券 ETF 流動性低且漲跌邏輯完全不同,過濾掉避免污染選股結果。

    過濾規則:
    1. 代號 "00" 開頭 → ETF / ETN(如 0050 / 00929 / 00764B / 00631L)
    2. 名稱含 ETF / ETN(英文不分大小寫)
    3. 名稱含「美債 / 公債 / 債券 / 投等債」等債券關鍵字
    4. 名稱含「槓桿 / 反向 / 正2 / 反1」等槓桿/反向衍生品關鍵字

    回 True = 是純股票,可選股;False = 該過濾掉。
    """
    if not stock_id:
        return False
    # ETF 慣例:台股 ETF / ETN 一律 4 碼以 "00" 開頭(0050 / 00929 / 00764B)
    if stock_id.startswith("00"):
        return False
    if not name:
        # 沒名字保守視為不確定 → 留著(避免誤殺剛上市還沒寫進 stocks 表的個股)
        return True
    name_lower = name.lower()
    if any(kw in name_lower for kw in _NAME_KW_EN_LOWER):
        return False
    if any(kw in name for kw in _NAME_KW_ZH):
        return False
    return True


def pure_stock_universe(min_history: int = 20) -> list[str]:
    """回 stock_id 清單(過濾 ETF / 債券 / 槓桿反向 + 歷史天數 >= min_history)。

    給 dashboard / 短線推薦 / 推播 等多個 caller 共用,避免 ETF/債券 noise。

    回傳順序穩定:依 stocks 表的 stock_id 升序。
    """
    sids_with_history = set(db.stocks_with_min_history(min_history))
    if not sids_with_history:
        return []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, name FROM stocks WHERE market='TW' "
            "ORDER BY stock_id"
        ).fetchall()
    return [
        r["stock_id"] for r in rows
        if r["stock_id"] in sids_with_history
        and is_pure_stock(r["stock_id"], r["name"])
    ]


THEMES_DIR = config.PROJECT_ROOT / "data" / "themes"


def load_theme_universe(themes_dir: Path | None = None) -> list[str]:
    """讀 `data/themes/*.yaml`,union 所有檔案內 `sids:` 清單。

    YAML schema:
        sids:
          - "2330"
          - "2317"
          ...

    沒有 yaml 套件 / 目錄不存在 / yaml 解析失敗 → 回 []。
    回傳順序穩定:升序去重。
    """
    d = themes_dir or THEMES_DIR
    if not d.exists():
        return []
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return []
    sids: set[str] = set()
    for fp in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        raw_sids = data.get("sids") or []
        if not isinstance(raw_sids, list):
            continue
        for s in raw_sids:
            if s is None:
                continue
            sids.add(str(s).strip())
    return sorted(s for s in sids if s)


def get_volume_top_n(
    top_n: int = 300,
    lookback_days: int = 5,
    db_path=None,
) -> list[str]:
    """依最近 `lookback_days` 個交易日的平均成交量,從 daily_prices 算 Top N 檔。

    過濾:排除 TAIEX(大盤指數,not 個股)+ 非純股票(ETF / 槓桿 / 反向 / 債券)。
    回傳順序依平均量降序;daily_prices 空 / 任何錯誤 → []。
    """
    if top_n < 1:
        return []
    try:
        with db.get_conn(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) AS d FROM daily_prices "
                "WHERE stock_id != 'TAIEX'"
            ).fetchone()
            if not row or not row["d"]:
                return []
            latest = row["d"]
            # 撈最近 lookback_days 個 distinct 日期(避免假日 / 空白日子拉低)
            recent_dates = conn.execute(
                "SELECT DISTINCT date FROM daily_prices "
                "WHERE stock_id != 'TAIEX' AND date <= ? "
                "ORDER BY date DESC LIMIT ?",
                (latest, lookback_days),
            ).fetchall()
            if not recent_dates:
                return []
            min_date = recent_dates[-1]["date"]
            # 聚合:平均量(SUM/COUNT 避開 NULL),要 stocks 表 join 拿 name
            # 走過濾 ETF / 衍生品(is_pure_stock 同邏輯,SQL 端先粗刪 "00" 起頭)
            rows = conn.execute(
                "SELECT dp.stock_id, s.name, "
                "       AVG(COALESCE(dp.volume, 0)) AS avg_vol "
                "FROM daily_prices dp "
                "LEFT JOIN stocks s ON s.stock_id = dp.stock_id "
                "WHERE dp.date BETWEEN ? AND ? "
                "  AND dp.stock_id != 'TAIEX' "
                "  AND dp.stock_id NOT LIKE '00%' "
                "GROUP BY dp.stock_id "
                "HAVING avg_vol > 0 "
                "ORDER BY avg_vol DESC "
                "LIMIT ?",
                (min_date, latest, top_n * 2),  # 多取 2x 給 python 端過濾
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []

    out: list[str] = []
    for r in rows:
        sid = r["stock_id"]
        name = r["name"]
        if not is_pure_stock(sid, name):
            continue
        out.append(sid)
        if len(out) >= top_n:
            break
    return out


def build_institutional_universe(
    top_volume_n: int = 300,
    include_themes: bool = True,
    include_watchlist: bool = True,
    include_top50: bool = True,
    lookback_days: int = 5,
    db_path=None,
) -> list[str]:
    """組合 institutional 抓取的擴大 universe:Top-volume + 主題 + watchlist + Top50。

    主公 2026-05-15 拍板:每日 institutional 抓 49 檔太窄(無大戶訊號)→
    擴到 ~300-500 檔(成交量 Top N + theme universe + watchlist + Top50 union)。

    Args:
        top_volume_n: 成交量 Top N(預設 300)
        include_themes: 是否納入 `data/themes/*.yaml` 主題(~144 檔)
        include_watchlist: 是否納入 `data/watchlist.txt`
        include_top50: 是否納入 TW_TOP_50(藍籌墊底,新上市量還沒衝高時保險)
        lookback_days: 計算成交量 Top N 用幾天平均(預設 5)

    Returns:
        list[str] 升序去重的 stock_id 清單。
    """
    sids: set[str] = set()
    if include_top50:
        sids.update(s for s, _ in TW_TOP_50)
    if include_watchlist:
        sids.update(s for s, _ in load_watchlist())
    if include_themes:
        sids.update(load_theme_universe())
    if top_volume_n > 0:
        sids.update(
            get_volume_top_n(
                top_n=top_volume_n,
                lookback_days=lookback_days,
                db_path=db_path,
            )
        )
    return sorted(sids)


__all__ = [
    "TW_TOP_50", "WATCHLIST_PATH", "THEMES_DIR", "load_watchlist",
    "get_full_universe", "is_pure_stock", "pure_stock_universe",
    "load_theme_universe", "get_volume_top_n",
    "build_institutional_universe",
]
