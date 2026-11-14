# 合約數據雷達

USDT 永續合約的公開行情整理。靜態頁面，資料大約每天更新一次。頁面上的署名只有 [@kalilkalilkai](https://x.com/kalilkalilkai)。

數字來自 `https://futures.ourbit.com` 的公開合約行情，不使用金鑰。總表讀取全市場報價、資金費率與合約規格；單幣頁另取日線與近期資金費率結算。請求會附上瀏覽器形式的 User-Agent。

名義持倉＝持倉張數 × 每張數量 × 最新價（USDT）。持倉量變化比較的是基礎幣數量。每次整理會用 24 小時成交額對一下「成交張數 × 每張數量 × 最新價」的數量級，差距過大就停止，並保留上一次成功的頁面。

介面沒有持倉歷史。比較用的前次數量存在 `data/oi-snapshot.json`，只保留最近 14 個日期。

## 本地產生

需要 Python 3.11 或更新版本，只用標準庫。

```bash
python3 build.py
```

產出在 `site/`。若行情讀取或頁面檢查失敗，既有的 `site/` 與快照不會被覆蓋。

單幣頁只做 24 小時成交額前 150 名。其餘合約仍列在總表。

## 發布到 GitHub Pages

預期網址：<https://kalilkai.github.io/perp-radar/>

1. 打開倉庫的 Settings → Pages → Build and deployment，Source 選 **GitHub Actions**。
2. 到 Actions 手動執行「每日更新頁面」，或等每天 00:30 UTC 的排程。

工作流程會重新產生頁面。在 `main` 上會把快照與 `site/` 推回倉庫，再發布到 Pages。推回時使用 GitHub Actions 的預設提交身分。

若執行環境連不到 `https://futures.ourbit.com`，這次工作流程會失敗，已發布的頁面維持原樣。改在可以連線的機器上執行：

```bash
python3 build.py && ./publish.sh
```

`publish.sh` 會提交 `site/` 與 `data/oi-snapshot.json`，並推送目前分支。執行前請把 git 作者設成 GitHub 的 noreply 位址；腳本會拒絕其他信箱。

## 頁面

- `site/index.html`：總表，可用五個數據範圍篩選（成交額前段、波動較大、資金費率偏離、持倉規模或變化、指數價差）
- `site/coins/`：單幣頁
- `site/about.html`：關於與揭露
