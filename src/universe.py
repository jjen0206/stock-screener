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


__all__ = ["TW_TOP_50", "WATCHLIST_PATH", "load_watchlist"]
