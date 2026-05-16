"""scripts/backfill_institutional.py 單元測試。

scripts/ 不是 package,用 importlib 載入。mock requests.Session 的 .get,
餵 fake TWSE / TPEx / FinMind JSON,驗 parsing + upsert + idempotent re-fetch。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import config, database as db


_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "backfill_institutional.py"
)
_spec = importlib.util.spec_from_file_location(
    "backfill_institutional", _SCRIPT,
)
bi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bi)


# === Fixtures ===

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """tmp DB 路徑,init schema。"""
    db_file = tmp_path / "inst.db"
    monkeypatch.setattr(config, "DATABASE_PATH", str(db_file))
    db._reset_path_cache()
    db.init_db()
    return db_file


@pytest.fixture
def fake_twse_payload():
    """mimic TWSE T86 ALL 回的 JSON 結構(精簡版)。"""
    return {
        "stat": "OK",
        "date": "20251103",
        "fields": [
            "證券代號",
            "證券名稱",
            "外陸資買進股數(不含外資自營商)",
            "外陸資賣出股數(不含外資自營商)",
            "外陸資買賣超股數(不含外資自營商)",
            "外資自營商買進股數",
            "外資自營商賣出股數",
            "外資自營商買賣超股數",
            "投信買進股數",
            "投信賣出股數",
            "投信買賣超股數",
            "自營商買賣超股數",
            "自營商買進股數(自行買賣)",
            "自營商賣出股數(自行買賣)",
            "自營商買賣超股數(自行買賣)",
            "自營商買進股數(避險)",
            "自營商賣出股數(避險)",
            "自營商買賣超股數(避險)",
            "三大法人買賣超股數",
        ],
        "data": [
            [
                "2330", "台積電",
                "10,000,000", "2,000,000", "8,000,000",  # foreign-excl-prop
                "500,000", "100,000", "400,000",         # foreign-prop
                "1,000,000", "200,000", "800,000",       # trust
                "300,000",                                # dealer-total (combined)
                "200,000", "50,000", "150,000",          # dealer-self
                "100,000", "50,000", "50,000",           # dealer-hedge
                "9,400,000",                              # total
            ],
            [
                "2454", "聯發科",
                "1,000,000", "1,500,000", "-500,000",
                "0", "0", "0",
                "100,000", "200,000", "-100,000",
                "-50,000",
                "0", "0", "0",
                "0", "50,000", "-50,000",
                "-650,000",
            ],
        ],
    }


@pytest.fixture
def fake_tpex_payload():
    """mimic TPEx 3insti 回的 JSON 結構。"""
    return {
        "tables": [
            {
                "fields": [
                    "代號", "名稱",
                    "外資及陸資(不含自營商)買進股數",
                    "外資及陸資(不含自營商)賣出股數",
                    "外資及陸資(不含自營商)買賣超股數",
                    "外資自營商買進股數",
                    "外資自營商賣出股數",
                    "外資自營商買賣超股數",
                    "投信買進股數",
                    "投信賣出股數",
                    "投信買賣超股數",
                    "自營商(自行買賣)買進股數",
                    "自營商(自行買賣)賣出股數",
                    "自營商(自行買賣)買賣超股數",
                    "自營商(避險)買進股數",
                    "自營商(避險)賣出股數",
                    "自營商(避險)買賣超股數",
                    "三大法人買賣超股數合計",
                ],
                "data": [
                    [
                        "6488", "環球晶",
                        "500,000", "300,000", "200,000",
                        "0", "0", "0",
                        "100,000", "0", "100,000",
                        "10,000", "5,000", "5,000",
                        "0", "0", "0",
                        "305,000",
                    ],
                ],
            },
        ],
    }


def _mock_session_with_responses(monkeypatch, responses):
    """`responses` = [{'url_contains': 'twse|tpex', 'json': dict, 'status': 200}]
    依序回。沒 match 就 raise。
    """
    call_log = []

    def _fake_get(url, params=None, timeout=None, **_):
        call_log.append({"url": url, "params": params})
        if not responses:
            pytest.fail(f"沒設定 mock response 給 url={url}")
        resp = responses.pop(0)
        mock_resp = MagicMock()
        mock_resp.status_code = resp.get("status", 200)
        mock_resp.text = "(mock)"
        if resp.get("raise"):
            raise resp["raise"]
        if resp.get("non_json"):
            mock_resp.json.side_effect = ValueError("not JSON")
        else:
            mock_resp.json.return_value = resp["json"]
        return mock_resp

    return _fake_get, call_log


# === Parsing tests ===

def test_parse_twse_t86_uses_field_names(fake_twse_payload):
    rows = bi.parse_twse_t86(fake_twse_payload, "2025-11-03")
    assert len(rows) == 2
    r0 = next(r for r in rows if r["stock_id"] == "2330")
    # foreign = 8M (excl-prop) + 400K (prop) = 8,400,000
    assert r0["foreign_buy_sell"] == 8_400_000
    # trust = 800K
    assert r0["trust_buy_sell"] == 800_000
    # dealer = self (150K) + hedge (50K) = 200K(優先 self+hedge,不用 dealer-total)
    assert r0["dealer_buy_sell"] == 200_000
    # total = 9,400,000 (直接從欄位拿)
    assert r0["total_buy_sell"] == 9_400_000
    assert r0["date"] == "2025-11-03"


def test_parse_twse_t86_handles_negative_and_dashes():
    """負數、'--'、空字串、None 都該轉成 0 或正確的 int。"""
    payload = {
        "stat": "OK",
        "fields": [
            "證券代號",
            "外陸資買賣超股數(不含外資自營商)",
            "投信買賣超股數",
            "自營商買賣超股數",
            "三大法人買賣超股數",
        ],
        "data": [
            ["1101", "-100", "--", "", "-100"],
            ["1102", "1,234,567", "0", None, "1,234,567"],
        ],
    }
    rows = bi.parse_twse_t86(payload, "2025-11-03")
    r0 = next(r for r in rows if r["stock_id"] == "1101")
    assert r0["foreign_buy_sell"] == -100
    assert r0["trust_buy_sell"] == 0  # '--' → 0
    assert r0["dealer_buy_sell"] == 0
    assert r0["total_buy_sell"] == -100
    r1 = next(r for r in rows if r["stock_id"] == "1102")
    assert r1["foreign_buy_sell"] == 1_234_567


def test_parse_twse_t86_empty_data_returns_empty_list():
    assert bi.parse_twse_t86({"stat": "OK", "fields": [], "data": []}, "x") == []
    assert bi.parse_twse_t86({}, "x") == []
    assert bi.parse_twse_t86(None, "x") == []


def test_parse_tpex_3insti_with_tables_wrapper(fake_tpex_payload):
    rows = bi.parse_tpex_3insti(fake_tpex_payload, "2025-11-03")
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "6488"
    # foreign = 200K (excl-prop) + 0 (prop) = 200,000
    assert r["foreign_buy_sell"] == 200_000
    assert r["trust_buy_sell"] == 100_000
    # dealer = self (5K) + hedge (0) = 5,000
    assert r["dealer_buy_sell"] == 5_000
    assert r["total_buy_sell"] == 305_000


def test_parse_tpex_3insti_repeated_field_names_positional():
    """新格式:fields 名稱都是「買賣超股數」重複 7 次,parser 應該認得 position 4/13/16/19/23。"""
    payload = {
        "tables": [
            {
                "fields": [
                    "代號", "名稱",
                    "買進股數", "賣出股數", "買賣超股數",  # 2-4 foreign excl prop
                    "買進股數", "賣出股數", "買賣超股數",  # 5-7 foreign prop
                    "買進股數", "賣出股數", "買賣超股數",  # 8-10 外資合計
                    "買進股數", "賣出股數", "買賣超股數",  # 11-13 投信
                    "買進股數", "賣出股數", "買賣超股數",  # 14-16 自營自行
                    "買進股數", "賣出股數", "買賣超股數",  # 17-19 自營避險
                    "買進股數", "賣出股數", "買賣超股數",  # 20-22 自營合計
                    "三大法人買賣超股數合計",                # 23
                ],
                "data": [
                    ["006201", "元大富櫃50",
                     "0", "2,000", "-2,000",      # foreign excl prop net = -2000
                     "0", "0", "0",                # foreign prop net = 0
                     "0", "2,000", "-2,000",       # 外資合計 (skipped)
                     "0", "0", "0",                # trust net = 0
                     "0", "0", "0",                # dealer self net = 0
                     "10,032", "156,441", "-146,409",  # dealer hedge net = -146409
                     "10,032", "156,441", "-146,409",  # 自營合計 (skipped)
                     "-148,409"],                  # total
                ],
            },
        ],
    }
    rows = bi.parse_tpex_3insti(payload, "2025-11-03")
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "006201"
    assert r["foreign_buy_sell"] == -2000  # -2000 + 0
    assert r["trust_buy_sell"] == 0
    assert r["dealer_buy_sell"] == -146409  # 0 + -146409
    assert r["total_buy_sell"] == -148409


def test_parse_tpex_3insti_fallback_no_fields():
    """老 aaData 格式 — 沒 fields header,走 fallback index 解析。"""
    payload = {
        "aaData": [
            ["6488", "環球晶", "0", "0", "200000",  # col 4 = foreign net
             "0", "0", "100000",                     # col 7 = trust net
             "0", "0", "5000",                       # col 10 = dealer net
             "305000"],                              # 最後 = total
        ],
    }
    rows = bi.parse_tpex_3insti(payload, "2025-11-03")
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "6488"
    assert r["foreign_buy_sell"] == 200_000
    assert r["trust_buy_sell"] == 100_000
    assert r["dealer_buy_sell"] == 5_000
    assert r["total_buy_sell"] == 305_000


# === Date helpers ===

def test_iter_workdays_skips_weekends():
    # 2025-11-01 = 週六, 11-02 = 週日, 11-03 = 週一, ..., 11-07 = 週五, 11-08 = 週六
    days = list(bi._iter_workdays("2025-11-01", "2025-11-09"))
    assert days == ["2025-11-03", "2025-11-04", "2025-11-05",
                    "2025-11-06", "2025-11-07"]


def test_iso_to_roc():
    assert bi._iso_to_roc("2025-11-03") == "114/11/03"
    assert bi._iso_to_roc("2024-01-02") == "113/01/02"


def test_iso_to_twse():
    assert bi._iso_to_twse("2025-11-03") == "20251103"


def test_parse_int_robust():
    assert bi._parse_int("1,234,567") == 1_234_567
    assert bi._parse_int("--") == 0
    assert bi._parse_int("-") == 0
    assert bi._parse_int("") == 0
    assert bi._parse_int(None) == 0
    assert bi._parse_int("-100") == -100
    assert bi._parse_int(42) == 42
    assert bi._parse_int("abc") == 0


# === Existing-date check ===

def test_existing_dates_threshold(tmp_db):
    """row 數 < min_rows 的日期該被視為「沒覆蓋」,讓 backfill 補完。"""
    db.upsert_institutional([
        {"stock_id": f"{1000 + i}", "date": "2025-11-04",
         "foreign_buy_sell": 1, "trust_buy_sell": 0, "dealer_buy_sell": 0,
         "total_buy_sell": 1}
        for i in range(150)  # 150 行 >= 100
    ])
    db.upsert_institutional([
        {"stock_id": f"{2000 + i}", "date": "2025-11-05",
         "foreign_buy_sell": 1, "trust_buy_sell": 0, "dealer_buy_sell": 0,
         "total_buy_sell": 1}
        for i in range(50)  # 50 行 < 100
    ])
    existing = bi.existing_dates("2025-11-01", "2025-11-30")
    assert "2025-11-04" in existing  # >= 100
    assert "2025-11-05" not in existing  # < 100,該被補


# === backfill_one_date orchestration ===

def test_backfill_one_date_combines_twse_and_tpex(
    tmp_db, monkeypatch, fake_twse_payload, fake_tpex_payload,
):
    responses = [
        {"json": fake_twse_payload},   # TWSE
        {"json": fake_tpex_payload},   # TPEx
    ]
    fake_get, _ = _mock_session_with_responses(monkeypatch, responses)
    session = MagicMock()
    session.get.side_effect = fake_get

    # 把 backfill_one_date 內的 sleep 拔掉加速
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    n, source = bi.backfill_one_date("2025-11-03", session)
    assert n == 3  # 2 from TWSE + 1 from TPEx
    assert source == "twse+tpex"

    # 驗 DB 真的寫入
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT stock_id, foreign_buy_sell FROM institutional "
            "WHERE date='2025-11-03' ORDER BY stock_id"
        ).fetchall()
    sids = [r["stock_id"] for r in rows]
    assert sids == ["2330", "2454", "6488"]


def test_backfill_one_date_twse_fail_tpex_ok(tmp_db, monkeypatch, fake_tpex_payload):
    """TWSE 掛掉但 TPEx OK → 用 tpex_only,不該 raise。"""
    twse_fail_responses = [
        # TWSE 3 次都 500 → with_retry 全失敗
        {"status": 500, "json": {}},
        {"status": 500, "json": {}},
        {"status": 500, "json": {}},
        # TPEx OK
        {"json": fake_tpex_payload},
    ]
    fake_get, _ = _mock_session_with_responses(monkeypatch, twse_fail_responses)
    session = MagicMock()
    session.get.side_effect = fake_get
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    n, source = bi.backfill_one_date(
        "2025-11-03", session, max_retries=3, use_finmind_fallback=False,
    )
    assert n == 1
    assert source == "tpex_only"


def test_backfill_one_date_both_fail_raises(tmp_db, monkeypatch):
    """TWSE + TPEx 都掛 → raise EndpointError(不靜默吞)。"""
    responses = [{"status": 500, "json": {}}] * 6  # 3 retries × 2 endpoints
    fake_get, _ = _mock_session_with_responses(monkeypatch, responses)
    session = MagicMock()
    session.get.side_effect = fake_get
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    with pytest.raises(bi.EndpointError):
        bi.backfill_one_date(
            "2025-11-03", session, max_retries=3, use_finmind_fallback=False,
        )


def test_backfill_one_date_idempotent_reupsert(
    tmp_db, monkeypatch, fake_twse_payload, fake_tpex_payload,
):
    """同一天跑兩次,DB row 數應該保持不變(ON CONFLICT DO UPDATE)。"""
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    # 第 1 次
    fake_get1, _ = _mock_session_with_responses(
        monkeypatch,
        [{"json": fake_twse_payload}, {"json": fake_tpex_payload}],
    )
    s1 = MagicMock()
    s1.get.side_effect = fake_get1
    bi.backfill_one_date("2025-11-03", s1)

    with db.get_conn() as conn:
        c1 = conn.execute(
            "SELECT COUNT(*) FROM institutional WHERE date='2025-11-03'"
        ).fetchone()[0]
    assert c1 == 3

    # 第 2 次,同一份資料
    fake_get2, _ = _mock_session_with_responses(
        monkeypatch,
        [{"json": fake_twse_payload}, {"json": fake_tpex_payload}],
    )
    s2 = MagicMock()
    s2.get.side_effect = fake_get2
    bi.backfill_one_date("2025-11-03", s2)

    with db.get_conn() as conn:
        c2 = conn.execute(
            "SELECT COUNT(*) FROM institutional WHERE date='2025-11-03'"
        ).fetchone()[0]
    assert c2 == c1  # 沒膨脹,證 idempotent


def test_backfill_one_date_holiday_empty_response(tmp_db, monkeypatch):
    """假日:兩個 endpoint 都回空 data(stat=OK 但 data 空)→ (0, 'empty')。"""
    holiday_resp = {"stat": "OK", "fields": [], "data": []}
    responses = [{"json": holiday_resp}, {"json": holiday_resp}]
    fake_get, _ = _mock_session_with_responses(monkeypatch, responses)
    session = MagicMock()
    session.get.side_effect = fake_get
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    n, source = bi.backfill_one_date(
        "2025-12-25", session, use_finmind_fallback=False,
    )
    assert n == 0
    assert source == "empty"


def test_main_skips_existing_dates_and_completes(
    tmp_db, monkeypatch, fake_twse_payload, fake_tpex_payload,
):
    """main() 應跳過已有的 date,只打缺的;結束 exit code = 0。"""
    # 預灌 2025-11-03 已有 200 行(>= 100 threshold)
    db.upsert_institutional([
        {"stock_id": f"{1000 + i}", "date": "2025-11-03",
         "foreign_buy_sell": 0, "trust_buy_sell": 0, "dealer_buy_sell": 0,
         "total_buy_sell": 0}
        for i in range(200)
    ])

    # 預期只打 2025-11-04 一天(2 calls: TWSE + TPEx)
    responses = [{"json": fake_twse_payload}, {"json": fake_tpex_payload}]
    fake_get, call_log = _mock_session_with_responses(monkeypatch, responses)

    # patch requests.Session 拿到的 session.get 為 fake
    fake_session = MagicMock()
    fake_session.get.side_effect = fake_get
    fake_session.headers = {}
    monkeypatch.setattr(bi.requests, "Session", lambda: fake_session)
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    code = bi.main([
        "--start", "2025-11-03", "--end", "2025-11-04",
        "--no-finmind-fallback", "--sleep", "0",
    ])
    assert code == 0
    # 只該打過 11-04 — 2 calls(TWSE + TPEx),不該打到 11-03
    twse_calls = [
        c for c in call_log if "twse" in c["url"]
    ]
    assert len(twse_calls) == 1
    assert twse_calls[0]["params"]["date"] == "20251104"


def test_dump_snapshot_csv_merges_with_existing(tmp_db, monkeypatch, tmp_path):
    """dump_snapshot_csv 該跟既有 CSV merge,同 (sid, date) 去重。"""
    snapshot_dir = tmp_path / "snapshot"
    monkeypatch.setattr(bi, "SNAPSHOT_DIR", snapshot_dir)
    snapshot_dir.mkdir()

    # 既有 CSV: 2025-11-04 兩檔
    existing_csv = snapshot_dir / "institutional.csv"
    existing_csv.write_text(
        "stock_id,date,foreign_buy_sell,trust_buy_sell,"
        "dealer_buy_sell,total_buy_sell\n"
        "1326,2025-11-04,918159,0,-87423,830736\n"
        "2330,2025-11-04,100,0,0,100\n",
        encoding="utf-8",
    )

    # DB 內補了 2025-11-03 一檔 + 2025-11-04 一檔(同 sid 但不同值,該蓋掉)
    db.upsert_institutional([
        {"stock_id": "2330", "date": "2025-11-03",
         "foreign_buy_sell": 500, "trust_buy_sell": 0,
         "dealer_buy_sell": 0, "total_buy_sell": 500},
        {"stock_id": "2330", "date": "2025-11-04",
         "foreign_buy_sell": 999, "trust_buy_sell": 0,
         "dealer_buy_sell": 0, "total_buy_sell": 999},
    ])

    n = bi.dump_snapshot_csv("2025-11-03", "2025-11-04")
    assert n >= 3

    import pandas as pd
    df = pd.read_csv(existing_csv, dtype={"stock_id": str})
    # 2330/2025-11-04 該被新值蓋掉
    new_2330_1104 = df[(df["stock_id"] == "2330") & (df["date"] == "2025-11-04")]
    assert len(new_2330_1104) == 1
    assert int(new_2330_1104.iloc[0]["foreign_buy_sell"]) == 999
    # 1326/2025-11-04 該保留
    keep = df[(df["stock_id"] == "1326") & (df["date"] == "2025-11-04")]
    assert len(keep) == 1
    # 2330/2025-11-03 該新增
    new = df[(df["stock_id"] == "2330") & (df["date"] == "2025-11-03")]
    assert len(new) == 1


def test_main_bad_date_args_returns_2():
    code = bi.main(["--start", "not-a-date", "--end", "2025-11-03"])
    assert code == 2


def test_main_start_after_end_returns_2():
    code = bi.main(["--start", "2025-11-30", "--end", "2025-11-01"])
    assert code == 2


# === FinMind fallback ===

def test_finmind_fallback_triggered_when_twse_tpex_fail(tmp_db, monkeypatch):
    """TWSE/TPEx 都掛,且 finmind_token 有值 → 走 FinMind path。"""
    finmind_payload = {
        "status": 200,
        "data": [
            {"stock_id": "2330", "date": "2025-11-03",
             "name": "Foreign_Investor", "buy": 1000, "sell": 200},
            {"stock_id": "2330", "date": "2025-11-03",
             "name": "Investment_Trust", "buy": 500, "sell": 100},
            {"stock_id": "2330", "date": "2025-11-03",
             "name": "Dealer_self", "buy": 0, "sell": 50},
        ],
    }
    responses = (
        [{"status": 500, "json": {}}] * 3  # TWSE 失敗 3 次
        + [{"status": 500, "json": {}}] * 3  # TPEx 失敗 3 次
        + [{"json": finmind_payload}]
    )
    fake_get, _ = _mock_session_with_responses(monkeypatch, responses)
    session = MagicMock()
    session.get.side_effect = fake_get
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    n, source = bi.backfill_one_date(
        "2025-11-03", session,
        max_retries=3, use_finmind_fallback=True, finmind_token="dummy",
    )
    assert n == 1
    assert source == "finmind"

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM institutional WHERE stock_id='2330' AND date='2025-11-03'"
        ).fetchone()
    assert row["foreign_buy_sell"] == 800  # 1000 - 200
    assert row["trust_buy_sell"] == 400    # 500 - 100
    assert row["dealer_buy_sell"] == -50   # 0 - 50


# === Retry on transient errors ===

def test_retry_recovers_on_second_attempt(
    tmp_db, monkeypatch, fake_twse_payload, fake_tpex_payload,
):
    """TWSE 第 1 次 timeout,第 2 次成功 → 該 OK,不 fail。"""
    import requests as rq
    responses = [
        {"raise": rq.ConnectTimeout("connect timeout")},
        {"json": fake_twse_payload},
        {"json": fake_tpex_payload},
    ]
    fake_get, _ = _mock_session_with_responses(monkeypatch, responses)
    session = MagicMock()
    session.get.side_effect = fake_get
    monkeypatch.setattr(bi.time, "sleep", lambda _: None)

    n, source = bi.backfill_one_date(
        "2025-11-03", session, max_retries=3, use_finmind_fallback=False,
    )
    assert n == 3
    assert source == "twse+tpex"
