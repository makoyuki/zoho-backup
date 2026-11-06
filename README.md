# zoho-backup — Zoho(CRM・Projects・Books・Desk)のバックアップを API で取得

Zoho の 4 つのサービスのデータを API で取得し、Linux サーバのディレクトリに ZIP で保存するスクリプトです。python3 の標準ライブラリだけで動きます(curl・jq は不要)。

Zoho はサービスによって API でできることが違います。CRM と Projects には全体のバックアップの API がありますが、Books と Desk にはありません(画面からの手動のバックアップだけです)。そこで Books と Desk は、**画面から手で取った全体のバックアップを「起点」とし、それ以降に変わった分を API で取る**という形にしています。

```
systemd timer(毎日 02:05)
  └ zoho-backup.service(User=zbackup、書き込みは保存先と /var/lib/zoho-backup だけ)
      └ /usr/local/lib/zoho-backup/zoho-backup.py run
           ├ accounts.zoho.jp でアクセストークンを取得(refresh token は /etc/zoho-backup/credentials)
           ├ crm / projects / books / desk(設定で enabled = yes のもの)
           ├ 世代の整理
           └ 失敗したら mail_to へメール
```

| ファイル | 置き場所 | 内容 |
|---|---|---|
| `zoho-backup.py` | `/usr/local/lib/zoho-backup/`(root 0755) | 本体 |
| `conf/zoho-backup.conf.example` | `/etc/zoho-backup/zoho-backup.conf`(root:zbackup 0640) | 設定(秘密情報なし) |
| `conf/credentials.example` | `/etc/zoho-backup/credentials`(root:zbackup 0640) | OAuth の client_id・client_secret・refresh_token(**実際の値はリポジトリに入れない**) |
| `systemd/zoho-backup.{service,timer}` | `/etc/systemd/system/` | 定期実行 |

ユーザー名(`zbackup`)と保存先(`/srv/backup/zoho/`)は例です。環境に合わせて、設定と `zoho-backup.service` の `User`・`ReadWritePaths` を変えてください。

## 1. サービスごとの取得方法

| サービス | 方法 | 保存名 | 頻度・Zoho 側の制限 |
|---|---|---|---|
| CRM | 公式のバックアップ API(`/crm/bulk/v5/backup`・`/backup/urls`)。Zoho 側の予定で作られたものを取る | `YYYYMMDD_CRM.zip`(データ)、`_CRM_Data_002.zip`…(1GB 超の分割)、`_CRM_Attachments_001.zip`…(添付) | 予定は CRM の画面で設定する。毎日確認し、**新しいものだけ**取る。リンクの期限は 7 日 |
| Projects | 公式の V3 API `portal_export`(作成の依頼 → 状態の確認 → ダウンロード) | `YYYYMMDD_PRJ.zip` | 7 日ごと(`interval_days`)。作成は 24 時間に 1 回まで。ファイルは約 15 日で削除 |
| Books | **バックアップの API が無い**。各モジュールの一覧の API を全ページ取得し JSON で保存(毎回全件)。請求書・見積などの明細は、**起点の日以降に更新されたものだけ** 1 件ずつ取得 | `YYYYMMDD_BOOKS.zip` | 7 日ごと。API の回数の上限は契約の版による(他の連携と共有) |
| Desk | **バックアップの API が無い**。Bulk Export の API で、**起点の日以降に変わった分だけ**モジュールごとに出力(削除・ゴミ箱も入る)。さらに、起点以降に更新されたチケットのスレッド(メールの本文・原文 .eml。添付ファイルは原文に含まれる)とコメントを `threads/` に入れる(前回の ZIP にあるスレッドは写して、新しいものだけ取る) | `YYYYMMDD_DESK.zip` | 7 日ごと。開始 1 回 500 クレジット × モジュール数 + 1 件 1 クレジット |

### 起点(Books・Desk)

Books・Desk は、画面から手で取った全体のバックアップを「起点」とし、毎週は起点の日から変わった分だけを取ります(API の消費を抑えるため)。

- 置き場所: `baseline_dir` の `YYYYMMDD_Books.zip`・`YYYYMMDD_Desk.zip`(YYYYMMDD は画面でバックアップを取った日。読み取り専用にしておく)。最新の日付のものを使い、その日の 0:00 以降に変わった分を取る(重なりは許す)。世代の整理の対象外
- 復元に使うのは **起点 + 最新の ZIP** の 2 つ(毎週の ZIP は前回からではなく起点からの差分を毎回すべて含むので、途中の ZIP が欠けても困らない)
- 起点から日がたつほど差分が大きくなるので、**定期的に(例: 四半期ごと)画面のバックアップを取り直し、`baseline_dir` に置く**。置くだけで次回から新しい起点になる。古い起点は、それを使う ZIP が世代の整理で消えるまで残す
- 最新の起点が `baseline_remind_days`(既定 90 日)を超えると、取り直しのお知らせのメールが、取り直すまで `baseline_remind_interval_days`(既定 7 日)ごとに届く
- 画面のバックアップは Books が 15 日に 1 回、Desk が 30 日に 1 回まで(Books: 設定 → データのバックアップ、Desk: 設定 → データ管理 → データのバックアップ)
- Desk のスレッド(メールの本文)は Bulk Export の対象外なので、チケットの検索・スレッドの API で別に取る(起点以降に更新されたチケットの分。それ以前のものは起点の Threads の CSV にある)
- 起点が無いと全件を取る(Books の明細は件数が多いと上限 `max_requests` を超えて失敗する。Desk は全件の出力になる)

## 2. 安全のための作り

- 秘密情報は `credentials` にだけ書く。他人が読める権限なら動かない。トークンは POST の本文で送り、ログ・メールでは伏せる
- ダウンロードは HTTP の状態・Content-Type・サイズ・ZIP の CRC を検証してから置き換える(エラーの応答を `.zip` として保存しない)
- Authorization は `auth_host_suffixes` のホストにだけ送る。リダイレクトは自前で追う
- 世代の整理は `YYYYMMDD_<種別>[_…].zip` に完全一致するものだけが対象。日付単位で数える
- 最後の取得から `alert_days` 日を超えても新しいものが取れないときは、失敗として通知する

## 3. 導入

```bash
# 実行用のユーザーと保存先
sudo useradd -r -s /usr/sbin/nologin zbackup
sudo install -d -o zbackup -g zbackup -m 0750 /srv/backup/zoho /srv/backup/zoho/backup /srv/backup/zoho/baseline

# 本体・設定
sudo install -d -o root -g root -m 0755 /usr/local/lib/zoho-backup
sudo install -o root -g root -m 0755 zoho-backup.py /usr/local/lib/zoho-backup/
sudo install -d -o root -g zbackup -m 0750 /etc/zoho-backup
sudo install -o root -g zbackup -m 0640 conf/zoho-backup.conf.example /etc/zoho-backup/zoho-backup.conf
sudo install -o root -g zbackup -m 0640 /dev/null /etc/zoho-backup/credentials
sudo install -d -o zbackup -g zbackup -m 0750 /var/lib/zoho-backup
sudoedit /etc/zoho-backup/zoho-backup.conf   # 保存先・メールの宛先・portal_id などを書き、使うサービスを enabled = yes にする
sudoedit /etc/zoho-backup/credentials        # conf/credentials.example の形で値を書く(次の 4 で発行する)
sudo install -m 0644 systemd/zoho-backup.service systemd/zoho-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload

# 確認(読み取りの API だけ。何も保存しない)
sudo runuser -u zbackup -- /usr/local/lib/zoho-backup/zoho-backup.py check
sudo runuser -u zbackup -- /usr/local/lib/zoho-backup/zoho-backup.py test-mail

# 定期実行を有効にする
sudo systemctl enable --now zoho-backup.timer
sudo systemctl start zoho-backup.service; journalctl -u zoho-backup -n 50
```

Books・Desk を有効にする場合は、先に画面からバックアップを取り、起点として `baseline_dir` に置いてください。

## 4. refresh token の発行

scope は発行時に決まり、後から足せません。scope を増やすときは発行し直してください。

1. https://api-console.zoho.jp/ に、Self Client を作ったユーザー(各サービスの管理者。Projects は Portal Owner)でログインし、Self Client を開く(無ければ作る)
2. **Generate Code** で、Scope 欄に下の 1 行をそのまま貼り付け、Time Duration は 10 分、説明は「zoho-backup」で Create(使わないサービスの分は省いてよい)
   ```
   ZohoCRM.bulk.backup.READ,ZohoFiles.files.ALL,ZohoProjects.portals.READ,ZohoBooks.contacts.READ,ZohoBooks.settings.READ,ZohoBooks.estimates.READ,ZohoBooks.invoices.READ,ZohoBooks.customerpayments.READ,ZohoBooks.creditnotes.READ,ZohoBooks.projects.READ,ZohoBooks.expenses.READ,ZohoBooks.salesorders.READ,ZohoBooks.purchaseorders.READ,ZohoBooks.bills.READ,ZohoBooks.debitnotes.READ,ZohoBooks.vendorpayments.READ,ZohoBooks.banking.READ,ZohoBooks.accountants.READ,Desk.exports,Desk.basic.READ,Desk.organization.READ,Desk.tickets.READ,Desk.search.READ
   ```
   - 「Select Portal」で Desk・CRM が出たら、それぞれクリックして本番の組織を選んでから Create
   - Invalid scope などのエラーが出たら、`Desk.exports` を `Desk.exports.CUSTOM` に変えて Generate し直す(公式資料の表記に揺れがある)
3. 表示された code を、10 分以内に refresh token に交換する。`<client_id>` だけ実際の値(Self Client の「Client Secret」タブ)に書き換えて実行し、client_secret(同じタブ)と code は聞かれたときに貼り付ける(画面・履歴に残らない)。プロキシが必要なら curl に `-x` を足す
   ```bash
   read -rsp 'client_secret: ' SECRET; echo; read -rsp 'code: ' CODE; echo
   printf 'grant_type=authorization_code&client_id=<client_id>&client_secret=%s&code=%s' "$SECRET" "$CODE" \
     | curl -s https://accounts.zoho.jp/oauth/v2/token -d @- \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("refresh_token") or d)'
   unset SECRET CODE
   ```
   `1000.` で始まる 1 行が出れば成功。`invalid_code` は期限切れか貼り間違い(Generate からやり直す)
4. credentials の refresh_token を、表示された値に書き換える
   ```bash
   sudoedit /etc/zoho-backup/credentials
   ```
5. Books・Desk の組織の ID を確認する(この段階の「失敗: 未設定」は正常。「組織:」の行の数字を控える)
   ```bash
   sudo runuser -u zbackup -- /usr/local/lib/zoho-backup/zoho-backup.py check books desk
   ```
6. 設定に書く: `[books]` に `enabled = yes`・`organization_id = <数字>`、`[desk]` に `enabled = yes`・`org_id = <数字>`
7. もう一度 `check` を実行する(最後の行が `失敗 -` なら OK)。有効にすると次の定期実行から取得が始まるので、先に「起点」を `baseline_dir` に置いておくこと

## 5. 運用

- 状態: `/var/lib/zoho-backup/`(`crm.json` などの前回の取得、`manifest.tsv` = 保存したファイルとサイズ・sha256)
- ログ: `journalctl -u zoho-backup`
- 取得が止まったときの通知: 最後の取得から `alert_days` 日を超えても新しいものが取れないとき、または CRM の Zoho 側の予定が停止・失敗のとき、失敗としてメールが届く(直るまで毎日)。CRM の予定の周期を変えたら `alert_days` も合わせる
- 手で実行: `sudo systemctl start zoho-backup.service`(特定のサービスだけ・試すだけなら `sudo runuser -u zbackup -- /usr/local/lib/zoho-backup/zoho-backup.py [--dry-run] run projects`)
- 取得の頻度を早めたいとき: `/var/lib/zoho-backup/<サービス>.json` を消す(次回の実行で取得する)
- CRM のバックアップの回数は版で決まる(月 2 回など)。予定の変更は CRM の画面で行う(API の予定作成は回数を使うので、このスクリプトはしない)

## 6. 注意

- 日本のデータセンター(`zoho.jp`)でだけ動作を確認しています。ほかのデータセンターでは、`accounts_url`・`api_domain`・`auth_host_suffixes`、`[projects]`・`[desk]` の `api_url` を変える必要があります(未確認)
- Zoho の非公式のスクリプトです。Zoho の API の仕様の変更で動かなくなることがあります
- 取得した ZIP から実際に復元できるかは、環境ごとに確かめてください

## ライセンス

MIT License。[LICENSE](LICENSE) を参照してください。
