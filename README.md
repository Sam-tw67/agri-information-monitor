# 農業資訊監控排程任務

每週一台灣時間 08:00 讀取 repository 內的 `sources.yml`，擷取前一週完整七個曆日內發布的文章，並依完整標題在 Notion database 建立或更新一筆 page。只需設定一個機密：`NOTION_TOKEN`。

程式只處理文章標題、原始 URL 與內部日期篩選所需的發布日期；不保存或寫入正文、摘要、圖片、作者或標籤。來源優先使用 RSS/Atom，找不到 feed 時才解析 HTML 文章列表與文章頁 metadata。

## 來源設定

三個來源集中在 `sources.yml`，不散落於爬蟲程式中：

```yaml
sources:
  - website: 上下游新聞
    url: https://www.newsmarket.com.tw/
    notion_heading: 上下游
    enabled: true
```

- `url`：實際監控位置。
- `notion_heading`：Notion page 內的二級標題。
- `website`：方便人員辨識，不影響輸出。
- `enabled`：只有布林值 `true` 才會執行。

修改來源只需編輯此檔，不需改 Python。設定檔不存在、格式錯誤、URL 無效或沒有啟用來源時，任務會以非零狀態結束。

## 前置需求

- Python 3.11 以上（GitHub Actions 使用 3.12）。
- Notion integration token；integration 必須獲授權存取目標 database。
- Notion database 必須只包含一個 data source，且已有 `Name`（title）及 `Status`（status 或 select）欄位，`Status` 有 `Unread` 選項。程式使用 Notion API `2026-03-11`，會由 database ID 自動取得 data source ID，不會變更 schema。

## 本機首次設定

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

編輯 `.env`，只需填入：

```dotenv
NOTION_TOKEN=你的_Notion_integration_token
```

`.env` 已被 `.gitignore` 排除，禁止提交 token。

執行測試：

```powershell
python -m pytest -q
```

手動 dry-run（實際檢查三個來源，但不連線或寫入 Notion）：

```powershell
python -m agri_monitor --dry-run
```

指定執行日重現某週：

```powershell
python -m agri_monitor --dry-run --run-date 2026-06-22
```

正式執行：

```powershell
python -m agri_monitor
```

任一未處理錯誤、來源設定檔無效，或全部來源擷取失敗時，程式以狀態碼 1 結束。部分來源失敗時會留下 ERROR log，並以成功來源的結果更新該週 page。所有來源成功但沒有區間內文章時，page 內容為「本週無符合日期區間的新文章。」

## GitHub Actions 設定

在 repository 的 **Settings → Secrets and variables → Actions** 新增一個 secret：

- `NOTION_TOKEN`：Notion integration token。

接著確認 Actions 已啟用即可。排程使用 `0 0 * * 1`，即每週一 00:00 UTC；台灣全年為 UTC+8，因此實際在 `Asia/Taipei` 每週一 08:00 執行。也可在 Actions 頁面使用 `workflow_dispatch` 手動執行並勾選 dry-run。

## 寫入與重跑行為

執行日往前 7 天為 `start_date`，往前 1 天為 `end_date`，首尾皆納入。例如 2026-06-22 執行會處理 2026-06-15 至 2026-06-21，page 標題固定為：

```text
農業資訊監控排程任務 (上次:2026-06-15/ 本次:2026-06-21)
```

寫入前會以這個完整標題查詢 `Name`。不存在時建立 page 並將 `Status` 設為 `Unread`；存在時只替換 page 內容，不更新任何 properties，因此保留原 Status。若資料庫意外已有兩筆同名 page，任務會失敗，避免任意覆寫。

同次執行以 canonical URL（文章頁可取得時）或移除追蹤參數、fragment 並正規化後的 URL 跨來源去重。無可靠發布日期的文章會記錄 warning 後略過，不推測日期。
