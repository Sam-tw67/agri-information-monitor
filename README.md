# 農業資訊監控排程任務

週一到週六台灣時間 07:37 由 GitHub Actions 執行，本機關機後仍會運作。任務包含兩部分：

1. 擷取一般農業資訊來源指定日期區間的文章，套用來源專屬標題篩選與去重後，將標題與原始 URL 寄至 Email。
2. 掃描 ACRI 農藥問答集全部分頁，以六碼 `編號` 比對專用 Notion database，只新增尚不存在的問答，並將本次新增項目的 Notion 連結放入同一封 Email。

一般監控結果不再寫入 Notion。程式不保存文章正文、摘要、圖片、作者或標籤；一般來源只處理標題、原始 URL 及日期。

## 監控來源

一般來源集中在 `sources.yml`，修改來源不需改 Python：

```yaml
sources:
  - website: 上下游新聞
    url: https://www.newsmarket.com.tw/
    output_heading: 上下游
    enabled: true
    include_title_patterns: []
```

- `url`：實際監控位置。
- `output_heading`：Email 中的來源標題。舊的 `notion_heading` 仍可讀取，但新設定應使用 `output_heading`。
- `enabled`：只有布林值 `true` 才會執行。
- `include_title_patterns`：選填的標題正規表示式白名單。
- `exclude_title_patterns`：選填的標題正規表示式排除清單。
- `show_no_update`：選填，預設為 `true`；該來源當日 0 筆時會彙整到「以下監控項目無新增項目來源」段落。

花蓮區農業改良場使用本場新聞、最新消息與近期活動三個官方 RSS。其他來源與所有排除規則記錄在 `sources.yml`。

農藥資訊服務網只收錄指定的農藥使用方法公告與「更新通知」；代噴人員與空中施作代噴人員公告不納入。衛福部食藥署只查詢標題含「農藥殘留容許量標準」的最新一筆公告。

## ACRI Notion 同步

- 來源：`https://mbox.acri.gov.tw/TA02.asp`
- Database ID：`e2c49f31c2424e9db4b37cb662e079ff`
- 必要欄位：`問題`（title）、`日期`（date）、`編號`（text）、`類別`（select）

ACRI 每次動態讀取最後頁碼並掃描全站，不受每日監控日期區間限制。同一編號已存在時不重複新增；新類別會自動加入 Notion select 選項。

## GitHub Secrets

在 Repository 的 **Settings → Secrets and variables → Actions** 建立：

- `NOTION_TOKEN`：已連接 ACRI 專用 database 的 Notion integration token。
- `SMTP_USERNAME`：Gmail 寄件帳號，例如 `david40569@gmail.com`。
- `SMTP_APP_PASSWORD`：Google 產生的 16 位應用程式密碼，不是 Gmail 登入密碼。
- `EMAIL_TO`：收件地址；多個收件人可以逗號或分號分隔。

工作流程固定使用 `smtp.gmail.com:587` 與 STARTTLS。機密值不得寫入程式、`.env.example` 或 log。

## 執行與排程

需求為 Python 3.11 以上（GitHub Actions 使用 3.12）：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m pytest -q
```

Dry-run 會讀取來源與 ACRI Notion 既有編號，但不新增 ACRI、不寄 Email：

```powershell
python -m agri_monitor --dry-run
python -m agri_monitor --dry-run --run-date 2026-07-13
```

工作流程使用：

```yaml
schedule:
  - cron: "37 23 * * 0-5"
```

這是週日到週五 23:37 UTC，即台灣週一到週六 07:37。週一擷取上週六、週日資料；週二至週六擷取前一天；週日不自動執行。

## Email 與失敗處理

Email 主旨包含監控日期、一般文章數與 ACRI 新增數。內文有新資料時依來源列出可點擊的標題；沒有新資料時仍寄信，並合併顯示：

```text
以下監控項目無新增項目來源：花蓮改良場、台中改良場…。
```

單一來源抓取失敗會列於 Email，其他成功來源仍正常寄出。ACRI 部分同步失敗時，已成功新增的 Notion 項目會保留，錯誤報告寄出後 GitHub Actions 會標記失敗，下次再依編號續跑。
