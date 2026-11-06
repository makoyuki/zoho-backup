#!/usr/bin/env python3
"""zoho-backup.py — Zoho(CRM・Projects・Books・Desk)のバックアップを API で取得する。

  run [SERVICE ...]   取得して、世代を整理する(SERVICE 省略時は設定で enabled = yes のもの全部)
  check               トークンの取得と、各サービスの読み取りの API だけを試す(何も作らない・保存しない)
  rotate              世代の整理だけ
  test-mail           試験のメールを送る
  --dry-run           (run・rotate)保存・削除・状態の更新をせずに、何をするかだけ表示する

設定: /etc/zoho-backup/zoho-backup.conf(-c で変更)。秘密情報は credentials(所有者のみ読める権限であること)。
python3 の標準ライブラリのみで動く(curl・jq は不要)。
"""
import argparse
import configparser
import datetime
import fcntl
import hashlib
import http.client
import json
import os
import re
import smtplib
import socket
import stat
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.message import EmailMessage

DEFAULT_CONF = "/etc/zoho-backup/zoho-backup.conf"
SERVICES = ("crm", "projects", "books", "desk")
# 保存ファイル名の種別(世代の整理もこの名前で判定する)
TAGS = {"crm": "CRM", "projects": "PRJ", "books": "BOOKS", "desk": "DESK"}
USER_AGENT = "zoho-backup/1.0"
CHUNK = 1024 * 1024


class BackupError(Exception):
    pass


class NotReady(Exception):
    """Zoho 側でまだバックアップが用意できていない(次回の実行で取得する)。失敗としては扱わない。"""


# ------------------------------------------------------------------ 設定

def load_config(path):
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8"):
        raise BackupError("設定ファイルが読めません: %s" % path)
    g = cp["general"]
    conf = {
        "backup_dir": g.get("backup_dir"),
        "state_dir": g.get("state_dir", "/var/lib/zoho-backup"),
        "credentials": g.get("credentials", "/etc/zoho-backup/credentials"),
        "accounts_url": g.get("accounts_url", "https://accounts.zoho.jp").rstrip("/"),
        "api_domain": g.get("api_domain", "https://www.zohoapis.jp").rstrip("/"),
        "proxy": g.get("proxy", "").strip(),
        # Authorization ヘッダーを送ってよいホスト(リダイレクト先が他社のストレージ等なら送らない)
        "auth_host_suffixes": [s.strip().lower() for s in
                               g.get("auth_host_suffixes", "zoho.jp,zohoapis.jp").split(",") if s.strip()],
        "keep_generations": g.getint("keep_generations", 6),
        # 画面から手で取った全体のバックアップ(起点)の置き場所。Books・Desk はこれ以降に変わった分だけ取る
        "baseline_dir": g.get("baseline_dir", "/srv/backup/zoho/baseline"),
        "min_free_gb": g.getfloat("min_free_gb", 5),
        "timeout": g.getint("timeout", 120),
        "mail": dict(cp["mail"]) if cp.has_section("mail") else {},
        "general": dict(g),
        "services": {},
    }
    if not conf["backup_dir"]:
        raise BackupError("[general] backup_dir が未設定です")
    for name in SERVICES:
        sec = dict(cp[name]) if cp.has_section(name) else {}
        sec["enabled"] = cp.getboolean(name, "enabled", fallback=False) if cp.has_section(name) else False
        if "keep_generations" in sec:
            sec["keep_generations"] = int(sec["keep_generations"])
        conf["services"][name] = sec
    return conf


def load_credentials(path):
    try:
        st = os.stat(path)
    except OSError as e:
        raise BackupError("credentials が読めません: %s" % e)
    # 他人が読める・グループや他人が書ける秘密情報は使わない(漏えい・すり替えの防止)
    if st.st_mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IWGRP):
        raise BackupError("credentials の権限が緩すぎます(%s: %o)。0600 か 0640 にしてください"
                          % (path, stat.S_IMODE(st.st_mode)))
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    try:
        sec = cp["oauth"]
        cred = {k: sec[k].strip() for k in ("client_id", "client_secret", "refresh_token")}
    except KeyError as e:
        raise BackupError("credentials の [oauth] に %s がありません" % e)
    if not all(cred.values()):
        raise BackupError("credentials の [oauth] に空の値があります")
    return cred


# ------------------------------------------------------------------ HTTP

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトは自前で追う(urllib は Authorization をそのまま別ホストへ送ってしまうため)。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, conf):
        self.conf = conf
        self.token = None
        handlers = [NoRedirect()]
        if conf["proxy"]:
            handlers.append(urllib.request.ProxyHandler({"http": conf["proxy"], "https": conf["proxy"]}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def _auth_allowed(self, url):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if urllib.parse.urlsplit(url).scheme != "https":
            return False
        return any(host == s or host.endswith("." + s) for s in self.conf["auth_host_suffixes"])

    def open(self, method, url, data=None, headers=None, auth=True, max_redirects=5):
        """応答(ファイル風オブジェクト)を返す。3xx は Location を追い、Authorization は許可したホストにだけ送る。"""
        for _ in range(max_redirects + 1):
            h = {"User-Agent": USER_AGENT}
            h.update(headers or {})
            if auth and self.token and self._auth_allowed(url):
                h["Authorization"] = "Zoho-oauthtoken " + self.token
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            try:
                resp = self.opener.open(req, timeout=self.conf["timeout"])
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    url = urllib.parse.urljoin(url, e.headers["Location"])
                    if e.code == 303 or (e.code in (301, 302) and method == "POST"):
                        method, data = "GET", None
                    e.close()
                    continue
                raise
            return resp
        raise BackupError("リダイレクトが多すぎます: %s" % redact(url))

    def request(self, method, url, params=None, data=None, headers=None, auth=True, retries=3, ok=(200,)):
        """JSON の API を呼ぶ。戻り値: (status, dict)。5xx・通信エラーは再試行する。"""
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
        last = None
        for i in range(retries):
            try:
                with self.open(method, url, data=data, headers=headers, auth=auth) as r:
                    body = r.read()
                    status = r.status
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
                e.close()
                if status >= 500 or status == 429:
                    last = "HTTP %d: %s" % (status, short(body))
                    time.sleep(10 * (i + 1))
                    continue
            except (urllib.error.URLError, socket.timeout, http.client.HTTPException, ConnectionError) as e:
                last = "通信エラー: %s" % e
                time.sleep(10 * (i + 1))
                continue
            try:
                js = json.loads(body) if body.strip() else {}
            except ValueError:
                js = {"_raw": short(body)}
            if status not in ok:
                raise BackupError("%s %s → HTTP %d: %s" % (method, redact(url), status, short(body)))
            return status, js
        raise BackupError("%s %s → %s" % (method, redact(url), last))

    def get_token(self, cred):
        # 秘密情報はコマンドラインに出さず、POST の本文で送る
        _, js = self.request("POST", self.conf["accounts_url"] + "/oauth/v2/token", auth=False, data={
            "grant_type": "refresh_token",
            "client_id": cred["client_id"],
            "client_secret": cred["client_secret"],
            "refresh_token": cred["refresh_token"],
        })
        if not js.get("access_token"):
            raise BackupError("アクセストークンを取得できません: %s" % js.get("error", js))
        self.token = js["access_token"]
        if js.get("api_domain"):
            self.conf["api_domain"] = js["api_domain"].rstrip("/")

    def download(self, method, url, dest, expect_zip=True, data=None, headers=None, retries=3):
        """dest へ保存する。<dest>.part に書いてから検証し、問題なければ置き換える。戻り値: (bytes, sha256)。"""
        part = dest + ".part"
        last = None
        for i in range(retries):
            try:
                return self._download_once(method, url, dest, part, expect_zip, data, headers)
            except (urllib.error.URLError, socket.timeout, http.client.HTTPException, ConnectionError) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429:
                    raise BackupError("ダウンロード失敗 %s → HTTP %d: %s"
                                      % (redact(url), e.code, short(e.read())))
                last = e
                time.sleep(15 * (i + 1))
            finally:
                if os.path.exists(part):
                    os.unlink(part)
        raise BackupError("ダウンロード失敗 %s → %s" % (redact(url), last))

    def _download_once(self, method, url, dest, part, expect_zip, data, headers):
        h = hashlib.sha256()
        size = 0
        with self.open(method, url, data=data, headers=headers) as r:
            ctype = r.headers.get("Content-Type", "")
            if "text/html" in ctype or "application/json" in ctype:
                raise BackupError("ファイルではなく %s が返りました(%s): %s"
                                  % (ctype, redact(url), short(r.read(2000))))
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(fd, "wb") as f:
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    h.update(buf)
                    size += len(buf)
                f.flush()
                os.fsync(f.fileno())
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) != size:
                raise BackupError("サイズが一致しません(%s: %d / %s)" % (redact(url), size, clen))
        if size == 0:
            raise BackupError("空のファイルが返りました: %s" % redact(url))
        if expect_zip:
            check_zip(part)
        os.replace(part, dest)
        return size, h.hexdigest()


def check_zip(path):
    if not zipfile.is_zipfile(path):
        raise BackupError("ZIP ではありません: %s" % os.path.basename(path))
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise BackupError("ZIP が壊れています(%s の %s)" % (os.path.basename(path), bad))
        if not z.namelist():
            raise BackupError("ZIP の中身が空です: %s" % os.path.basename(path))


def short(body, n=500):
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    return redact(" ".join(body.split()))[:n]


def redact(s):
    """ログ・メールに秘密情報を出さない(トークン・URL の署名など)。"""
    s = re.sub(r"1000\.[0-9a-f]{20,}(\.[0-9a-f]{20,})?", "<token>", s)
    s = re.sub(r"(?i)((?:access_token|refresh_token|client_secret|token|ticket|signature|sig|x-amz-[a-z-]+)=)[^&\s\"']+",
               r"\1<redacted>", s)
    return s


# ------------------------------------------------------------------ 保存・状態

class Ctx:
    def __init__(self, conf, http, dry_run, log):
        self.conf = conf
        self.http = http
        self.dry_run = dry_run
        self.log = log
        self.check_only = False
        self.today = datetime.date.today().strftime("%Y%m%d")

    def svc(self, name):
        return self.conf["services"][name]

    def path(self, tag, suffix=""):
        return os.path.join(self.conf["backup_dir"], "%s_%s%s.zip" % (self.today, tag, suffix))

    def state(self, name):
        p = os.path.join(self.conf["state_dir"], name + ".json")
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save_state(self, name, st):
        if self.dry_run:
            return
        p = os.path.join(self.conf["state_dir"], name + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def fetch(self, method, url, dest, **kw):
        """ダウンロードして、記録を残す。dry-run では何もしない。"""
        if self.dry_run:
            self.log("  (dry-run) %s を保存する" % os.path.basename(dest))
            return
        if os.path.exists(dest):
            # 同じ日の途中で失敗した実行の残り(保存は検証後の置き換えなので、あるものは完全)。上書きしない
            if kw.get("expect_zip", True):
                check_zip(dest)
            self.log("  既にあるので取得しない: %s" % os.path.basename(dest))
            return
        check_free(self.conf)
        size, digest = self.http.download(method, url, dest, **kw)
        self.log("  保存: %s(%s バイト, sha256 %s)" % (os.path.basename(dest), format(size, ","), digest))
        with open(os.path.join(self.conf["state_dir"], "manifest.tsv"), "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%d\t%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"),
                                         dest, size, digest))


def check_free(conf):
    st = os.statvfs(conf["backup_dir"])
    free = st.f_bavail * st.f_frsize / 1024 ** 3
    if free < conf["min_free_gb"]:
        raise BackupError("保存先の空きが少ない(%.1f GB < %.1f GB): %s" % (free, conf["min_free_gb"], conf["backup_dir"]))


# ------------------------------------------------------------------ CRM

def backup_crm(ctx):
    """Zoho 側の定期バックアップ(CRM の「データバックアップ」の予定)ができた分を取得する。
    CRM は API から即時のバックアップを作れない(予定の作成・変更はできる)。毎日実行し、新しいものだけを取る。"""
    svc = ctx.svc("crm")
    base = ctx.conf["api_domain"] + "/crm/bulk/%s/backup" % svc.get("api_version", "v5")
    _, info = ctx.http.request("GET", base, ok=(200, 204))
    b = info.get("backup") or {}
    ctx.log("  予定: %s / 次回 %s / 状態 %s" % (b.get("rrule"), b.get("scheduled_date"), b.get("status")))
    if not b:
        raise BackupError("CRM のバックアップの予定がありません(CRM の設定 → データ管理 → データのバックアップ で予定を作成すること)")
    # 予定が止まっている・Zoho 側で失敗した場合も、ダウンロードできる分は取ってから失敗として通知する
    problem = None
    if str(b.get("status", "")).upper() in ("STOPPED", "FAILED"):
        problem = "Zoho 側のバックアップの状態が %s です(CRM の画面で予定・結果を確認すること)" % b.get("status")
    try:
        _crm_download(ctx, base)
    except NotReady:
        if not problem:
            raise
    finally:
        if problem:
            ctx.log("  " + problem)
    if problem:
        raise BackupError(problem)


def _crm_download(ctx, base):
    svc = ctx.svc("crm")
    status, js = ctx.http.request("GET", base + "/urls", ok=(200, 204))
    urls = js.get("urls") or {}
    expiry = urls.get("expiry_date")
    if status == 204 or not expiry:
        raise NotReady("ダウンロードできるバックアップがまだありません")
    st = ctx.state("crm")
    if st.get("expiry_date") == expiry:
        ctx.log("  取得済み(期限 %s のもの。%s に取得)" % (expiry, st.get("downloaded")))
        return
    exp = datetime.datetime.fromisoformat(expiry)
    if exp < datetime.datetime.now(exp.tzinfo):
        # 期限後も取れることがあるので試す(取れなければ失敗として通知される)
        ctx.log("  注意: ダウンロードの期限(%s)を過ぎています。取得を試みます" % expiry)

    data = urls.get("data_links") or []
    if not data:
        raise BackupError("data_links が空です: %s" % short(json.dumps(js)))
    files = []
    for i, u in enumerate(data, 1):
        dest = ctx.path("CRM", "" if i == 1 else "_Data_%03d" % i)  # 1 つ目は YYYYMMDD_CRM.zip
        ctx.fetch("GET", u, dest)
        files.append(os.path.basename(dest))
    if yes(svc.get("attachments"), True):
        for i, u in enumerate(urls.get("attachment_links") or [], 1):
            dest = ctx.path("CRM", "_Attachments_%03d" % i)
            ctx.fetch("GET", u, dest)
            files.append(os.path.basename(dest))
    ctx.save_state("crm", {"expiry_date": expiry, "downloaded": ctx.today, "files": files})


# ------------------------------------------------------------------ 共通(複数のファイルを 1 つの ZIP にまとめる)

def yes(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("yes", "true", "1", "on")


def due(ctx, name):
    """前回の取得から interval_days 日以上たったか(Zoho 側の回数制限・API の消費を抑えるため)。"""
    interval = int(ctx.svc(name).get("interval_days", 7))
    last = ctx.state(name).get("downloaded")
    if not last:
        return True
    days = (datetime.date.today() - datetime.datetime.strptime(last, "%Y%m%d").date()).days
    if days < interval:
        ctx.log("  前回 %s に取得済み(%d 日ごと。次は %d 日後)" % (last, interval, interval - days))
        return False
    return True


def stale(ctx, name):
    """最後に取得した日から alert_days 日を超えていれば、その旨を返す。
    既定は CRM が 16 日(Zoho 側の予定が 2 週間ごとのため)、それ以外は interval_days + 2 日。"""
    svc = ctx.svc(name)
    default = 16 if name == "crm" else int(svc.get("interval_days", 7)) + 2
    limit = int(svc.get("alert_days", default))
    last = ctx.state(name).get("downloaded")
    if not last:
        return None  # 初回(まだ一度も取得していない)
    days = (datetime.date.today() - datetime.datetime.strptime(last, "%Y%m%d").date()).days
    if days > limit:
        return "最後の取得(%s)から %d 日たっています(alert_days = %d)。新しいバックアップが取れていません" % (last, days, limit)
    return None


def newest_baseline(conf, label):
    """baseline_dir の YYYYMMDD_<label>.zip のうち最新のものの名前(無ければ None)。"""
    pat = re.compile(r"^(\d{8})_%s\.zip$" % re.escape(label), re.I)
    try:
        names = sorted((n for n in os.listdir(conf["baseline_dir"]) if pat.match(n)), reverse=True)
    except FileNotFoundError:
        names = []
    return names[0] if names else None


def baseline(ctx, label):
    """baseline_dir の YYYYMMDD_<label>.zip のうち最新のものと、その日の 0:00(この日以降に変わった分を取る)を返す。
    画面のバックアップは、取得した日の途中までのデータを含むので、その日の 0:00 からを取れば漏れない(重なりは許す)。
    無ければ (None, None) = 全件を取る。"""
    d = ctx.conf["baseline_dir"]
    name = newest_baseline(ctx.conf, label)
    if not name:
        ctx.log("  起点なし(%s に YYYYMMDD_%s.zip が無い): 全件を取る" % (d, label))
        return None, None
    names = [name]
    day = datetime.datetime.strptime(names[0][:8], "%Y%m%d")
    since = day.astimezone()  # このサーバの時刻の 0:00
    ctx.log("  起点: %s(%s 以降に変わった分を取る)" % (names[0], since.isoformat()))
    return os.path.join(d, names[0]), since


def parse_time(v):
    """Books・Desk の時刻("2026-10-22T10:00:00+0900"・"2026-10-22T01:00:00.000Z")を読む。読めなければ None。"""
    if not v:
        return None
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(v).strip())
    v = re.sub(r"Z$", "+00:00", v)  # python 3.10 の fromisoformat は Z を読めない
    try:
        t = datetime.datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


class ZipOut:
    """<dest>.part に書き、検証してから dest に置き換える。"""

    def __init__(self, ctx, dest):
        self.ctx, self.dest, self.part = ctx, dest, dest + ".part"
        self.z = None
        if not ctx.dry_run:
            if os.path.exists(dest):
                raise BackupError("同じ名前のファイルが既にあります(上書きしない): %s" % dest)
            check_free(ctx.conf)
            self.z = zipfile.ZipFile(self.part, "w", zipfile.ZIP_DEFLATED, allowZip64=True)

    def add_json(self, name, obj):
        if self.z:
            self.z.writestr(name, json.dumps(obj, ensure_ascii=False, indent=1))

    def add_bytes(self, name, data):
        if self.z:
            self.z.writestr(name, data)

    def add_file(self, name, path):
        if self.z:
            self.z.write(path, name)

    def close(self):
        if not self.z:
            self.ctx.log("  (dry-run) %s を保存する" % os.path.basename(self.dest))
            return
        self.z.close()
        os.chmod(self.part, 0o640)
        check_zip(self.part)
        os.replace(self.part, self.dest)
        size = os.path.getsize(self.dest)
        h = hashlib.sha256()
        with open(self.dest, "rb") as f:
            for buf in iter(lambda: f.read(CHUNK), b""):
                h.update(buf)
        self.ctx.log("  保存: %s(%s バイト, sha256 %s)" % (os.path.basename(self.dest), format(size, ","), h.hexdigest()))
        with open(os.path.join(self.ctx.conf["state_dir"], "manifest.tsv"), "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%d\t%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"),
                                         self.dest, size, h.hexdigest()))

    def abort(self):
        if self.z:
            self.z.close()
            if os.path.exists(self.part):
                os.unlink(self.part)


def find(js, key):
    """応答のトップレベルか details の中にある key を返す(Projects の応答は版により位置が違う)。"""
    if key in js:
        return js[key]
    return (js.get("details") or {}).get(key)


# ------------------------------------------------------------------ Projects

def backup_projects(ctx):
    """Projects のポータルのデータバックアップ(V3 の portal_export)。作成の依頼 → 完了を待つ → ダウンロード。
    Zoho 側の制限: 24 時間に 1 回(24 時間以内に作成済みなら、既存のものの情報が返る)、ファイルは約 15 日で削除。
    scope: ZohoProjects.portals.READ と ZohoFiles.files.READ(または .ALL)。Portal Owner のトークンであること。"""
    svc = ctx.svc("projects")
    portal = svc.get("portal_id")
    if not portal:
        raise BackupError("[projects] portal_id が未設定です")
    base = "%s/api/v3/portal/%s/portal_export" % (svc.get("api_url", "https://projectsapi.zoho.jp").rstrip("/"), portal)
    if ctx.check_only:
        _, js = ctx.http.request("GET", base + "/history")
        hist = js.get("portal_export_history") or []
        for h in hist[:3]:
            ctx.log("  履歴: %s %s %s 期限 %s" % (h.get("scheduled_on"), h.get("status"),
                                                   h.get("filesize"), h.get("link_expires_on")))
        ctx.log("  履歴 %d 件(読み取りのみ。作成の依頼はしない)" % len(hist))
        return
    if not due(ctx, "projects"):
        return
    st = ctx.state("projects")
    if ctx.dry_run:
        ctx.log("  (dry-run) portal_export の作成を依頼して、完了後にダウンロードする")
        return

    _, js = ctx.http.request("POST", base, ok=(200, 201, 202))
    export_id = find(js, "export_id")
    ctx.log("  依頼: export_id %s / %s / 期限 %s" % (export_id, find(js, "message"), find(js, "link_expires_on")))
    if not export_id:
        raise BackupError("export_id が返りません: %s" % short(json.dumps(js, ensure_ascii=False)))
    if st.get("export_id") == export_id:
        ctx.log("  この export_id は取得済み(%s)。24 時間後に作成できるようになる" % st.get("downloaded"))
        return

    url = find(js, "download_api_url")
    deadline = time.time() + int(svc.get("wait_minutes", 60)) * 60
    while True:
        # 作成中は HTTP 202(PORTAL_EXPORT_IN_PROGRESS)が返る
        _, s = ctx.http.request("GET", "%s/%s/status" % (base, export_id), ok=(200, 202))
        msg = str(find(s, "message") or find(s, "status") or "")
        url = find(s, "download_api_url") or url
        if "COMPLETED" in msg.upper():
            break
        if "FAIL" in msg.upper():
            raise BackupError("Zoho 側で作成に失敗: %s" % short(json.dumps(s, ensure_ascii=False)))
        if time.time() > deadline:
            raise NotReady("作成が終わっていません(%s)。次回の実行で取得する" % msg)
        time.sleep(60)
    if not url:
        raise BackupError("download_api_url が返りません")
    ctx.fetch("GET", url, ctx.path("PRJ"))
    ctx.save_state("projects", {"export_id": export_id, "downloaded": ctx.today})


# ------------------------------------------------------------------ Books

# 一覧の API で取れるもの: 名前 → (パス, 応答の一覧のキー, 1 件の ID のキー)。
# 公式の資料(https://www.zoho.com/books/api/v3/)で確認。パスと応答のキーがモジュール名と違うものがある
BOOKS_TABLE = {
    "contacts": ("/contacts", "contacts", "contact_id"),
    "items": ("/items", "items", "item_id"),
    "invoices": ("/invoices", "invoices", "invoice_id"),
    "recurringinvoices": ("/recurringinvoices", "recurring_invoices", "recurring_invoice_id"),
    "creditnotes": ("/creditnotes", "creditnotes", "creditnote_id"),
    "customerpayments": ("/customerpayments", "customer_payments", "payment_id"),
    "estimates": ("/estimates", "estimates", "estimate_id"),
    "salesorders": ("/salesorders", "salesorders", "salesorder_id"),
    "purchaseorders": ("/purchaseorders", "purchaseorders", "purchaseorder_id"),
    "bills": ("/bills", "bills", "bill_id"),
    "recurringbills": ("/recurringbills", "recurringbills", "recurring_bill_id"),
    "vendorcredits": ("/vendorcredits", "vendorcredits", "vendor_credit_id"),
    "vendorpayments": ("/vendorpayments", "vendorpayments", "payment_id"),
    "expenses": ("/expenses", "expenses", "expense_id"),
    "recurringexpenses": ("/recurringexpenses", "recurring_expenses", "recurring_expense_id"),
    "journals": ("/journals", "journals", "journal_id"),
    "chartofaccounts": ("/chartofaccounts", "chartofaccounts", "account_id"),
    "bankaccounts": ("/bankaccounts", "bankaccounts", "account_id"),
    "banktransactions": ("/banktransactions", "banktransactions", "transaction_id"),
    "taxes": ("/settings/taxes", "taxes", "tax_id"),
    "currencies": ("/settings/currencies", "currencies", "currency_id"),
    "users": ("/users", "users", "user_id"),
    "projects": ("/projects", "projects", "project_id"),
}
BOOKS_MODULES = ",".join(BOOKS_TABLE)


def backup_books(ctx):
    """Books には全体のバックアップの API が無い(画面の Settings → Data Backup のみ)。
    代わりに、各モジュールの一覧の API を全ページ取得し、JSON にして 1 つの ZIP にまとめる。
    detail_modules に書いたモジュールは、明細(行)を含めるため 1 件ずつ詳細も取る。起点(画面のバックアップ。
    baseline_dir の YYYYMMDD_Books.zip)があれば、その日以降に更新されたものだけ取る(それ以前の明細は起点にある)。
    復元に使うのは、起点 + 最新の ZIP(一覧は毎回全件、明細は起点からの差分を毎回すべて含む)。
    scope: ZohoBooks の 15 種(contacts・settings・invoices など)の .READ(README の 4)。
    API の制限: 1 分 100 回・1 日 1000〜10000 回(版による)。"""
    svc = ctx.svc("books")
    org = svc.get("organization_id")
    base = svc.get("api_url", ctx.conf["api_domain"] + "/books/v3").rstrip("/")
    if ctx.check_only or not org:
        baseline(ctx, "Books")
        _, js = ctx.http.request("GET", base + "/organizations")
        for o in js.get("organizations") or []:
            ctx.log("  組織: organization_id %s %s" % (o.get("organization_id"), o.get("name")))
        if not org:
            raise BackupError("[books] organization_id が未設定です(上の一覧から設定する)")
        return
    if not due(ctx, "books"):
        return
    modules = [m.strip() for m in svc.get("modules", BOOKS_MODULES).split(",") if m.strip()]
    unknown = [m for m in modules if m not in BOOKS_TABLE]
    if unknown:
        raise BackupError("[books] modules に未知の名前があります: %s" % ", ".join(unknown))
    detail = {m.strip() for m in svc.get("detail_modules", "").split(",") if m.strip()}
    base_file, since = baseline(ctx, "Books") if detail else (None, None)
    budget = int(svc.get("max_requests", 1500))
    pause = 60.0 / int(svc.get("requests_per_minute", 80))
    used = [0]

    def get(path, params):
        if used[0] >= budget:
            raise BackupError("API の回数が max_requests(%d)に達したので中止" % budget)
        used[0] += 1
        time.sleep(pause)
        p = dict(params, organization_id=org)
        return ctx.http.request("GET", base + path, params=p)[1]

    out = ZipOut(ctx, ctx.path("BOOKS"))
    try:
        summary = {}
        for m in modules:
            path, key, idkey = BOOKS_TABLE[m]
            rows, page = [], 1
            while True:
                js = get(path, {"page": page, "per_page": 200})
                rows.extend(js.get(key) or [])
                if not (js.get("page_context") or {}).get("has_more_page"):
                    break
                page += 1
            out.add_json("%s.json" % m, rows)
            summary[m] = len(rows)
            fetched = 0
            if m in detail:
                for r in rows:
                    rid = r.get(idkey)
                    if not rid:
                        continue
                    t = parse_time(r.get("last_modified_time"))
                    if since and t and t < since:
                        continue  # 起点にある
                    out.add_json("%s/%s.json" % (m, rid), get("%s/%s" % (path, rid), {}))
                    fetched += 1
            ctx.log("  %s: %d 件%s" % (m, len(rows), "(明細 %d 件)" % fetched if m in detail else ""))
        out.add_json("_summary.json", {"date": ctx.today, "organization_id": org, "counts": summary,
                                       "requests": used[0],
                                       "baseline": os.path.basename(base_file) if base_file else None,
                                       "details_since": since.isoformat() if since else None})
        out.close()
    except BaseException:
        out.abort()
        raise
    ctx.log("  API の回数: %d" % used[0])
    ctx.save_state("books", {"downloaded": ctx.today, "counts": summary})


# ------------------------------------------------------------------ Desk

DESK_MODULES = "tickets,contacts,accounts,tasks,calls,events,timeEntry,solutions,products,departments,agents,teams"


def backup_desk(ctx):
    """Desk には全体のバックアップの API が無い(画面の Data Backup のみ。主連絡先だけが実行でき、30 日に 1 回)。
    代わりに Bulk Export の API で、モジュールごとに出力して、1 つの ZIP にまとめる。
    起点(画面のバックアップ。baseline_dir の YYYYMMDD_Desk.zip)があれば、modifiedAfter でその日以降に変わった分だけ出す
    (削除・ゴミ箱・スパムにしたものも別の CSV で入る)。復元に使うのは、起点 + 最新の ZIP。
    チケットのスレッド(メールの本文)・コメントは Bulk Export の対象外なので、desk_threads で別に取る(threads = yes)。
    添付ファイルの本体は含まれない。ダウンロードは CSV の ZIP。scope: Desk.exports・Desk.basic.READ。全 API に orgId ヘッダーが必要。
    制限: モジュールごとに 1 日 20 回、1 回 10 万件まで(超える分は exportId で続きを取る)、開始 1 回 500 クレジット。"""
    svc = ctx.svc("desk")
    base = svc.get("api_url", "https://desk.zoho.jp").rstrip("/") + "/api/v1"
    org = svc.get("org_id")
    if ctx.check_only or not org:
        baseline(ctx, "Desk")
        _, js = ctx.http.request("GET", base + "/organizations")
        for o in js.get("data") or []:
            ctx.log("  組織: org_id %s %s" % (o.get("id"), o.get("companyName")))
        if not org:
            raise BackupError("[desk] org_id が未設定です(上の一覧から設定する)")
        return
    if not due(ctx, "desk"):
        return
    hdr = {"orgId": str(org)}
    modules = [m.strip() for m in svc.get("modules", DESK_MODULES).split(",") if m.strip()]
    base_file, since = baseline(ctx, "Desk")
    run_at = datetime.datetime.now().astimezone()
    if ctx.dry_run:
        # 出力の開始だけでクレジットを使うので、dry-run では API を呼ばない
        ctx.log("  (dry-run) %s を Bulk Export して %s を保存する" % (",".join(modules), os.path.basename(ctx.path("DESK"))))
        return
    work = os.path.join(ctx.conf["state_dir"], "work")
    os.makedirs(work, mode=0o750, exist_ok=True)
    out = ZipOut(ctx, ctx.path("DESK"))
    try:
        for m in modules:
            export_id, part = None, 1
            while True:
                body = {"module": m, "limitPerFile": 100000}
                if since:
                    body["modifiedAfter"] = since.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                if export_id:
                    body["exportId"] = export_id
                _, js = ctx.http.request("POST", base + "/bulkExport", headers=dict(hdr, **{"Content-Type": "application/json"}),
                                         data=json.dumps(body).encode(), ok=(200, 201, 202))
                export_id = js.get("exportId") or export_id
                if not export_id:
                    raise BackupError("%s: exportId が返りません: %s" % (m, short(json.dumps(js))))
                deadline = time.time() + int(svc.get("wait_minutes", 60)) * 60
                while True:
                    _, s = ctx.http.request("GET", "%s/bulkExport/%s" % (base, export_id), headers=hdr)
                    status = str(s.get("status", "")).upper()
                    if status == "COMPLETED":
                        break
                    if "FAIL" in status or status in ("SUSPENDED", "DELETED"):
                        raise BackupError("%s: Zoho 側で失敗: %s" % (m, short(json.dumps(s))))
                    if time.time() > deadline:
                        raise BackupError("%s: 出力が終わりません(%s)" % (m, status))
                    time.sleep(30)
                tmp = os.path.join(work, "%s_%03d" % (m, part))
                if os.path.exists(tmp):
                    os.unlink(tmp)
                ctx.http.download("GET", "%s/downloadBulkExportFile?exportId=%s" % (base, urllib.parse.quote(str(export_id))),
                                  tmp, expect_zip=False, headers=hdr)
                ext = ".zip" if zipfile.is_zipfile(tmp) else ".csv"
                out.add_file("%s/%s_%03d%s" % (m, m, part, ext), tmp)
                os.unlink(tmp)
                ctx.log("  %s: %d 個目を取得" % (m, part))
                if str(s.get("hasNextPage")).lower() != "true":  # 文字列 "false" で返ることがある
                    break
                part += 1
        threads = desk_threads(ctx, base, hdr, since, base_file, out) if yes(svc.get("threads"), True) and since else None
        out.add_json("_summary.json", {"date": ctx.today, "run_at": run_at.isoformat(timespec="seconds"),
                                       "org_id": org, "modules": modules, "threads": threads,
                                       "baseline": os.path.basename(base_file) if base_file else None,
                                       "modified_after": since.isoformat() if since else None})
        out.close()
    except BaseException:
        out.abort()
        raise
    ctx.save_state("desk", {"downloaded": ctx.today, "modules": modules,
                            "baseline": os.path.basename(base_file) if base_file else None})


def desk_threads(ctx, base, hdr, since, base_file, out):
    """起点の日以降に更新されたチケットの、スレッド(メールの本文)とコメントを取る。
    ZIP の threads/<チケット番号>_<ID>/ に ticket.json・comments.json・<スレッド ID>.json(本文)・
    <スレッド ID>.eml(メールの原文。添付ファイルも含む)を入れる。
    スレッドは一度できたら変わらないので、前回の DESK.zip(同じ起点のもの)にあるものはそこから写し、
    前回以降に更新されていないチケットは丸ごと写す(API の消費と時間を抑える)。
    API: 検索(Desk.search.READ)で更新日時の範囲を指定してチケットを探す(1 回の範囲で 5,000 件まで)。
    スレッド・コメント(Desk.tickets.READ)。クレジット: 一覧 3、1 件 1。"""
    svc = ctx.svc("desk")
    budget = int(svc.get("max_requests", 20000))
    size = int(svc.get("page_size", 100))
    used = [0]

    def get(path, params=None, ok=(200,)):
        if used[0] >= budget:
            raise BackupError("Desk の API の回数が max_requests(%d)に達したので中止" % budget)
        used[0] += 1
        return ctx.http.request("GET", base + path, params=params, headers=hdr, ok=ok)

    # 前回の DESK.zip(同じ起点のもの)
    prev, prev_at = None, None
    pat = re.compile(r"^(\d{8})_DESK\.zip$")
    for n in sorted((n for n in os.listdir(ctx.conf["backup_dir"]) if pat.match(n)), reverse=True):
        p = os.path.join(ctx.conf["backup_dir"], n)
        if p == ctx.path("DESK"):
            continue
        try:
            z = zipfile.ZipFile(p)
            sm = json.loads(z.read("_summary.json"))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        if sm.get("baseline") == os.path.basename(base_file) and sm.get("threads") and sm.get("run_at"):
            prev, prev_at = z, datetime.datetime.fromisoformat(sm["run_at"])
            ctx.log("  スレッド: 前回 %s(%s)から変わっていない分は写す" % (n, sm["run_at"]))
        break
    prev_names = set(prev.namelist()) if prev else set()

    # 起点以降に更新されたチケット(7 日ごとに区切って検索。1 回の範囲で 5,000 件まで)
    tickets = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    t0 = since.astimezone(datetime.timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    while t0 < now:
        t1 = min(t0 + datetime.timedelta(days=7), now)
        start = 0
        while True:
            status, js = get("/tickets/search", {"modifiedTimeRange": "%s,%s" % (t0.strftime(fmt), t1.strftime(fmt)),
                                                 "sortBy": "modifiedTime", "from": start, "limit": size}, ok=(200, 204))
            rows = js.get("data") or [] if status == 200 else []
            for r in rows:
                tickets[str(r["id"])] = r
            if len(rows) < size:
                break
            start += size
            if start >= 5000:
                raise BackupError("チケットの検索が 1 回の範囲(%s〜)で 5,000 件を超えました" % t0.strftime(fmt))
        t0 = t1

    fetched = copied = 0
    for tid, t in sorted(tickets.items()):
        d = "threads/%s_%s/" % (t.get("ticketNumber", ""), tid)
        out.add_json(d + "ticket.json", t)
        mt = parse_time(t.get("modifiedTime"))
        if prev and mt and prev_at and mt < prev_at and (d + "comments.json") in prev_names:
            for n in prev_names:
                if n.startswith(d) and n != d + "ticket.json":
                    out.add_bytes(n, prev.read(n))
            copied += 1
            continue
        # ページの境目で同じものが 2 回返ることがある(取得中に新しいスレッドが増えたときなど)ので ID で重複を除く
        start, ths = 0, {}
        while True:
            status, js = get("/tickets/%s/threads" % tid, {"from": start, "limit": size}, ok=(200, 204))
            rows = js.get("data") or [] if status == 200 else []
            for r in rows:
                ths.setdefault(str(r["id"]), r)
            if len(rows) < size:
                break
            start += size
        for hid, th in ths.items():
            for ext in (".json", ".eml"):
                if d + hid + ext in prev_names:
                    out.add_bytes(d + hid + ext, prev.read(d + hid + ext))
            if d + hid + ".json" in prev_names:
                continue
            _, full = get("/tickets/%s/threads/%s" % (tid, hid), {"include": "plainText"})
            out.add_json(d + hid + ".json", full)
            if str(th.get("channel", "")).upper() == "EMAIL":
                _, orig = get("/tickets/%s/threads/%s/originalContent" % (tid, hid), ok=(200, 204))
                if orig.get("content"):
                    out.add_bytes(d + hid + ".eml", orig["content"].encode("utf-8", "surrogateescape"))
        start, cms = 0, {}
        while True:
            status, js = get("/tickets/%s/comments" % tid, {"from": start, "limit": min(size, 100)}, ok=(200, 204))
            rows = js.get("data") or [] if status == 200 else []
            for r in rows:
                cms.setdefault(str(r.get("id")), r)
            if len(rows) < min(size, 100):
                break
            start += min(size, 100)
        out.add_json(d + "comments.json", list(cms.values()))
        fetched += 1
    ctx.log("  スレッド: チケット %d 件(取得 %d、前回から写す %d)、API %d 回" % (len(tickets), fetched, copied, used[0]))
    return {"tickets": len(tickets), "fetched": fetched, "copied": copied, "requests": used[0]}


# ------------------------------------------------------------------ 世代の整理

def rotate(conf, dry_run, log):
    """保存先の YYYYMMDD_<種別>*.zip を、種別ごとに新しい日付から keep_generations 日分だけ残す。
    名前が完全に一致するファイルだけを対象にし、シンボリックリンク・ディレクトリには触れない。"""
    d = conf["backup_dir"]
    names = os.listdir(d)
    for name, tag in TAGS.items():
        keep = conf["services"][name].get("keep_generations", conf["keep_generations"])
        pat = re.compile(r"^(\d{8})_%s(_[A-Za-z0-9_]+)?\.zip$" % tag)
        by_date = {}
        for n in names:
            m = pat.match(n)
            if m and os.path.isfile(os.path.join(d, n)) and not os.path.islink(os.path.join(d, n)):
                by_date.setdefault(m.group(1), []).append(n)
        dates = sorted(by_date, reverse=True)
        for old in dates[keep:]:
            for n in sorted(by_date[old]):
                log("  削除%s: %s" % ("(dry-run)" if dry_run else "", n))
                if not dry_run:
                    os.unlink(os.path.join(d, n))
    # 中断で残った書きかけ(1 日以上前のもの)
    for n in names:
        p = os.path.join(d, n)
        if n.endswith(".zip.part") and os.path.isfile(p) and time.time() - os.path.getmtime(p) > 86400:
            log("  書きかけを削除%s: %s" % ("(dry-run)" if dry_run else "", n))
            if not dry_run:
                os.unlink(p)


# ------------------------------------------------------------------ 通知

def send_mail(conf, subject, body):
    m = conf["mail"]
    if not m.get("mail_to"):
        return
    msg = EmailMessage()
    msg["Subject"] = "%s %s" % (m.get("subject_prefix", "[zoho-backup]"), subject)
    msg["From"] = m.get("mail_from", "zoho-backup@%s" % socket.getfqdn())
    msg["To"] = m["mail_to"]
    msg.set_content(redact(body))
    with smtplib.SMTP(m.get("smtp_host", "localhost"), int(m.get("smtp_port", 25)), timeout=60) as s:
        s.send_message(msg)


# ------------------------------------------------------------------ 起点の取り直しのお知らせ

REMIND_TEXT = """Books・Desk のバックアップは、画面から手で取った全体のバックアップ(起点)からの差分で取っています。
起点から日がたつほど毎週の差分が大きくなり、Desk のメールの本文の一部(起点より前のチケットの古いスレッド)は起点にしか無いため、
定期的に取り直してください。

{items}

■ 取り直しの手順
1. 各サービスの管理者で Zoho にログインし、画面からバックアップを取る(ダウンロードのリンクがメールで届く)
   - Books: 設定 → データのバックアップ(15 日に 1 回まで)
     {books_url}
   - Desk: 設定 → データ管理 → データのバックアップ(30 日に 1 回まで。主連絡先のみ)
     {desk_url}
2. ダウンロードした ZIP を、このサーバの {baseline_dir}/ に YYYYMMDD_Books.zip・YYYYMMDD_Desk.zip(取った日付)の名前で置く(0440)。
   置けば次回の取得から新しい起点が使われ、このお知らせも止まる

このお知らせは、取り直すまで {interval} 日ごとに届きます(取り直しの目安: 起点から {limit} 日)。
"""


def baseline_reminder(conf, dry_run, log):
    """Books・Desk(有効なもの)の最新の起点が remind_days 日を超えたら、お知らせのメールを送る(remind_interval_days 日ごと)。
    失敗の通知とは別(バックアップ自体は続いている)。"""
    g = conf["general"]
    limit, interval = int(g.get("baseline_remind_days", 90)), int(g.get("baseline_remind_interval_days", 7))
    today = datetime.date.today()
    items = []
    for name, label in (("books", "Books"), ("desk", "Desk")):
        if not conf["services"][name]["enabled"]:
            continue
        n = newest_baseline(conf, label)
        if not n:
            items.append("- %s: 起点がありません" % label)
            continue
        age = (today - datetime.datetime.strptime(n[:8], "%Y%m%d").date()).days
        if age >= limit:
            items.append("- %s: 最新の起点 %s から %d 日たっています" % (label, n, age))
    if not items:
        return
    p = os.path.join(conf["state_dir"], "reminder.json")
    try:
        with open(p, encoding="utf-8") as f:
            last = json.load(f).get("sent")
    except (OSError, ValueError):
        last = None
    if last and (today - datetime.datetime.strptime(last, "%Y%m%d").date()).days < interval:
        log("[起点] 取り直しの時期(%s に通知済み)" % last)
        return
    log("[起点] 取り直しの時期のお知らせを送る%s: %s" % ("(dry-run)" if dry_run else "", " / ".join(items)))
    if dry_run:
        return
    send_mail(conf, "お知らせ: Books・Desk の全体のバックアップを取り直す時期です",
              REMIND_TEXT.format(items="\n".join(items), baseline_dir=conf["baseline_dir"], interval=interval, limit=limit,
                                 books_url=conf["services"]["books"].get("backup_page_url", ""),
                                 desk_url=conf["services"]["desk"].get("backup_page_url", "")))
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"sent": today.strftime("%Y%m%d")}, f)


# ------------------------------------------------------------------ 実行

BACKUP_FUNCS = {}  # サービス名 → 関数(下で登録)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=DEFAULT_CONF)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("command", choices=("run", "check", "rotate", "test-mail"))
    ap.add_argument("services", nargs="*", metavar="SERVICE", help=" / ".join(SERVICES))
    args = ap.parse_args()
    args.service_given = bool(args.services)  # 手でサービスを指定したときは、お知らせを送らない
    for s in args.services:
        if s not in SERVICES:
            ap.error("サービスは %s のいずれか: %s" % (", ".join(SERVICES), s))

    os.umask(0o027)
    lines = []

    def log(s):
        s = redact(s)
        lines.append(s)
        print(s, flush=True)

    conf = load_config(args.config)
    if args.command == "test-mail":
        send_mail(conf, "試験のメール", "zoho-backup からの試験のメールです(%s)。" % socket.getfqdn())
        return 0

    os.makedirs(conf["state_dir"], mode=0o750, exist_ok=True)
    lock = open(os.path.join(conf["state_dir"], "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("別の zoho-backup が実行中です", file=sys.stderr)
        return 1

    if args.command == "rotate":
        rotate(conf, args.dry_run, log)
        return 0

    targets = args.services or [s for s in SERVICES if conf["services"][s]["enabled"]]
    failed, notready, done = [], [], []
    log("zoho-backup %s 開始 %s(%s)%s" % (args.command, datetime.datetime.now().isoformat(timespec="seconds"),
                                         ", ".join(targets), " dry-run" if args.dry_run else ""))
    http = Http(conf)
    try:
        http.get_token(load_credentials(conf["credentials"]))
    except Exception as e:
        log("トークンの取得に失敗: %s" % e)
        failed.append("token")
        targets = []
    ctx = Ctx(conf, http, args.dry_run or args.command == "check", log)
    ctx.check_only = args.command == "check"
    for name in targets:
        log("[%s]" % name)
        try:
            BACKUP_FUNCS[name](ctx)
            done.append(name)
        except NotReady as e:
            log("  未作成: %s" % e)
            notready.append(name)
        except Exception as e:
            log("  失敗: %s" % e)
            if not isinstance(e, BackupError):
                log(traceback.format_exc())
            failed.append(name)

        # 取得が止まったことに気づくため、最後の取得から alert_days 日を超えたら失敗として通知する
        msg = stale(ctx, name)
        if msg:
            log("  " + msg)
            if args.command == "run" and name not in failed:
                failed.append(name)

    if args.command == "run" and not failed:
        log("[rotate]")
        try:
            rotate(conf, args.dry_run, log)
        except Exception as e:
            log("  失敗: %s" % e)
            failed.append("rotate")

    log("終了: 成功 %s / 未作成 %s / 失敗 %s" % (",".join(done) or "-", ",".join(notready) or "-",
                                           ",".join(failed) or "-"))
    if args.command == "run" and not args.service_given:
        try:
            baseline_reminder(conf, args.dry_run, log)
        except Exception as e:
            log("[起点] お知らせを送れません: %s" % e)

    if args.command == "run" and not args.dry_run:
        notify_success = yes(conf["mail"].get("notify_success"))
        if failed or notify_success:
            try:
                send_mail(conf, ("失敗: %s" % ", ".join(failed)) if failed else "成功",
                          "\n".join(lines) + "\n\nホスト: %s\n" % socket.getfqdn())
            except Exception as e:
                print("メールを送れません: %s" % e, file=sys.stderr)
                return 2
    return 1 if failed else 0


BACKUP_FUNCS.update(crm=backup_crm, projects=backup_projects, books=backup_books, desk=backup_desk)

if __name__ == "__main__":
    sys.exit(main())
