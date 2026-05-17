# 彩球雲端版

這版保留原本 `scripts/lottery_auto_update_full.py` 的抓取、合併、去重、排行與 Dashboard 產生邏輯，另外加上雲端 Web 外殼。

## 主要新增

- `cloud_app.py`：FastAPI 雲端服務。
- `/`：直接顯示 `output/lottery_final_dashboard.html`。
- `/api/web-update`：網頁「更新資料」按鈕專用，預設不用輸入 token。
- `/api/update`：背景更新，保留 token 保護，適合手動網址或程式呼叫。
- `/api/update-sync`：同步更新，適合 cron 或 GitHub Actions 呼叫。
- `/api/status`：回傳目前狀態與輸出檔路徑。
- `Dockerfile`、`Procfile`、`render.yaml`：常見雲端部署設定。
- `.github/workflows/update-cloud.yml`：定時觸發雲端更新範例。

## 本機先跑

```bash
python -m pip install -r requirements.txt
python cloud_update.py --weekly
uvicorn cloud_app:app --host 0.0.0.0 --port 8000
```

打開：

```text
http://localhost:8000
```

## Docker 跑法

```bash
docker build -t lottery-cloud .
docker run --rm -p 8000:8000 -e UPDATE_TOKEN=change-me lottery-cloud
```

## 雲端部署

把整包上傳到 GitHub，接到支援 Python Web Service 的平台即可。

建議環境變數：

```text
APP_NAME=彩球雲端版
LOTTERY_UPDATE_MODE=daily
AUTO_UPDATE_ON_START=0
PUBLIC_WEB_UPDATE=1
UPDATE_TOKEN=自行設定一組密碼
```

啟動指令：

```bash
uvicorn cloud_app:app --host 0.0.0.0 --port $PORT
```

第一次部署完成後，直接進首頁按「更新資料」即可。按鈕會呼叫 `/api/web-update?mode=weekly`，開始後會自動等待並重新整理頁面。

若你要用 token 保護的 API，也可以呼叫：

```bash
curl -X POST "https://你的網域/api/update-sync?mode=weekly" -H "x-update-token: 你的UPDATE_TOKEN"
```

## GitHub Actions 定時更新

如果使用 `.github/workflows/update-cloud.yml`，請在 GitHub repo secrets 設定：

```text
CLOUD_UPDATE_URL=https://你的網域
CLOUD_UPDATE_TOKEN=你的UPDATE_TOKEN
```

排程目前是台北時間每天 09:15 觸發一次。也可以手動在 Actions 裡按 `workflow_dispatch` 執行。

## 注意

- 如果雲端平台沒有永久磁碟，`data/lottery` 與 `output` 可能在重啟或重新部署後重置。
- 這包仍會每次更新時重新產生 `output/lottery_final_dashboard.html`，首頁不走預覽流程，直接回傳 HTML。
- 若台彩網站短暫連線失敗，錯誤會寫入 `output/lottery_error.txt`，可看 `/api/error`。
- 彩球統計僅供參考，不保證中獎。


## 本版修正

- Dashboard 頁面上方與右下角都會出現「更新資料」按鈕。
- 按「更新資料」後不需要自己打 API 網址，完成後會自動重新整理。
- 539 信心排行在雲端剛部署、資料筆數還很少時，也會先顯示「啟動排行」，不會空白。
- 清理 `nan` 空值，避免 539 多出第 6 顆或日期/特別號顯示 nan。

如果想關閉免 token 網頁更新，把 Render 的 `PUBLIC_WEB_UPDATE` 改成 `0`。
