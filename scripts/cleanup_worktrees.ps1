# =============================================================================
# cleanup_worktrees.ps1  —  worktree / branch 清理腳本
# 產生於 2026-05-20,依據 docs/worktree_cleanup_audit_2026_05_20.md
#
# 用途:清掉已 merge 的 worktree 與 branch,減輕 Claude desktop dispatch 卡頓。
# 由主公(諸葛亮）本機檢視 audit 報告後手動執行。
#
# 安全保證:
#   - 只清「已 merge / PR 已 MERGED」的分支,絕不碰未 merge 的工作。
#   - 不碰 main、不碰本 session、不碰跑中的 quizzical-goodall-35f73d。
#   - Tier 1 用 git branch -d(git 自我保護,未 merge 會自動拒刪)。
#   - Tier 2 用 git branch -D,但每個都有對應的 MERGED PR 為證,且獨立確認門檻。
#
# 跑之前請先確認:
#   1. 主 worktree(D:\Claude-workspace\projects\stock-screener）工作區乾淨。
#   2. 「Phase B swing backtest」(quizzical-goodall-35f73d）session 已結束。
# =============================================================================

$ErrorActionPreference = 'Continue'
$MainRepo  = 'D:\Claude-workspace\projects\stock-screener'
$WtBase    = 'D:\Claude-workspace\projects\stock-screener\.claude\worktrees'

Write-Host '=== worktree / branch 清理腳本 ===' -ForegroundColor Cyan
Write-Host '將清理:46 worktree + 54 branch(Tier 1）;Tier 2 另行確認。' -ForegroundColor Yellow
$ok = Read-Host '確定執行?輸入 yes 繼續'
if ($ok -ne 'yes') { Write-Host '已取消。' -ForegroundColor Red; exit 1 }

Set-Location $MainRepo

# -----------------------------------------------------------------------------
# 步驟 0:同步 local main(目前落後 origin/main 87 個 commit)
#   純 fast-forward(main ⊂ origin/main 已驗證),零衝突風險。
#   不做這步,後面 git branch -d 會因 HEAD 落後而誤拒。
# -----------------------------------------------------------------------------
Write-Host "`n[0] fetch + 快轉 local main ..." -ForegroundColor Cyan
git fetch origin --prune
git merge --ff-only origin/main
if ($LASTEXITCODE -ne 0) {
    Write-Host '!! main 快轉失敗(工作區可能不乾淨）。請手動處理後再跑。' -ForegroundColor Red
    exit 1
}

# -----------------------------------------------------------------------------
# 步驟 1:移除 Tier 1 worktree（46 個，分支已驗證 merge 進 origin/main）
# -----------------------------------------------------------------------------
$Tier1Worktrees = @(
    'adoring-goldberg-95a9f3', 'adoring-leavitt-94e53b', 'affectionate-fermi-44fbb0',
    'awesome-tereshkova-b8ace4', 'blissful-dhawan-ad2c8c', 'bold-pike-90dd27',
    'charming-williamson-121337', 'competent-engelbart-7016a7', 'competent-lehmann-6a665b',
    'competent-shirley-9c6030', 'cranky-turing-a0eb8b', 'dazzling-franklin-3d0047',
    'determined-liskov-08a574', 'ecstatic-moser-5a3797', 'elegant-mcclintock-c282ba',
    'elegant-snyder-e7c048', 'eloquent-vaughan-bb319e', 'exciting-mclaren-077e90',
    'friendly-lamarr-b4e8f1', 'frosty-bardeen-6df2ff', 'frosty-hellman-161296',
    'frosty-mcnulty-18dd81', 'gallant-lovelace-28db3d', 'goofy-murdock-24c508',
    'goofy-newton-5bbe68', 'happy-khorana-061cff', 'infallible-black-981086',
    'inspiring-raman-f0a3d7', 'interesting-almeida-cdd152', 'jolly-lewin-d47daa',
    'kind-swartz-d14442', 'modest-tereshkova-d480cb-r3', 'nervous-brahmagupta-003012',
    'nostalgic-beaver-79f2aa', 'nostalgic-black-30ed24', 'nostalgic-lamarr-95ed63',
    'romantic-elbakyan-d6d9b9', 'sad-ardinghelli-6c57a6', 'serene-gould-0fe34c',
    'sleepy-goodall-7cb10f', 'strange-wiles-36d653', 'stupefied-solomon-2ed85b',
    'suspicious-almeida-cde55d', 'thirsty-chandrasekhar-621591', 'vigorous-hugle-f3a45b',
    'vigorous-rubin-b3f4c9'
)
Write-Host "`n[1] 移除 $($Tier1Worktrees.Count) 個 worktree ..." -ForegroundColor Cyan
foreach ($w in $Tier1Worktrees) {
    $path = Join-Path $WtBase $w
    git worktree remove $path --force
    if ($LASTEXITCODE -eq 0) { Write-Host "  removed  $w" -ForegroundColor Green }
    else                     { Write-Host "  SKIP/ERR $w" -ForegroundColor Yellow }
}
git worktree prune -v

# -----------------------------------------------------------------------------
# 步驟 2:刪除 Tier 1 branch（54 個，git 已驗證 merge；-d 會自我保護）
# -----------------------------------------------------------------------------
$Tier1Branches = @(
    # --- 46 個原本有 worktree 的分支 ---
    'claude/adoring-goldberg-95a9f3', 'claude/adoring-leavitt-94e53b',
    'claude/affectionate-fermi-44fbb0', 'claude/awesome-tereshkova-b8ace4',
    'claude/blissful-dhawan-ad2c8c', 'claude/bold-pike-90dd27',
    'claude/charming-williamson-121337',
    'claude/fix-default-settlement-fetcher-bfigtu-tpex-breach',  # worktree: competent-engelbart-7016a7
    'claude/competent-lehmann-6a665b', 'claude/competent-shirley-9c6030',
    'claude/cranky-turing-a0eb8b',
    'claude/llm-company-profiles-5047',                          # worktree: dazzling-franklin-3d0047
    'claude/determined-liskov-08a574', 'claude/ecstatic-moser-5a3797',
    'claude/elegant-mcclintock-c282ba', 'claude/elegant-snyder-e7c048',
    'claude/eloquent-vaughan-bb319e', 'claude/exciting-mclaren-077e90',
    'claude/friendly-lamarr-b4e8f1', 'claude/frosty-bardeen-6df2ff',
    'claude/frosty-hellman-161296', 'claude/frosty-mcnulty-18dd81',
    'claude/gallant-lovelace-28db3d', 'claude/goofy-murdock-24c508',
    'claude/goofy-newton-5bbe68', 'claude/happy-khorana-061cff',
    'claude/infallible-black-981086', 'claude/inspiring-raman-f0a3d7',
    'claude/interesting-almeida-cdd152', 'claude/jolly-lewin-d47daa',
    'claude/kind-swartz-d14442',
    'claude/modest-tereshkova-d480cb',                           # worktree: modest-tereshkova-d480cb-r3
    'claude/nervous-brahmagupta-003012', 'claude/nostalgic-beaver-79f2aa',
    'claude/nostalgic-black-30ed24', 'claude/nostalgic-lamarr-95ed63',
    'claude/romantic-elbakyan-d6d9b9', 'claude/sad-ardinghelli-6c57a6',
    'claude/serene-gould-0fe34c', 'claude/sleepy-goodall-7cb10f',
    'claude/strange-wiles-36d653', 'claude/stupefied-solomon-2ed85b',
    'claude/suspicious-almeida-cde55d', 'claude/thirsty-chandrasekhar-621591',
    'claude/vigorous-hugle-f3a45b',
    'claude/fix-silent-fails-cbg82e',                            # worktree: vigorous-rubin-b3f4c9
    # --- 8 個無 worktree 的 dangling 分支 ---
    'claude/charming-jemison-aa2f1a',          # PR #1
    'claude/clever-goldberg-e7debf',           # PR #11 closed,內容經 PR #10 進 main
    'claude/competent-engelbart-7016a7',
    'claude/dazzling-franklin-3d0047',
    'claude/fix-backfill-storage-quota-0635ad',
    'claude/flamboyant-kare-1936a1',
    'claude/mystifying-moore-27aab6',
    'claude/vigorous-rubin-b3f4c9'
)
Write-Host "`n[2] 刪除 $($Tier1Branches.Count) 個 Tier 1 branch ..." -ForegroundColor Cyan
foreach ($b in $Tier1Branches) {
    git branch -d $b
    if ($LASTEXITCODE -eq 0) { Write-Host "  deleted  $b" -ForegroundColor Green }
    else                     { Write-Host "  SKIP/ERR $b（-d 拒刪 = 未 merge,保留）" -ForegroundColor Yellow }
}

Write-Host "`n=== Tier 1 完成 ===" -ForegroundColor Cyan
Write-Host '剩餘 worktree:' -ForegroundColor Cyan
git worktree list

# =============================================================================
# 步驟 3:Tier 2 —— PR 已 MERGED 但屬 squash merge 的分支（git -d 偵測不到）
#   這些工作確實已在 main 裡(gh 確認 PR 狀態 MERGED）,GitHub PR 保留完整歷史。
#   需用 git branch -D(force）。獨立確認門檻。
# =============================================================================
Write-Host "`n=== Tier 2:squash-merged 分支(需 -D force delete）===" -ForegroundColor Yellow
Write-Host '共 29 個分支,每個都對應一個 MERGED PR(見 audit 報告）。' -ForegroundColor Yellow
$ok2 = Read-Host '一併清理 Tier 2?輸入 yes 繼續,其他則跳過'
if ($ok2 -eq 'yes') {
    # 先移除 PR #32 的 worktree(swing-phase-a-features / mystifying-moore-27aab6)
    Write-Host '[3a] 移除 PR #32 worktree mystifying-moore-27aab6 ...' -ForegroundColor Cyan
    git worktree remove (Join-Path $WtBase 'mystifying-moore-27aab6') --force
    git worktree prune -v

    $Tier2Branches = @(
        'claude/youthful-saha-2ce944',          # PR #2
        'claude/admiring-sanderson-557886',     # PR #3
        'claude/priceless-montalcini-8bc173',   # PR #4
        'claude/sharp-noyce-ed7604',            # PR #5
        'claude/great-benz-bf4a74',             # PR #6
        'claude/infallible-shockley-9d2c48',    # PR #7
        'claude/focused-keller-d68357',         # PR #8
        'claude/great-sanderson-84b385',        # PR #9
        'claude/thirsty-volhard-6bbae1',        # PR #10
        'claude/sad-swartz-69e43d',             # PR #12
        'claude/recursing-kepler-2d030f',       # PR #13
        'claude/beautiful-bassi-06efdf',        # PR #14
        'claude/exciting-mirzakhani-c7354d',    # PR #15
        'claude/adoring-proskuriakova-409598',  # PR #16
        'claude/zen-curran-0248ba',             # PR #17
        'claude/recursing-jemison-07e9d2',      # PR #18
        'claude/fervent-turing-effa11',         # PR #19
        'claude/gifted-tereshkova-96b895',      # PR #20
        'claude/blissful-chebyshev-f61362',     # PR #21
        'claude/bold-kowalevski-431ea7',        # PR #22
        'claude/suspicious-hamilton-c0dfd2',    # PR #24
        'claude/flamboyant-haibt-72a873',       # PR #25
        'claude/fervent-johnson-a23830',        # PR #26
        'claude/festive-bohr-0b95bc',           # PR #27
        'claude/determined-chatelet-3ea370',    # PR #28
        'claude/distracted-spence-3395f3',      # PR #29
        'claude/wonderful-elbakyan-6af232',     # PR #30
        'claude/intelligent-kalam-3f1415',      # PR #31
        'claude/swing-phase-a-features'         # PR #32
    )
    Write-Host "[3b] 刪除 $($Tier2Branches.Count) 個 Tier 2 branch ..." -ForegroundColor Cyan
    foreach ($b in $Tier2Branches) {
        git branch -D $b
        if ($LASTEXITCODE -eq 0) { Write-Host "  deleted  $b" -ForegroundColor Green }
        else                     { Write-Host "  ERR      $b" -ForegroundColor Red }
    }
} else {
    Write-Host 'Tier 2 已跳過。' -ForegroundColor Yellow
}

# =============================================================================
# 完成
# =============================================================================
Write-Host "`n=== 全部完成 ===" -ForegroundColor Cyan
Write-Host "最終 worktree 數:" -NoNewline; (git worktree list | Measure-Object).Count
Write-Host "最終 branch 數  :" -NoNewline; (git branch | Measure-Object).Count
Write-Host ''
Write-Host '保留未動(Tier 3,真未 merge / active / 跑中):' -ForegroundColor Yellow
Write-Host '  worktree: inspiring-goldberg-d27c1c(本 session）, quizzical-goodall-35f73d(跑中）,'
Write-Host '            fervent-clarke-fd3cb8(PR #33 OPEN）, clever-panini-52c253, ecstatic-pike-4a3c71,'
Write-Host '            jolly-faraday-dcab2e, keen-jennings-3a10ec'
Write-Host '  其他未 merge 分支(無 worktree）需主公另行裁示後再清。'
