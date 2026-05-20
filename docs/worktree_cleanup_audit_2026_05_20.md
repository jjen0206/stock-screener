# Worktree / Branch 清理 Audit — 2026-05-20

> 目的:清掉累積的 worktree 與已 merge branch,減輕 Claude desktop dispatch 卡頓。
> 執行者:claude/inspiring-goldberg-d27c1c sub-session。
> 規範:未 merge 的 branch 不刪;有 active session 的 worktree 不動;main 不碰。

---

## 結論(主公先看這段)

| 項目 | 數字 |
|------|------|
| 階段 1 孤兒 worktree 清理 | **0 個**(`git worktree prune` 沒清到 — 54 個 worktree 全部指向實際存在目錄) |
| 目前 worktree 總數 | **54**(+ main = 55) |
| 目前本地 branch 總數 | **96**(+ main = 97) |
| 🟢 可清(git 已驗證 merge) | **54 branch + 46 worktree** |
| 🟡 可清(PR 已 MERGED 但 squash,git 驗不到) | **29 branch + 1 worktree** |
| 🔴 不可動(真未 merge / active PR / 跑中 session) | **13 branch + 7 worktree** |
| 跑完腳本後預估剩下 | worktree 54→**7**、branch 96→**13** |

**已產出 `scripts/cleanup_worktrees.ps1`** — 主公本機檢視後拍板再跑,亮/sub-session 不直接刪。

### 兩個必須先知道的關鍵事實

1. **本地 `main` 落後 `origin/main` 87 個 commit**(local `7d90aca` ⊂ `origin/main` `fb0f0e6`)。
   所以 `git branch --merged main` 會大量漏判 — 本報告一律改用 **`origin/main`** 當基準。
   清理腳本第一步會 `git merge --ff-only origin/main` 把 local main 快轉同步(純 fast-forward,零衝突風險),
   否則之後的 `git branch -d` 會因 HEAD 落後而誤拒。

2. **GitHub 上多數 PR 是 squash merge**。squash 後分支 tip 不會成為 main 的祖先,
   `git branch --merged` 偵測不到 → 29 個分支「看起來未 merge、實際上工作早在 main 裡」。
   這些靠 `gh pr list` 交叉比對才抓得出來,歸 🟡,腳本用 `git branch -D`(附 PR 編號為證)。

---

## 分類報告

### 🟢 Tier 1 — git 已驗證 merge 進 origin/main(完全安全,`git branch -d` 自我保護)

分支 tip 是 `origin/main` 的祖先 → 工作 100% 在 main 裡。共 **56 個**,扣掉 2 個要保留的
(`inspiring-goldberg-d27c1c` = 本 session 自己;`quizzical-goodall-35f73d` = 有跑中 session)
→ **可清 54 branch**。其中 **46 個**有對應 worktree(腳本會先 `git worktree remove --force` 再 `git branch -d`)。

說明 worktree 目錄名 ≠ 分支名的 4 個特例(腳本已用正確路徑/分支名分開處理):

| worktree 目錄 | 實際 checkout 的分支 |
|---|---|
| `competent-engelbart-7016a7` | `claude/fix-default-settlement-fetcher-bfigtu-tpex-breach` |
| `dazzling-franklin-3d0047` | `claude/llm-company-profiles-5047` |
| `vigorous-rubin-b3f4c9` | `claude/fix-silent-fails-cbg82e` |
| `modest-tereshkova-d480cb-r3` | `claude/modest-tereshkova-d480cb` |

8 個無 worktree 的純 dangling 分支(直接 `git branch -d`):
`charming-jemison-aa2f1a`(PR #1)、`clever-goldberg-e7debf`(PR #11 已關但內容經 PR #10 進 main)、
`competent-engelbart-7016a7`、`dazzling-franklin-3d0047`、`fix-backfill-storage-quota-0635ad`、
`flamboyant-kare-1936a1`、`mystifying-moore-27aab6`、`vigorous-rubin-b3f4c9`。

> 注意:後 4 個分支與同名 worktree 目錄是「同名但不同物」— 該目錄 checkout 的是別的分支(見上表),
> 刪這幾個 dangling 分支不會動到 worktree。

46 個 worktree-分支完整清單見 `scripts/cleanup_worktrees.ps1` 第 1 段。

---

### 🟡 Tier 2 — PR 已 MERGED 但屬 squash merge(git ancestry 驗不到,實際安全)

`gh pr list` 確認這些 PR 狀態皆為 **MERGED**,工作已進 `origin/main`,只是 squash 後
分支 tip 不在 main 歷史 → `git branch -d` 會拒,腳本改用 `git branch -D`。
GitHub 上的 PR 本身保留了完整 commit 歷史,刪本地分支不會遺失任何東西。

共 **29 個分支**(28 個純 dangling + 1 個有 worktree):

```
PR#2  youthful-saha-2ce944        PR#17 zen-curran-0248ba
PR#3  admiring-sanderson-557886   PR#18 recursing-jemison-07e9d2
PR#4  priceless-montalcini-8bc173 PR#19 fervent-turing-effa11
PR#5  sharp-noyce-ed7604          PR#20 gifted-tereshkova-96b895
PR#6  great-benz-bf4a74           PR#21 blissful-chebyshev-f61362
PR#7  infallible-shockley-9d2c48  PR#22 bold-kowalevski-431ea7
PR#8  focused-keller-d68357       PR#24 suspicious-hamilton-c0dfd2
PR#9  great-sanderson-84b385      PR#25 flamboyant-haibt-72a873
PR#10 thirsty-volhard-6bbae1      PR#26 fervent-johnson-a23830
PR#12 sad-swartz-69e43d           PR#27 festive-bohr-0b95bc
PR#13 recursing-kepler-2d030f     PR#28 determined-chatelet-3ea370
PR#14 beautiful-bassi-06efdf      PR#29 distracted-spence-3395f3
PR#15 exciting-mirzakhani-c7354d  PR#30 wonderful-elbakyan-6af232
PR#16 adoring-proskuriakova-409598 PR#31 intelligent-kalam-3f1415
PR#32 swing-phase-a-features  ← 唯一有 worktree(mystifying-moore-27aab6)
```

**PR #32 `swing-phase-a-features` 特別說明**:
- `gh` 確認 PR #32 **已 MERGED**(2026-05-20 04:03,merge commit `fb0f0e6` 即現在的 `origin/main` HEAD)。
- 記憶檔 `project_swing_phase_a_2026_05_20.md` 寫「PR #32 open 等主公 review」— **已過時**,實際已合併。
- 因此 worktree `mystifying-moore-27aab6` 與分支 `swing-phase-a-features` 都可清,放在腳本第 3 段(opt-in)。
- 該 worktree 的 session「Phase A swing features 實作」目前 **未在跑**(`isRunning: false`)。

---

### 🔴 Tier 3 — 真‧未 merge,**不可動**(共 13 branch / 7 worktree 保留)

#### 跑中 session 的 worktree(絕對不碰)

| worktree | 分支 | session | 狀態 |
|---|---|---|---|
| `quizzical-goodall-35f73d` | `claude/quizzical-goodall-35f73d` | 「Phase B swing backtest」 | **isRunning: TRUE**(最後活動 05-20 04:06) |

> 此分支本身已 merge 進 origin/main,但 session 正在跑、可能產出新 commit → 保留 worktree+branch。

#### 本 session 自己

`inspiring-goldberg-d27c1c` worktree + `claude/inspiring-goldberg-d27c1c` 分支 — 不刪自己。

#### Active PR(未 merge)

| 分支 | worktree | PR | 說明 |
|---|---|---|---|
| `claude/fervent-clarke-fd3cb8` | `fervent-clarke-fd3cb8` | **#33 OPEN** | company_profiles 3 root cause,審核中 |

#### 真未 merge 分支(無 PR,git ancestry 也驗不到)— 9 個

有 worktree(4 個,session 皆未在跑,但屬未完成工作 → 保留):
`clever-panini-52c253`、`ecstatic-pike-4a3c71`、`jolly-faraday-dcab2e`、`keen-jennings-3a10ec`

純 dangling(5 個,無 worktree、無 PR、未 merge — **本次規範不刪未 merge,保留待主公另行裁示**):
`affectionate-darwin-c2575d`、`amazing-thompson-25a373`、`angry-kirch-ade18a`、
`sweet-dirac-22841a`、`watchlist-sync`(注意:此分支無 `claude/` 前綴,可能是手動建的,尤其別動)

#### PR 已關閉未採用(CLOSED,非 MERGED)— 1 個

`claude/agitated-volhard-45f8cc`(PR #23,CLOSED)— PEAD 策略的舊版,由 PR #22 取代後關閉。
技術上未 merge,依規範保留;主公若確認廢棄可日後手動 `git branch -D`。

---

## Audit 方法(可追溯)

- worktree 清單:`git worktree list --porcelain`
- merge 判定:`git branch --merged origin/main` / `--no-merged origin/main`(fetch 後,`origin/main` = `fb0f0e6`)
- PR 對照:`gh pr list --state all --limit 200`(共 33 個 PR:30 MERGED、2 CLOSED #11/#23、1 OPEN #33)
- 跑中 session 對照:`mcp__ccd_session_mgmt__list_sessions`(全部 90+ session,唯一 `isRunning: true` = quizzical-goodall)
- 每個分支都對應到實際 worktree 或實際 PR,無一憑印象。

## 殘留風險與注意事項

1. 腳本第 1 步 `git merge --ff-only origin/main` 會更新 local main — 主公先確認 main worktree 工作區乾淨。
2. `git worktree remove --force` 會丟棄該 worktree 內未提交的變更。Tier 1 的 worktree 都是已 merge 的
   完成品(多為「合 X 進 main」「驗證 X」類 session),預期無重要未提交內容;主公仍可先抽查。
3. Tier 2 的 `git branch -D` 是 force delete,但每個都附 PR 編號佐證已 MERGED,GitHub PR 保留完整歷史。
4. 腳本不碰 Tier 3 的任何 branch / worktree。
5. 跑腳本前建議主公再次確認 `quizzical-goodall-35f73d` 的「Phase B swing backtest」session 已結束。
