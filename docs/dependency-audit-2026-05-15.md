# Dependency Audit — 2026-05-15

> Round 1 即時清理維護:requirements.txt 加版本範圍 lock、拆出 dev 用套件、補上漏寫的直接依賴。

## 摘要

| 項目 | 數量 |
|---|---|
| 維持 (production) | 14 |
| 新增 (production) | 1 (`requests`) |
| 移到 dev | 2 (`pytest`, `ruff`) |
| 真正移除 | 0 |
| 加版本上界 (`<X.0.0`) | 全部 |

## 變更清單

### 新增到 `requirements.txt`
- **`requests>=2.31.0,<3.0.0`** — 之前是透過其他套件帶進來的 transitive dep,但 `src/data_fetcher.py`、`src/discord_notifier.py`、`src/financial_fetcher_free.py`、`src/github_sync.py`、`src/notifier.py`、多個 `scripts/*.py` 都直接 `import requests`。應該宣告為直接依賴,避免上游移除時意外壞掉。

### 移到 `requirements-dev.txt`
- **`pytest>=8.0.0,<10.0.0`** — 只在測試/CI 用,不該進 production 影像。
- **`ruff>=0.4.0,<1.0.0`** — 同上,純 lint/format 工具。

### 加上版本上界(避免主版升級破壞)
所有套件改成 `>=X.Y.0,<NEXT_MAJOR.0.0` 範圍格式:

| Package | 舊 | 新 | 當前安裝 |
|---|---|---|---|
| streamlit | `>=1.34.0` | `>=1.34.0,<2.0.0` | 1.57.0 |
| finmind | `>=1.9.0` | `>=1.9.0,<2.0.0` | 1.9.8 |
| yfinance | `>=0.2.40` | `>=0.2.40,<2.0.0` | 1.3.0 |
| tqdm | `>=4.66.0` | `>=4.66.0,<5.0.0` | 4.67.3 |
| httpx | `>=0.27.0` | `>=0.27.0,<1.0.0` | 0.28.1 |
| beautifulsoup4 | `>=4.12.0` | `>=4.12.0,<5.0.0` | 4.14.3 |
| pandas | `>=2.2.0` | `>=2.2.0,<3.0.0` | 2.3.3 |
| numpy | `>=1.26.0` | `>=1.26.0,<3.0.0` | 2.4.4 |
| plotly | `>=5.20.0` | `>=5.20.0,<7.0.0` | 6.7.0 |
| scikit-learn | `>=1.3.0` | `>=1.3.0,<2.0.0` | 1.8.0 |
| joblib | `>=1.3.0` | `>=1.3.0,<2.0.0` | 1.5.3 |
| shap | `>=0.44.0` | `>=0.44.0,<1.0.0` | 0.51.0 |
| vectorbt | `>=1.0.0` | `>=1.0.0,<2.0.0` | 1.0.0 |
| google-generativeai | `>=0.7.0` | `>=0.7.0,<1.0.0` | 0.8.6 |
| python-dotenv | `>=1.0.0` | `>=1.0.0,<2.0.0` | 1.2.2 |

**設計理念**:
- `>=X.Y.0`(下界):確保用到的功能存在
- `<NEXT_MAJOR.0.0`(上界):允許 minor / patch 升級(bug fix、功能擴充),但封住主版升級(可能 breaking)
- **不 pin 死**:主公以後升級單一套件時,不會卡住其他套件的版本

## 沒移除的「未直接 import」套件

| Package | 為何保留 |
|---|---|
| `tqdm` | finmind 1.9.x 漏寫的 transitive dep,移掉 finmind 會炸。requirements.txt 的註解已寫明。 |

## 已安裝但未在 requirements.txt 的套件

`pip list` 列出但兩份 requirements 都沒寫的套件,推測是 transitive dep(例如 `aiohttp`、`pydantic`、`cryptography`、`xgboost` 等)。**沒動**,因為:
- 不是直接 import 的,不該被 pin
- transitive dep 的版本由 pip resolver 處理

但有一個值得注意:**`xgboost==3.2.0`** 安裝了但完全沒被 import。可能是:
- 之前實驗 ML 模型時裝的,後來改用 sklearn `RandomForestClassifier`
- 或是 SHAP 的 transitive dep

主公可日後在 `pip uninstall xgboost` 試一次,看 SHAP 還能不能 import — 若可以代表純殘留,可以拔。

## 驗證指令

```bash
# 重裝 production 依賴
pip install -r requirements.txt

# 重裝 production + dev 依賴(本機開發)
pip install -r requirements.txt -r requirements-dev.txt

# 確認 import 沒壞
python -c "import streamlit, pandas, numpy, plotly, sklearn, shap, vectorbt, finmind, yfinance, requests, httpx, bs4, joblib, google.generativeai, dotenv; print('OK')"

# 跑測試
pytest -x
```

## 後續建議(下一輪)

1. **遷移 `google-generativeai` → `google-genai`** — Google 已將前者標 deprecated。
2. **拔 `xgboost`** — 確認沒被間接需要後可移除(節省 ~150MB)。
3. 考慮改用 `pyproject.toml` + `uv` 管理依賴(`CLAUDE.md` 也建議過 uv)。
