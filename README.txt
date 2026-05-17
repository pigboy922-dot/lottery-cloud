彩球 ONE CLICK SSL + 分區/特別號修正版

一鍵跑：
  RUN_彩球_一鍵更新.cmd

備用：
  RUN_LOTTERY_ONE_CLICK.cmd

功能：
1. CMD 不會自動關閉。
2. 台彩 SSL 憑證錯誤會自動 fallback。
3. 每次跑會抓最新開獎 + 補歷史。
4. 自動更新 data/lottery/539.csv、lotto.csv、power.csv。
5. 自動產生 output/lottery_final_dashboard.html。
6. Dashboard 顯示正確欄位：
   - 539：5 個號碼，無特別號。
   - 大樂透：6 個主號 + 特別號。
   - 威力彩：第一區 6 個號碼 + 第二區 1 個號碼。
7. 新增/保留 539 信心排行 Top10，Dashboard 會直接顯示。
8. 會輸出：
   output/539_confidence_rank.csv
   output/539_number_confidence_rank.csv
   output/lotto_special_rank.csv
   output/power_special_rank.csv

539 信心排行說明：
- 這是統計信心分數，不保證中獎。
- 依據近 30 / 80 / 180 期熱度、遺漏補位、組合關聯、號碼分散度計算。

---
雲端版：
  請看 README_CLOUD.md
  啟動：uvicorn cloud_app:app --host 0.0.0.0 --port 8000
  更新：/api/update 或 /api/update-sync?mode=weekly
