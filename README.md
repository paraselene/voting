# 教會執事選舉

手機優先、繁體中文的多輪教會投票系統。Django 5.2、SQLite、Gunicorn、WhiteNoise、ReportLab；單一容器，預設投票關閉。

## 首次部署

1. 將 `.env.example` 複製為 `.env`，填寫正式網域及公開 HTTPS 網址。網址上限 200 字元。正式環境不使用 `DEBUG=1`。
2. 分別執行以下指令產生三個金鑰，依序填入 `SECRET_KEY`、`PASSWORD_INDEX_KEY`、`PASSWORD_ENCRYPTION_KEY`。前兩者必須不同，至少 40 字元。請將三者另行安全保存；遺失索引或加密金鑰會使現有憑證無法使用。不要在有紀錄或共用的終端輸出正式秘密。

   ```sh
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
   ```

3. 建立 `data`、`fonts` 目錄，限制 `.env` 權限為 `600`、`data` 為 `700`。將 Google Fonts 的 `NotoSansTC[wght].ttf` 及 OFL 授權放入 `fonts/`（可沿用主日學字型）。安裝開發依賴後，執行 `.venv/bin/fonttools varLib.instancer "fonts/NotoSansTC[wght].ttf" wght=400 --output fonts/NotoSansTC-Regular.ttf`，產生適合 ReportLab 的標準字重。字型不提交 Git，容器以唯讀掛載載入。
4. 確認既有外部網絡 `wordpress_wordpress_net` 可用，執行 `docker compose up -d --build`。
5. 以 `docker compose exec voting-web python manage.py create_admin` 建立首位管理員。密碼只輸出一次，管理員不保存可還原密碼。必要時可用同一指令建立另一位管理員。
6. 反向代理 upstream 使用 `voting-web:8000`，不公開容器連接埠。代理處理 TLS、傳送 Host，並**覆寫** `X-Forwarded-Proto` 與 `X-Real-IP`，不可直接相信客戶端傳入的值。例如 Nginx：

   ```nginx
   location / {
       proxy_pass http://voting-web:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_set_header X-Real-IP $remote_addr;
   }
   ```

   將代理在 Docker 網絡的實際固定 IP 填入 `TRUSTED_PROXY_IPS`（多個以逗號分隔）。若前方還有 CDN，須在代理正確設定可信來源，不能原樣轉送外部 IP 標頭。應用僅對列出的代理採用 `X-Real-IP`，其餘使用 TCP 來源。此網絡只應加入可信服務。
7. 執行下方 `setup_election` 初始化編號用戶及候選人，以管理員登入匯出憑證，再建立並開放首輪投票。

## 輪次、編號用戶與環境設定

這是全新安裝版本，不支援舊單場資料庫原地升級。使用空白資料目錄；不要對舊資料庫執行新版初始化遷移。歷史舊備份亦不能還原到新版。首次安裝只建立結構，不會自動建立或清除選舉資料。

`.env`（Git 忽略，權限 600）使用逗號分隔的 `ELECTION_TITLES`（依次序顯示）及 `CANDIDATE_NAMES`（恰好 19 位、不重複）。`.env.example` 包含標題清單及示例候選人，實際候選人只填入本機 `.env`，不提交 Git。Docker 讀取 `.env`；本機直接執行 Python 須先自行載入環境變數。修改標題設定後重建容器即影響新輪次；修改候選人名單後須明確執行 setup/reset 才會重建名單。已存輪次不跟隨環境設定變動。

```sh
docker compose exec voting-web python manage.py setup_election --confirm 清除全部資料
```

此指令先備份再清除所有輪次及用戶，按環境設定建立 19 位候選人及永久編號 1–19 的用戶（不儲存姓名），保留管理員。舊用戶登入及憑證失效，重新由管理頁匯出憑證。一般啟動／重啟只執行結構遷移，保留全部資料。

- 管理員選擇標題、1–19 位候選人及 1 至所選人數的投票上限，建立後立即開放。預設全選，提供全選／取消全選；「10位執事」預設上限 10，其他職位預設 1。
- 必須先關閉最新輪次才可建立下一輪。標題可重複，以輪次編號及建立時間區分。已建立的標題、上限及候選人名單固定，只有最新輪次可以重新開放，保留選票及設定。
- 同一套 19 位用戶憑證用於所有輪次。投票者只看到最新輪次及自己的選票；每輪可選 0 至上限人數，空白票須確認，開放期間重投完整取代本輪舊選票。
- 管理員可選擇歷史輪次查看結果及參與數字，不顯示個別選擇。空白票總數不顯示；開放中隱藏票數及名次，關閉後顯示，同票並列、不自動判定當選。資料庫仍將選票連結至編號帳戶，並非不可追溯的匿名選舉。
- 可逐一啟用／停用編號用戶；停用後仍可用原密碼／QR 登入及查看已投選票，既有選票仍計票，不能提交或修改。管理員不能停用。
- 管理頁不顯示用戶密碼，也不提供點擊揭示功能；登入憑證只透過 PDF 匯出。PDF 每頁 2 欄 × 4 列（8 張），只印編號、密碼與 QR，沒有投票者姓名或固定投票上限。掃描會切換至該帳戶，保留 CSRF 與限速驗證。
- 「清除本輪選票」須確認，只清除最新輪次的選票並關閉，保留歷史、用戶及憑證。「清除全部資料」須確認，刪除所有輪次並重建固定名單及全新 19 套憑證，下一輪編號重新從 1 開始，保留管理員；此網頁操作不另行備份，請先手動備份。
- 所有投票及管理操作包含輪次 ID 和版本，交易內拒絕過期頁面；關閉、重新開放及清除都會使舊表單失效。
- 密碼為 5 位英數字、不分大小寫，排除 `0 O 1 I L`；QR 等同密碼。Fernet 加密用戶憑證，HMAC-SHA256 作登入索引；管理員只存索引。資料庫與金鑰分開保管，勿任意更換金鑰。
- 失敗登入共用資料庫限速，可信代理之外忽略來源 IP 標頭；保留權限、CSRF、安全 cookie、禁止快取及登入時 session/CSRF 旋轉。登入一小時有效，共用裝置請登出。

## 更新、備份、還原

更新前先執行線上備份，將備份複製到離機的受保護位置。備份包含管理員姓名、加密憑證及各輪選票；必須限制存取。秘密金鑰另行安全保存。SQLite 使用短 `IMMEDIATE` 交易，逾時會顯示重試訊息。

```sh
docker compose exec voting-web python manage.py backup_db
./update.sh
```

備份位於 `./data/backups/`，以 SQLite backup API 取得一致快照，完成完整性檢查後原子命名。`update.sh` 執行 `git pull --ff-only` 及重建啟動，任一步失敗立即停止。

容器啟動時先建立資料庫結構，再啟動 Gunicorn。此版本只支援全新資料庫；沒有舊資料升級遷移。`update.sh` 不等待網站就緒，完成後以 `docker compose logs --tail 50 voting-web` 確認。

還原前先備份現有資料，**停止所有會使用該 SQLite 的程序**。還原指令不能確認外部程序是否停止，請勿在運行中的網站執行。停止後由一次性容器還原：

```sh
docker compose exec voting-web python manage.py backup_db
docker compose stop voting-web
docker compose run --rm --no-deps voting-web python manage.py restore_db /data/backups/你的備份.sqlite3 --confirm 還原資料庫
docker compose up -d
```

還原先驗證資料庫完整性與資料表，透過暫存檔原子替換；清除既有登入 session、關閉投票並遞增版本。恢復後核對用戶與結果，確認無誤才重新開放。

## 本機開發與檢查

本機 Docker 使用獨立設定，不依賴正式 `.env` 或外部代理網絡：

```sh
docker compose -p voting-local -f compose.local.yaml up -d --build
docker compose -p voting-local -f compose.local.yaml exec voting-web python manage.py setup_election --confirm 清除全部資料
docker compose -p voting-local -f compose.local.yaml exec voting-web python manage.py create_admin
```

開啟 `http://localhost:18000`。只綁定本機 loopback；資料存於 `data/local-docker/`，採用本機測試金鑰，勿放真實會眾資料或用於正式部署。
空白本機資料庫可執行 `.venv/bin/python scripts/smoke_local.py`，完成真實 HTTP／CSRF、PDF、多用戶提交、重啟及備份還原驗證；會建立虛構測試資料，憑證寫入 `data/local-docker/demo-credentials.json`（600 權限）。已有用戶時腳本會拒絕執行，避免覆寫人工測試資料。可用 `VOTING_LOCAL_PROJECT`、`VOTING_LOCAL_DATA`、`VOTING_LOCAL_PORT` 選擇獨立的測試容器、資料目錄及埠號；Compose 使用後兩者。

```sh
uv venv --python 3.13
uv pip install -r requirements-dev.txt
export DEBUG=1
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py setup_election --confirm 清除全部資料
.venv/bin/python manage.py create_admin
.venv/bin/python manage.py runserver
```

`DEBUG=1` 只使用固定本機測試金鑰，不能帶到正式站。環境變數直接從 shell 或 Compose 讀取；Django 不自動讀 `.env`。

```sh
DEBUG=1 .venv/bin/python manage.py check
DEBUG=1 .venv/bin/python manage.py makemigrations --check --dry-run
DEBUG=1 .venv/bin/python manage.py test
node --test scripts/test_qr_login.cjs
DEBUG=1 .venv/bin/python manage.py collectstatic --noinput
sh -n update.sh
```

測試使用獨立的 `data/test.sqlite3` 實體檔案，勿指向正式資料庫；並行測試使用多個 SQLite 連線。若上次測試中斷，確認沒有程序使用後刪除該測試檔再執行。

## 正式上線驗收

- HTTPS 轉址、Host／CSRF 來源、安全 cookie 與信任代理 IP。
- 管理員／用戶登入、手機操作、每輪上限、空白票、重投、關閉與重新開放。
- PDF 的全部／勾選、多頁、編號、嵌入中文字型及實際列印裁切。
- 重建／重啟容器後資料仍存在；備份還原演練。
- 用預計會眾同時投票人數做負載測試；單容器單 worker、4 threads，SQLite 序列化寫入，不能以本機功能測試代替正式負載測試。

不包含照片、通知或自動判定當選。

本次驗證請以測試輸出及實際本機操作為準；舊單場版本的驗證紀錄不適用。
