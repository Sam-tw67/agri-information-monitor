# 農業資訊監控排程任務

週二到週六台灣時間 08:00 由 GitHub Actions 執行，關機後仍會運作。任務包含兩部分：

1. 擷取一般農業資訊來源前一個曆日的文章，建立或更新一筆「農業資訊每日監控」頁面。
2. 掃描 ACRI 農藥問答集全部分頁，以六碼 `編號` 比對專用 Notion database，只新增尚不存在的問答，並把本次新增項目連結放進同一筆監控頁面。

程式不保存文章正文、摘要、圖片、作者或標籤。一般來源只處理標題、原始 URL 及日期；ACRI 只處理編號、類別、日期、問題標題及原始明細 URL。

## 監控來源

一般來源集中在 `sources.yml`，修改來源不需改 Python：

```yaml
sources:
  - website: 上下游新聞
    url: https://www.newsmarket.com.tw/
    notion_heading: 上下游
    enabled: true
    include_title_patterns: []
```

- `url`：實際監控位置。
- `notion_heading`：監控頁面中的二級標題。
- `enabled`：只有布林值 `true` 才會執行。
- `include_title_patterns`：選填的標題正規表示式白名單。
- `show_no_update`：選填，預設為 `true`；該來源當日 0 筆時會彙整到「以下監控項目無新增項目來源」段落。

花蓮區農業改良場目前使用三個官方 RSS 來源：

- `https://www.hdares.gov.tw/api.php?func=news&format=rss`：本場新聞
- `https://www.hdares.gov.tw/api.php?func=hotnews&format=rss`：最新消息
- `https://www.hdares.gov.tw/api.php?func=activity&format=rss`：近期活動

農藥資訊服務網目前只收錄：公告修正農藥使用方法、公告農藥使用方法、預先通知公告農藥使用方法，以及標題以「更新通知」結尾的公告。代噴人員與空中施作代噴人員公告不會納入。

所有一般來源當日 0 筆時，監控頁面不會逐一建立空段落，會彙整為一行「以下監控項目無新增項目來源：...」。

衛福部食藥署來源只查詢標題含「農藥殘留容許量標準」的公告，並只保留發布日期最新的一筆。最新版若發布於本次日期區間內，監控頁面在「衛福部食藥署」段落列出可點擊標題；不在區間內則併入無新增來源彙整。目前已讀基準為 `id=31518`（2026-04-21），不會因新增監控設定而回填為當日更新。

ACRI 來源由 `ACRI_SOURCE_URL` 指定，預設為 `https://mbox.acri.gov.tw/TA02.asp`。程式每次動態讀取最後頁碼並掃描全站，不採固定頁數，也不受每日監控日期區間限制。

## Notion 目標

監控頁面 database：

- Database ID：`34435800664b80e0917dcd7c0535732c`
- 必要欄位：`Name`（title）、`Status`（status 或 select，含 `Unread`）

ACRI 專用 database：

- Database ID：`e2c49f31c2424e9db4b37cb662e079ff`
- 必要欄位：`問題`（title）、`日期`（date）、`編號`（text）、`類別`（select）

ACRI 的 `問題` 標題連回原始 ACRI 明細頁；監控頁面中的 ACRI 標題則連到新建的 Notion 項目。若來源出現新的非空白類別，程式會保留既有選項並自動加入新選項。來源類別空白時仍會新增該筆，Notion 類別維持空白。

同一 ACRI 編號已存在時不重複新增；若 database 原本已有重複編號，任務保留原資料、略過新增，並在 log 與監控頁面警告。首次正式執行會回填所有缺少的歷史編號。

## 本機首次設定

需求為 Python 3.11 以上（GitHub Actions 使用 3.12）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

編輯 `.env`，只需填入機密：

```dotenv
NOTION_TOKEN=你的_Notion_Internal_Integration_Access_Token
```

同一個 integration 必須連接監控頁面與 ACRI 兩個 database。其他 ID 與 URL 已在 `.env.example` 提供。`.env` 已被 `.gitignore` 排除，不得提交 token、cookie 或憑證。

執行測試：

```powershell
python -m pytest -q
```

Dry-run 會實際讀取來源及 Notion 既有編號，但不建立頁面、不修改欄位，也不更新監控頁面：

```powershell
python -m agri_monitor --dry-run
python -m agri_monitor --dry-run --run-date 2026-06-22
```

正式執行：

```powershell
python -m agri_monitor
```

## GitHub Actions

Repository 的 **Settings → Secrets and variables → Actions** 只需一個 secret：

- `NOTION_TOKEN`：已連接兩個目標 database 的 Notion integration access token。

工作流程 `.github/workflows/agri-monitor.yml` 使用：

```yaml
schedule:
  - cron: "0 0 * * 2-6"
```

這是週二到週六 00:00 UTC；台灣全年 UTC+8，因此實際為 `Asia/Taipei` 週二到週六 08:00。週二抓週一資料，週六抓週五資料；週日與週一不自動啟動，因官方資訊與上下游皆依停更原則處理。GitHub 伺服器負責執行，本機可關機。也可在 Actions 頁面手動 Run workflow，並選擇 dry-run。

## 日報與重跑規則

執行日往前 1 天為 `start_date` 與 `end_date`，首尾皆納入。例如 2026-06-22 執行時，標題固定為：

```text
農業資訊每日監控 (日期:2026-06-21)
```

寫入前依完整標題查詢：不存在時建立並將 `Status` 設為 `Unread`；存在時只替換內容，保留原 Status。同一次一般文章依 canonical URL 或正規化 URL 去重；沒有可靠日期的文章會記錄 warning 並略過。

一般來源與 ACRI 同步成功但沒有新項目時，監控頁面會彙整列出無新增來源。首次回填即使項目很多，也會完整列出所有本次新增標題。

若 ACRI 在部分新增後失敗，成功項目會保留並寫入監控頁面，錯誤原因也會寫入監控頁面，工作流程最後以非零狀態結束；下次執行會依編號自動續跑。若全部一般來源與 ACRI 都失敗，則不建立空白監控頁面。
