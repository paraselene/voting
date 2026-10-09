# 教會執事選舉

手機優先、繁體中文的單場教會投票系統。Django 5.2、SQLite、Gunicorn、WhiteNoise、ReportLab；單一容器，預設投票關閉。

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
7. 以管理員登入，新增候選人與用戶，匯出登入憑證，再開放投票。

## 使用規則與私隱

- 只需密碼登入；5 字元英數密碼排除 `0 O 1 I L`，不分大小寫。所有身份共用唯一索引。用戶可以同名，以編號區分。
- 可選 0–10 人。空白票須勾選確認，瀏覽器另有確認提示。重投完整取代舊選票，空白票也計入已投票。
- 關閉後可查看自己的選票，重新開放可修改。有任何選票（含空白票）即鎖定候選人名單。
- 「清除全部選票」須在彈出視窗點擊確認，保留身份、密碼、候選人，關閉投票並使舊頁面失效。
- 「清除全部資料」須在彈出視窗點擊確認，刪除全部選票、候選人及一般用戶與憑證，關閉投票並使舊頁面及一般用戶登入失效；保留所有管理員帳戶、密碼及登入。登入嘗試限制仍然保留。
- 結果按票數排序，同票並列，不自動判定當選。管理頁不顯示個別選擇；資料庫仍關聯用戶與選票，因此不是不可追溯的匿名選舉。資料庫管理者仍可查閱。
- 用戶憑證以 Fernet 加密儲存，HMAC-SHA256 索引用於登入；管理員只存索引。金鑰與資料庫須分開保管。勿任意更換索引或加密金鑰。
- 共用資料庫記錄來源 IP 的失敗次數（IP 以 HMAC 儲存），前 5 次失敗無延遲，其後逐步增加到最多 8 秒；5 分鐘無失敗後重設，1 小時後清理。短密碼仍可能被分散式猜測；會前才發放憑證，限制開放時段，代理可加整體流量防護。持有憑證者即可代投。
- 登入、管理及 PDF 回應禁止快取；不啟用 access log，不記錄密碼、PDF 或選票內容。正式環境關閉 debug，也不要在代理記錄 POST body。
- 登入閒置設定為一小時有效期（從登入起計，不自動延長），共用裝置請登出。

## 更新、備份、還原

更新前先執行線上備份，將備份複製到離機的受保護位置。備份包含姓名、加密憑證及選票；必須限制存取。秘密金鑰另行安全保存。SQLite 使用短 `IMMEDIATE` 交易，逾時會顯示重試訊息。

```sh
docker compose exec voting-web python manage.py backup_db
./update.sh
```

備份位於 `./data/backups/`，以 SQLite backup API 取得一致快照，完成完整性檢查後原子命名。`update.sh` 執行 `git pull --ff-only` 及重建啟動，任一步失敗立即停止。

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
docker compose -p voting-local -f compose.local.yaml exec voting-web python manage.py create_admin
```

開啟 `http://localhost:18000`。只綁定本機 loopback；資料存於 `data/local-docker/`，採用本機測試金鑰，勿放真實會眾資料或用於正式部署。
空白本機資料庫可執行 `.venv/bin/python scripts/smoke_local.py`，完成真實 HTTP／CSRF、PDF、多用戶提交、重啟及備份還原驗證；會建立虛構測試資料，憑證寫入 `data/local-docker/demo-credentials.json`（600 權限）。已有用戶時腳本會拒絕執行，避免覆寫人工測試資料。

```sh
uv venv --python 3.13
uv pip install -r requirements-dev.txt
export DEBUG=1
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py create_admin
.venv/bin/python manage.py runserver
```

`DEBUG=1` 只使用固定本機測試金鑰，不能帶到正式站。環境變數直接從 shell 或 Compose 讀取；Django 不自動讀 `.env`。

```sh
DEBUG=1 .venv/bin/python manage.py check
DEBUG=1 .venv/bin/python manage.py makemigrations --check --dry-run
DEBUG=1 .venv/bin/python manage.py test
DEBUG=1 .venv/bin/python manage.py collectstatic --noinput
sh -n update.sh
```

測試使用獨立的 `data/test.sqlite3` 實體檔案，勿指向正式資料庫；並行測試使用多個 SQLite 連線。若上次測試中斷，確認沒有程序使用後刪除該測試檔再執行。

## 正式上線驗收

- HTTPS 轉址、Host／CSRF 來源、安全 cookie 與信任代理 IP。
- 管理員／用戶登入、手機操作、10 位上限、空白票、重投、關閉與重新開放。
- PDF 的全部／勾選、多頁、長姓名、嵌入中文字型及實際列印裁切。
- 重建／重啟容器後資料仍存在；備份還原演練。
- 用預計會眾同時投票人數做負載測試；單容器單 worker、4 threads，SQLite 序列化寫入，不能以本機功能測試代替正式負載測試。

不包含多場選舉、照片、通知或自動判定當選。

## 本機驗證紀錄（2026-10-09）

- Docker Desktop 29.8.2；Python 3.13 容器成功建置、啟動及重建，網址 `http://localhost:18000`，僅綁定 loopback。
- 本機及容器內 18 項測試通過，包含實體 SQLite 並行交易、真正鎖定逾時、舊選票保留、權限、CSRF、限速、空白票、重投與候選人鎖定。
- Django check、遷移一致性、Python 編譯、靜態檔收集、update.sh 語法及可執行權限通過。
- 真實 HTTP：19 位用戶、10 個並行連線，登入及投票約 0.49 秒；這是小規模功能冒煙測試，不代表正式會議容量。
- 20 張憑證分為 8／8／4 張共 3 頁，中文字型已嵌入；逐頁渲染檢查長姓名、行距與裁切線通過。
- 容器重啟／重建資料保留，線上備份、停止容器後還原、舊 session 失效及清除後舊頁面拒收均通過。
- 以正式環境設定測試 HTTPS 轉址、安全 CSRF cookie、HSTS、CSRF 拒收及 frame/cache headers；缺少秘密金鑰時正確拒絕啟動。
- `check --deploy` 保留 W005／W021：尚未啟用 HSTS 所有子網域及 preload，須待正式網域政策確認；不是自動替其他子網域作承諾。
- 尚未驗證真實手機／瀏覽器視覺操作（本次 Comet 工具未獲授權）、實體列印、正式反向代理、正式容量或遠端部署。

本機保留 20 位虛構用戶及 12 位候選人，還原後投票關閉。管理員及用戶的測試憑證位於 `data/local-docker/demo-credentials.json`，可登入後開放投票繼續人工驗收。
