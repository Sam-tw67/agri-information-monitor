# 農業資訊監控排程任務

週一到週六台灣時間 07:37 由 GitHub Actions 執行，本機關機後仍會運作。任務包含兩部分：

1. 擷取一般農業資訊來源最近 30 日的文章，套用來源專屬標題篩選與已寄送紀錄去重後，將新發現的標題與原始 URL 寄至 Email；來源恢復時會自動回補先前漏寄項目。
2. 掃描 ACRI 農藥問答集全部分頁，以來源識別碼及問題標題比對專用 Notion database，只新增尚不存在的問答，並將本次新增項目的 Notion 連結放入同一封 Email。

一般監控結果不再寫入 Notion。程式不保存文章正文、摘要、圖片、作者或標籤；一般來源只處理標題、原始 URL 及日期，並將已寄送項目的標題、URL、來源、發布日與寄送日保存於 Claude 亦可直接讀取的 `.state/sent_articles.json`。

## 監控來源

一般來源集中在 `sources.yml`，修改來源不需改 Python：

```yaml
sources:
  - website: 上下游新聞
    url: 'https://www.newsmarket.com.tw/wp-json/wp/v2/posts?per_page=100&_fields=link,title,date'
    fallback_url: 'https://news.google.com/rss/search?q=site%3Anewsmarket.com.tw&hl=zh-TW&gl=TW&ceid=TW%3Azh-Hant'
    output_heading: 上下游
    enabled: true
    parser: wordpress_posts_json
```

- `url`：實際監控位置。
- `output_heading`：Email 中的來源標題。舊的 `notion_heading` 仍可讀取，但新設定應使用 `output_heading`。
- `enabled`：只有布林值 `true` 才會執行。
- `include_title_patterns`：選填的標題正規表示式白名單。
- `exclude_title_patterns`：選填的標題正規表示式排除清單。
- `fallback_url`：選填的提示性 RSS 備援；主來源連線或解析失敗時才讀取，但不視為完整來源。
- `show_no_update`：選填，預設為 `true`；該來源當日 0 筆時會彙整到「以下監控項目無新增項目來源」段落。

花蓮區農業改良場使用本場新聞、最新消息與近期活動三個官方 RSS；所有一般來源都保留官方網站為第一順位。另以農業部「農業知識入口網－農情關注站」的分頁清單作為官方安全網，會回讀最多 12 頁，但只保留目前監控之花蓮、桃園、苗栗、臺中、高雄、臺南及臺東農改場發布的消息。因此即使 GitHub 主機暫時連不上個別農改場，仍可補收已被農業部彙整的重要農情消息，且不會擴張到其他機關。相同標題的原站與官方鏡像若發布日期相差一天，視為同一則，避免重複寄送。

主來源連線或解析失敗時，Google News 只作為可能不完整的提示性備援，Email 會列於「來源降級」而不再宣稱該來源沒有更新。不同來源的首次抓取最多同時執行 6 個，避免單一區域網路故障讓每個來源依序等待逾時。每次執行只讀取各來源的列表／RSS；已有可靠發布日期的項目不會再讀取文章頁，只有缺日期的項目才補抓中繼資料。所有標題白名單、排除規則與去重仍會套用在備援結果。

一般來源每次回溯最近 30 日，再以 `.state/sent_articles.json` 排除已寄送文章。Email 成功後才更新狀態檔；GitHub Actions 會自動提交該檔案到 `main`，因此某來源短暫中斷後，只要在回溯期間恢復且文章仍存在於列表或 RSS，就會在下一次執行補寄。狀態紀錄保留 180 日。首次啟用此機制時，可能寄出最近 30 日內尚未建立寄送紀錄的文章。

單一來源失敗時，系統會在寄送日報前等待 5 分鐘補抓一次；若所有來源都因網路連線錯誤而失敗，會先以 10 分鐘間隔全域重試，最多 3 次，避免 GitHub Actions 的短暫網路異常造成整批錯誤通知。

農藥資訊服務網只收錄指定的農藥使用方法公告與「更新通知」；代噴人員與空中施作代噴人員公告不納入。衛福部食藥署只查詢標題含「農藥殘留容許量標準」的最新一筆公告。

## ACRI Notion 同步

- 來源：`https://mboxap.acri.gov.tw/POP30/FAQ/FAQ/GetFaqList`
- Database ID：`e2c49f31c2424e9db4b37cb662e079ff`
- 必要欄位：`問題`（title）、`日期`（date）、`編號`（text）、`類別`（select）

ACRI 每次依新版公開 FAQ API 回報的總筆數掃描全部分頁，不受每日監控日期區間限制。舊資料使用六碼編號，新版資料使用 UUID；識別碼或問題標題已存在時不重複新增，新類別會自動加入 Notion select 選項。連線逾時、HTTP 408／429 或來源端 5xx 錯誤會等待 30 秒、120 秒後重試；永久性 4xx 錯誤則立即回報。

## GitHub Secrets

在 Repository 的 **Settings → Secrets and variables → Actions** 建立：

- `NOTION_TOKEN`：已連接 ACRI 專用 database 的 Notion integration token。
- `SMTP_USERNAME`：Gmail 寄件帳號，例如 `david40569@gmail.com`。
- `SMTP_APP_PASSWORD`：Google 產生的 16 位應用程式密碼，不是 Gmail 登入密碼。
- `EMAIL_TO`：收件地址；多個收件人可以逗號或分號分隔。

一般設定另包含 `ARTICLE_LOOKBACK_DAYS=30` 與 `ARTICLE_STATE_FILE=.state/sent_articles.json`。工作流程需要 `contents: write`，僅用於 Email 成功後提交寄送狀態檔。

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
