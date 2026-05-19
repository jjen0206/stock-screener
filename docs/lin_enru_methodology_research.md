# 林恩如方法 — 深度研究報告

> **撰寫日期**:2026-05-19
> **資料來源**:4 個獨立 web 來源交叉驗證(見 Sources 區)
> **研究目的**:在 Phase A 開工前釐清「林恩如方法」的真實面貌,避免 spec 建立在錯的假設上

---

## 🚨 PART 0:重大警告 — 亮的 spec 跟真實林恩如方法**完全不符**

主公,在開始研究細節前,**必須先讓你看這個 misalignment 警告** —

亮在 v1/v2 spec 內描述的「林恩如方法」是這樣的:

| 亮 spec 描述 | 真實林恩如方法 |
|---|---|
| ✗ 基本面派(連 5 年 EPS 正、殖利率穩定、月營收) | ✓ **純技術派**(20 週均線、K 線型態、成交量) |
| ✗ 創 52 週新高才買 | ✓ **漲破 20 週均線**才買(完全不同訊號) |
| ✗ **不停損**(只設停利,任跌續抱領股利) | ✓ **嚴格停損**(跌破 20MA 立即砍)— 「停損是交易的精髓」 |
| ✗ 持有 1-3 年(無上限) | ✓ 由 20MA 決定(典型數週至數月,**不固定**) |
| ✗ 停利 +30%(可調) | ✓ **移動停利**(前一根週 K 最低點,跌破出場) |
| ✗ 「存股輪轉派」哲學 | ✓ **「飆股女王」/ 趨勢交易派**哲學 |
| ✗ 不看技術指標(刻意簡化) | ✓ **只**看技術指標(刻意簡化 = 簡化技術派工具,不是放棄技術) |
| ✗ 不看 K 線、不看 MACD/KD | ✓ 看 K 線型態(W 底 / M 頭)、看趨勢線、看成交量 |

**100% 證據鏈**(4 個獨立來源完全一致):
- Smart 自學網 / 商業周刊:「漲破 20 週均線就買進,跌破 20 週均線就賣出」
- CMoney 投資網誌(她官方專欄):「確認股價站上 20MA、跌破 20MA 紀律出場」
- url.com.tw 訪談:「停損是交易的精髓」「股價跌破 20 週均線時就應該賣掉手中持股」
- enrumoney.net 部落格:「本週週五突破週線 20MA → 進場;本週任何一天跌破 → 出場」

**主公決定點**(在 Phase A 開工前**必須選**):

- **A. 主公真的要做「真實林恩如方法」**(技術派 / 20週均線 / 嚴格停損)
  → v2 spec **整個作廢**,以本文 Part 1-5 為基礎重寫 v3
- **B. 主公本來想做的是「亮口中那個基本面存股」**(但弄錯人名)
  → 真正的方法可能是:**陳重銘**(《存股輪轉達人》)、**雪球股女郎温國信**(雪球股理論)、**棒喬飛**(波段存股實戰)、**棒球擊出全壘打的存股達人 udn 那 16 檔)等;**請主公指明**正確的人
- **C. 主公想要的就是 v2 spec 那個方法**(連 5 年 EPS + 不停損 + 52 週新高)
  → 那個方法**沒有正式名稱**,屬於亮 / 主公的 hybrid 創作;可以做但不要叫「林恩如方法」,改名「存股輪轉策略」之類,避免誤導

**Part 1-5 的後續內容,是「真實林恩如方法」的完整 spec**。主公先看完再選 A/B/C。

---

## Part 1:林恩如其人

### 1.1 經歷
- 20+ 年股市資歷(部分資料說 24 年 / 28 年,版本不同)
- **背景軌跡**:券商營業員(單月最高 7 億元成交量) → 高頻當沖(10 年)→ 波段趨勢交易(現)
- 痛點覺醒:「在當沖交易 10 年後發現,自己戶頭的錢很少是透過價差賺來的,多數是手續費折讓」
- 2000 年科技泡沫慘賠(智邦 120 → 20 元才認賠) → 「痛定思痛大量閱讀投資大師的書籍 → 15 年研究後發現交易其實是有系統的」 → 轉技術線型波段
- 推廣超簡單投資法 **13 年**(2013 起),**15,000+ 學員**
- 自詡「飆股女王」/「均線達人」

### 1.2 著作演進(時間軸)

| 年份 | 書名 | 出版社 | 方法版本 |
|---|---|---|---|
| 2020-07-29 | 《飆股女王林恩如,**超簡單投資法**:2 條均線 × 4 大法寶 × 公式選股》 | 幸福文化 → 後改商周 | **2 條均線版**(20 週 + 20 日) |
| ~2022-2024 | 《飆股女王林恩如·**超簡單趨勢波段投資法**:不用盯盤,靠 **1 條均線**找到 200% 飆股》 | 商周出版 | **1 條均線版**(可能更簡化) |
| 2025-12-18 | 《【暢銷珍藏版】**極簡投資**:股市小白也能 1 天搞懂的技術分析》 | 商周出版 | 入門普及版 |

**方法核心多年來沒變** — 都是「20 週均線進出」,只是後期書名強調「1 條均線」(主軸 20 週,日 K 20 日是輔助確認)。

### 1.3 衍生產品
- **CMoney「強棒旺旺來」App**(2017+ 起發行):她設計的選股軟體,內建 4 種選股策略
  - 強勢噴出(5 個交易日內第 3 根紅 K)
  - 跳空走高(開盤跳空 ≥ 2.5%)
  - 強勢排行(短中長期均線多頭排列 + 當日漲幅 > 2%)
  - 趨勢強多(均線多頭排列 + 當日漲幅 > 2%)
- **Shifu 大師課業**:線上課「超簡單趨勢波段投資法」
- **CMoney 理財寶**:多種付費學員班 / 體驗課

**重點**:她的選股 App 內**完全沒有基本面篩選**(EPS / 殖利率 / 配息) — 都是技術面型態識別。再次印證「純技術派」。

---

## Part 2:真實林恩如方法 — 完整可寫 code 的 spec

### 2.1 進場 5 條件(全 AND)

| # | 條件 | 具體判定 | 計算方式 |
|---|---|---|---|
| 1 | **漲破 20 週均線** | 本週 close ≥ 20 週 MA,且**上週 close < 20 週 MA**(新突破) | 週 K 線 SMA(close, 20) |
| 2 | **20 日均線向上突破 20 週均線** | 日 K 20MA(close, 20) > 週 K 20MA(close, 20) 且方向往上 | 兩種均線差 > 0 + 連 3 日上升 |
| 3 | **正處於上升趨勢線** | 過去 N 週的低點連線斜率 > 0,且 close 在線上方 | 取近 3-5 個週低點線性回歸 |
| 4 | **K 線出現 W 底**(底底高、峰峰高) | 近 8-12 週內有兩個 swing low,第二個 > 第一個,且中間有 swing high | swing point detection |
| 5 | **週成交量爆量** | 本週成交量 ≥ 過去 N 週均量 × 倍數(典型 1.5-2.0x) | 週 K volume SMA + threshold |

**0050 案例**(她書中的進場示範):
- 2019/08/30 進場
- 當天收盤 81.85 元
- 當天 20 週均線 80.93 元
- 均差距 = 81.85 - 80.93 = 0.92 元(這是「乖離量」)
- 乖離率 = 0.92 / 80.93 × 100% = **1.14%**(剛剛突破,還在合理買點範圍)
- 預設買 1 張(1000 股),最大虧損估算 = 0.92 × 1000 = **920 元**

### 2.2 出場規則 — 4 大停損訊號 + 移動停利

**停損(只要符合任一 → 立即出場,無條件)**:

| # | 訊號 | 判定 |
|---|---|---|
| 1 | 股價跌破 20 週均線 | 本週任何一日 close < 20 週 MA |
| 2 | 跌破上升趨勢線 | 收盤跌破趨勢線連線 |
| 3 | K 線出現 M 頭(峰峰低、底底低) | swing point detection 反向 |
| 4 | 股價持續創新低 | 連 3 週新低 |

**停利(達停損訊號之前,動態鎖獲利)**:
- **移動停利法**:「以**前一根週 K 棒的最低點**為準,若跌破就出場」
- 即:本週若 low < 上週 low → 出場

**林恩如名言**:「**停損是交易的精髓**」「練習沒有休止符,個性可以不服輸,但操作要勇於認輸,不遵守紀律市場會修理你」

### 2.3 風險量化公式(在進場前算)

她書中強調進場前必須算清楚最大虧損,確認自己**心理上承受得了再進場**:

```
均差距    = 當日收盤價 − 20 週均線價
最大虧損  = 均差距 × 1000(假設買 1 張 = 1000 股)
乖離率%   = (當日收盤價 − 20 週均線) / 20 週均線 × 100%
```

**進場乖離率上限**(書中沒明確,但案例提示):
- 0050 案例乖離率 **1.14%** = 合理進場
- 乖離率 > 5% 通常表示「追太高」,停損距離太遠,**避開**
- 我推測:**乖離率 ≤ 3-5% 才是安全進場區**(spec 可設成可調參數)

### 2.4 選股池(universe)

林恩如限制選股範圍 — **不是全市場 2400+ 檔都選**:

| 早期版本(2014-2019) | 後期版本(2020+) |
|---|---|
| **台灣 50 + 中型 100 = 共 150 檔** | **市值 20 億元以上中大型股**(寬鬆) |

選股池條件理由:「股本大、流通性夠,股價不易被人為操弄」

**對 stock-screener 的對照**:
- 現有 `TW_TOP_50`(50 檔)<< 林恩如的 150 檔
- 全市場 ~2400 檔 >> 林恩如要的 ~600 檔(市值 20 億+)
- **需要建 `lin_enru_universe.py`** 用市值篩選

### 2.5 資金配置

| 場景 | 部位比例 |
|---|---|
| **入門起點** | **50 萬閒錢**(扣掉生活費 / 緊急預備金後) |
| 同時持有檔數 | **3-5 檔**(分散 + 易管理) |
| 保留現金 | **3 成**(保命錢 — 心法 4) |
| 多頭強勢市場 | 可動用 5-7 成、甚至**融資** |
| 跌破 20MA 後 | 投資金額降到 **3 成**,融資轉現股 |
| 盤勢不佳 | 適當減碼 |

**單筆部位大小書中沒明確**,但合理推算:50 萬 × 70% / 5 檔 = **每檔 ~7 萬元** = 約 1-3 張中價股。

### 2.6 操作頻率與心態

- **看盤頻率**:每週五看一次篩選器(本週突破 / 跌破 20MA)。**不用每天盯盤**。
- **賣出時機**:每天檢查本週是否已跌破 20MA(只要有,馬上出)
- **進場時機**:只看週五收盤後篩選結果
- 名言:「**所有的答案,都在圖形中**」(她的招牌說法)
- 名言:「**百分之百遵守制定的交易規則**」

### 2.7 5 大投資心法

| # | 心法 | 對應 code 化 |
|---|---|---|
| 1 | 穩賺不賠的投資,就是投資自己 | (心理層,無 code) |
| 2 | 堅持信念,永不放棄 | (心理層) |
| 3 | 不間斷練習,愈賺愈上手 | (心理層) |
| 4 | 想敗部復活,請留**保命錢**(3 成現金) | 部位上限 ≤ 70% 強制檢查 |
| 5 | 不想認賠,你會賠更多(**嚴格設停損**) | 強制 stop_loss 邏輯 |

### 2.8 4 大法寶(技術分析工具)

| # | 工具 | 對應現有 stock-screener 模組 |
|---|---|---|
| 1 | **均線**(20 週 + 20 日) | `src/indicators.py` 已有 SMA helper,可直接用 |
| 2 | **趨勢線** | **現有 codebase 沒有**(沒有 swing point detection) |
| 3 | **K 線型態**(W 底 / M 頭) | `src/candlestick_patterns.py` 有部分 — 需確認 W 底 / M 頭有沒有 |
| 4 | **成交量**(週爆量) | `src/indicators.py` 應有 volume ratio,需確認週 K 聚合邏輯 |

---

## Part 3:跟 stock-screener 現有架構的對應

### 3.1 完整方法 → code module 對應

| 林恩如方法元素 | 寫進哪個模組(隔離) | 工時估 |
|---|---|---|
| 週 K 線聚合(daily → weekly resample) | `src/features/lin_enru/weekly_kline.py` | 1.5h |
| 20 週均線 + 20 日均線 | `src/features/lin_enru/ma_signals.py` | 1h |
| 上升 / 下降趨勢線偵測 | `src/features/lin_enru/trendline.py` | **4-6h**(swing point detection 是新算法) |
| W 底 / M 頭型態 | `src/features/lin_enru/pattern_w_m.py` | **3-4h**(難題:多少容差才算「峰峰高」) |
| 週成交量爆量 | `src/features/lin_enru/volume_signal.py` | 1h |
| 進場 5 條件 AND(主邏輯) | `src/strategies/lin_enru/screener.py` | 2.5h |
| 停損 4 訊號 + 移動停利(出場主邏輯) | `src/lin_enru_paper_trading.py` | 4h |
| 風險量化公式(均差距 / 乖離率) | `src/features/lin_enru/risk_calc.py` | 0.5h |
| 中大型股 universe(市值 20 億+) | `src/features/lin_enru/universe.py` | 1.5h(需 FinMind 抓 market_cap) |
| `lin_enru_trades` 表 schema | `src/database.py` 加一段 | 1h |
| backtest(週級別) | `src/lin_enru_backtest.py` | 4.5h |
| UI(新 tab + 卡片) | `app.py` + `src/lin_enru_ui_cards.py` | 6h |
| GHA workflow(每週五觸發) | `.github/workflows/lin-enru-weekly.yml` | 1.5h |

**真實 Phase A + B 工時(技術派版,隔離架構)**:
- A:9.5h(週 K 聚合 + 4 種 signal helper + universe)
- B:7h(screener + backtest)
- **總 16.5h**(對比 v2 假基本面版的 13.5h,**+3h**)

**為什麼比假基本面版多 3h?**
- 趨勢線偵測 + W 底型態識別 = 6-10h(假基本面版完全沒這個技術)
- 中大型股 universe 抓 market_cap = 1.5h(假版只用 TW_TOP_50)

---

### 3.2 跟現有 17 套策略對照

| 現有策略 | 跟林恩如重疊 | 差別 |
|---|---|---|
| `ma_alignment`(多頭排列) | **中-高** | 都看均線排列,但林恩如要 W 底 + 趨勢線 + 爆量,`ma_alignment` 只看 4 條均線排列 |
| `volume_breakout`(量爆突破 20 日) | 中 | 都看突破 + 量;林恩如用週 K 不是日 K |
| `bias_convergence`(乖離收斂) | 低 | `bias_convergence` 用日 K 20MA 乖離 -5% ~ +1%,林恩如用週 K 突破當下乖離 ≤ ~5% |
| `bb_lower_rebound`(布林下軌反彈) | 低 | 完全不同訊號 |

**結論**:**現有 17 套策略沒一套真的覆蓋林恩如方法**。即使最像的 `ma_alignment` 也是日 K 不是週 K、沒 W 底、沒趨勢線、沒週爆量。**林恩如方法有實質差異化價值**。

---

## Part 4:`lin_enru_trades` 表 schema(技術派版)

跟 v2 的「不停損 + 達停利」schema **完全不同**,改成符合真實林恩如方法:

```sql
CREATE TABLE IF NOT EXISTS lin_enru_trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sid                 TEXT NOT NULL,
    name                TEXT,
    entry_date          TEXT NOT NULL,      -- 週五進場日(週 K 收盤)
    entry_price         REAL NOT NULL CHECK(entry_price > 0),
    entry_week_ma20     REAL NOT NULL,      -- 進場時的 20 週 MA
    entry_bias_pct      REAL NOT NULL,      -- 乖離率 = (entry - week_ma20)/week_ma20 × 100
    max_loss_per_lot    REAL,               -- 均差距 × 1000(每張最大虧損)
    
    -- 進場時 5 條件 snapshot(debug + 復盤用)
    cond_break_w20      INTEGER NOT NULL,   -- 1=漲破 20 週 MA
    cond_d20_cross_w20  INTEGER NOT NULL,   -- 1=20 日 MA 向上突破 20 週 MA
    cond_uptrend        INTEGER NOT NULL,   -- 1=上升趨勢線
    cond_w_bottom       INTEGER NOT NULL,   -- 1=W 底型態
    cond_volume_burst   INTEGER NOT NULL,   -- 1=週爆量
    
    -- 移動停利狀態(每週更新)
    last_week_low       REAL,               -- 上週週 K 低點(下週判斷出場用)
    
    -- 出場(active 時為 NULL)
    exit_date           TEXT,
    exit_price          REAL,
    exit_reason         TEXT CHECK(exit_reason IN (
                            'break_w20',          -- 跌破 20 週 MA
                            'break_uptrend',      -- 跌破上升趨勢線
                            'm_top_pattern',      -- M 頭型態
                            'new_low_3w',         -- 連 3 週新低
                            'trail_stop'          -- 移動停利(跌破上週低)
                        )),
    return_pct          REAL,
    holding_weeks       INTEGER,            -- 持有週數
    
    status              TEXT NOT NULL CHECK(status IN ('active', 'closed')),
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    UNIQUE(sid, entry_date)
);

CREATE INDEX IF NOT EXISTS idx_lin_enru_trades_status ON lin_enru_trades(status);
CREATE INDEX IF NOT EXISTS idx_lin_enru_trades_entry ON lin_enru_trades(entry_date DESC);
```

**跟 v2 假基本面 schema 對比**(關鍵差異):
- **沒有** `target_price` / `target_pct`(林恩如**沒有固定停利目標**,完全靠移動停利動態)
- **沒有** `eps_streak_years` / `dividend_yield_pct` / `consec_div_years` / `high_52w_price`(這些是亮 v2 假版的基本面欄位)
- **新增** 5 個 `cond_*` 欄位記錄進場時技術訊號(復盤用)
- **新增** `last_week_low`(移動停利狀態,每週更新)
- **新增** `holding_weeks`(週級別持有期)
- **`exit_reason` enum 改 5 種**(全是技術訊號,沒有基本面崩 / 配息停)

---

## Part 5:`lin_enru_universe`(中大型股市值篩選)

林恩如的選股池是「市值 20 億+ 中大型股」≈ 600-800 檔。需要:

```python
# src/features/lin_enru/universe.py

def get_lin_enru_universe(min_market_cap_billion: float = 20.0) -> list[str]:
    """回符合林恩如選股池條件的 sid list。
    
    條件:
      - 上市(TWSE,排除 OTC 跟興櫃)
      - market_cap >= min_market_cap_billion 億新台幣
      - 排除 ETF / 權證 / 特別股(用 stocks.type 過濾)
    """
    # FinMind dataset: TaiwanStockInfo + TaiwanStockShareholding
    # 或直接 FinMind TaiwanStockMarketCap
```

**市值資料是缺的**(看 stocks 表 schema):
```sql
-- 現有 stocks 表沒有 market_cap 欄位
CREATE TABLE stocks (
    stock_id, name, market, industry, type, updated_at
)
```

**要加** `market_cap REAL` 欄位 + 每月 backfill 一次,**工時 +2h**(沒寫進原 Part 3.1 工時)。

---

## Part 6:回測設計(技術派版)

跟 v2 假基本面版**完全不同**:

| 項目 | v2 假版 | 真實林恩如版 |
|---|---|---|
| 時間單位 | 日 K | **週 K**(20 週 MA 是週 K 上跑) |
| Lookback | 5 年(252×5 = 1260 日) | **5 年**(52×5 = 260 週) |
| 進場頻率 | 每日掃 | **每週五掃**(reduce 1/5 工作量) |
| 出場掃描 | 每日掃 stop / target | **每日掃跌破 20 週 MA** + 每週五更新 last_week_low |
| Sweep 維度 | target_pct × yield × eps_years = 36 組 | **20 週 MA(嚴格 vs 容差 1%)× 爆量門檻(1.5x / 2.0x)× 上升趨勢線回看週數(3 / 5)** = 12 組 |
| 評估指標 | EV / WR / max DD | **EV / WR / max DD / 平均持有週數 / 進場乖離率分佈** |
| 對照組 | screen_long / high_yield_stable | **`ma_alignment`(日 K 多頭排列)/ buy & hold 0050** |

GATE 通過門檻**改技術派合理值**:

| 指標 | 門檻 | 理由 |
|---|---|---|
| Avg holding return | ≥ +8% | 技術派週級別,單筆不期待 +30%(基本面版才那樣) |
| WR | ≥ 50% | 嚴格停損特性 → 失敗單損失小,WR 50% 就能賺(對比基本面 55%) |
| Max DD(單筆) | ≤ -8% | 嚴格停損 → 應該不會超過進場乖離率 + 2-3% |
| 跑贏 `ma_alignment` baseline | EV +2pp 以上 | 都是均線策略,林恩如多 4 個 filter,要看是不是值得 |
| 跑贏 0050 buy & hold(同期) | 年化報酬 +5pp 以上 | 林恩如本人聲稱「年獲利 80%」,主公至少要勝大盤 |

---

## Part 7:Open Questions(主公需拍板)

| # | 問題 | 預設假設 |
|---|---|---|
| 1 | **真的要做真實林恩如方法嗎?**(misalignment 警告 A/B/C) | 等主公拍 |
| 2 | 進場乖離率上限要設多少?(書中沒明寫) | 我建議 ≤ 3%(可調參數) |
| 3 | W 底「峰峰高」容差?(書中沒明寫) | 我建議 swing low 第二個 ≥ 第一個 × 1.005(0.5% 內)|
| 4 | 週爆量倍數?(書中說「爆量」沒明寫倍數) | 我建議 1.8x(可調) |
| 5 | 趨勢線回看週數? | 我建議近 5 個週 swing low 線性回歸 |
| 6 | 選股池用「台灣 50 + 中型 100」(150 檔)還是「市值 20 億+」(~600 檔)? | 後者更接近她近年版本 |
| 7 | 移動停利「跌破上週週 K 低」的「跌破」用 close 還是 low? | 我建議 close(避免盤中假突破) |
| 8 | 持有期週數上限?(她沒設,但 backtest 要 cap) | 我建議 104 週(2 年)— 超過就視為錯訊號 |
| 9 | 多頭時可融資 — 要實作嗎? | 首版**不做**(個人專案不碰槓桿) |
| 10 | 5 大心法的「保命錢 3 成」要寫進 portfolio 檢查嗎? | 首版**不做**(portfolio sizing 是另一層) |

---

## Part 8:研究信心度與資料完整度

| 項目 | 信心度 | 來源數 |
|---|---|---|
| 「20 週均線進出」核心邏輯 | **100%** | 4 個獨立來源 |
| 進場 5 條件 | **100%** | Smart 自學網 + CMoney 學員班 |
| 停損 4 訊號 | **100%** | Smart 自學網 + CMoney 課程 |
| 移動停利「前一週低」公式 | 95% | Smart 自學網明確、其他來源含蓄 |
| 0050 案例 81.85 元 / 80.93 元數字 | **100%** | Smart 自學網逐字 |
| 兆赫 2485 案例 30 → 60 元 | **100%** | url.com.tw 逐字 |
| 智邦 120 → 20 元教訓 | **100%** | url.com.tw 逐字 |
| 資金 50 萬 / 3-5 檔 / 3 成現金 | **100%** | Smart 自學網 |
| 多頭 5-7 成 / 可融資 | **100%** | url.com.tw |
| 市值 20 億+ 選股池 | 90% | Smart 自學網明確、後期版本 |
| 早期「台灣 50 + 中型 100 = 150 檔」 | 95% | url.com.tw(2015 訪談) |
| 5 大心法內容 | **100%** | 天瓏網路書店書介(直接從書內目錄) |
| 4 大法寶內容 | **100%** | 天瓏網路書店書介 + Smart |
| 進場乖離率上限數字 | **50%**(推測) | 書中無明確,從 0050 案例 1.14% 推算 |
| W 底容差具體數字 | **30%**(推測) | 業界標準,書中無明寫 |
| 週爆量倍數 | **40%**(推測) | 書中說「爆量」未明說 |
| 趨勢線實作細節 | **20%**(推測) | 書中只說「上升趨勢線」概念 |

**結論**:核心方法(20 週 MA + 5 條件 + 4 停損 + 移動停利)**100% 確定**,參數細節(容差 / 倍數)**需要在 backtest 階段 sweep 找最佳值**。

---

## Sources

主要 web 來源(4 個獨立交叉驗證):

- [飆股女王林恩如:4 大重點不看盤賺更多 — Smart 自學網 / 商業周刊](https://smart.businessweekly.com.tw/Reading/IndepArticle.aspx?id=6004925)
- [飆股總是抱不住?4 步驟穩定投資心態 — CMoney 投資網誌](https://cmnews.com.tw/article/linenru-f0a173a5-eba4-11f0-b47e-3b44e51ac5e5)
- [嚴守「20 週均線」林恩如克服人性弱點 — url.com.tw 訪談 / 學員部落格](https://www.url.com.tw/blog/?p=38893)
- [林恩如的 20 週均線操作法 — 簡單的規則卻有不簡單的報酬 — enrumoney.net](https://enrumoney.net/%E6%9E%97%E6%81%A9%E5%A6%82-%E5%88%86%E4%BA%AB-20%E9%80%B1%E5%9D%87%E7%B7%9A%E6%93%8D%E4%BD%9C%E6%B3%95-%E7%B0%A1%E5%96%AE%E7%9A%84%E8%A6%8F%E5%89%87%EF%BC%8C%E5%8D%BB%E6%9C%89%E4%B8%8D%E7%B0%A1/)
- [均線達人林恩如:嚴格遵守 20 週均線大賺小賠 — CMoney 投資小學堂](https://www.cmoney.tw/learn/course/njulin/topic/523)
- [均線達人林恩如:309% 高績效高獲利選股法 — CMoney 投資小學堂](https://www.cmoney.tw/learn/course/njulin/topic/2742)

書籍 / 課程 / App 來源:

- [《超簡單投資法》— 博客來 / 天瓏網路書店 / Amazon Kindle / Google Play](https://www.tenlong.com.tw/products/9789865536084)
- [《超簡單趨勢波段投資法》— 博客來](https://www.books.com.tw/products/0011037400)
- [林恩如 - 強棒旺旺來 即時技術型態選股 App — Google Play](https://play.google.com/store/apps/details?id=cmoney.linenru.stock.app)
- [林恩如 - ShiFu 大師課業](https://shifu.tw/@25)
- [林恩如的 20 週均線操作法 — CMoney 理財寶 expert page](https://www.cmoney.tw/app/expert/imoney889)
