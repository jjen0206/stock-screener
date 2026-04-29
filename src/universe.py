"""台股精選清單(快速選股範圍用)。

TW_TOP_50:
  約 50 檔台股大型股(參考 0050 ETF 成份股組合),作為「快速選股」的預設範圍,
  避免無 token 模式呼叫 FinMind 抓全市場被頻率限制(無 token 連續打數百次就會被擋)。

load_watchlist():
  讀取使用者自訂關注清單(`data/watchlist.txt`),一行一個股號,# 開頭算註解。
"""
from __future__ import annotations

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


__all__ = [
    "TW_TOP_50", "WATCHLIST_PATH", "load_watchlist",
    "get_full_universe", "is_pure_stock",
]
