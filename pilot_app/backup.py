"""Create a consistent SQLite backup, keep a sensible history, and push a copy offsite.

Three properties this file exists to protect, in order of how badly they bite:

1. **The backup must be transactionally correct.** It uses sqlite3's online backup
   API, never `cp`. Litestream's own documentation says it plainly ("Do not use
   `cp` to back up SQLite databases. It is not transactionally safe."), and the
   same API is what `.backup` / `VACUUM INTO` wrap.
2. **Retention must be by age, not by count.** It used to keep "the newest seven"
   -- and the pre-upgrade backup runs through *this same function*, so a day with
   seven deploys silently rotated out every daily copy. That is not theoretical:
   on 2026-09-14 the directory held seven deploy-time backups from one afternoon,
   and that morning's 03:20 daily was already gone.
3. **A copy has to leave the machine.** Everything here lives on one VPS; a dead
   disk takes the database and its backups together. The offsite push is optional
   and inert until an operator configures a target, because a self-hosted install
   must work with no third-party account at all.

What a backup is *not*: it does not contain the master key (`INFE_PILOT_MASTER_KEY`
lives in `pilot.env`, deliberately kept out of every backup). Without that key the
mailbox passwords and API keys inside are undecryptable, so an offsite copy of the
database alone is **not** a disaster recovery plan -- `--check` says so out loud
rather than letting the presence of backups imply more than it should.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


# How long a copy is worth keeping. Age rather than count: one function produces
# both the daily backups and the pre-upgrade ones, and a count cannot tell "a week
# of daily copies" apart from "one afternoon of deploys".
#
# 7 days rather than 14 (2026-09-18): the backups hold AES-GCM payloads, so they
# are **incompressible** (measured: gzip gets 1.0x), and the backup directory is
# therefore `copies x live size` -- the single largest consumer of disk on a small
# VPS. On a 40 GB disk with 100 accounts this moves "full in 26 months" to "full in
# 46 months" (`tools/disk_budget.py`). The window is a *recovery* window, not a
# retention promise about user data: the live database is never pruned, and the
# privacy policy now states 7 days in all three places that name it.
BACKUP_KEEP_DAYS = _int_env("INFE_PILOT_BACKUP_KEEP_DAYS", 7, 1, 3650)
# ...but never fewer than this, even when none are inside the window (a clock
# jump, or a database that is only written once a month).
BACKUP_KEEP_MIN = _int_env("INFE_PILOT_BACKUP_KEEP_MIN", 7, 1, 10_000)
# A ceiling, so a 100 MB database cannot quietly fill the disk with 14 days of
# copies. Whichever limit is reached first wins.
BACKUP_KEEP_MAX = _int_env("INFE_PILOT_BACKUP_KEEP_MAX", 60, 1, 100_000)

# Offsite target. WebDAV because it is one PUT over plain HTTP with basic auth,
# which the standard library does in twenty lines -- so this stays a program with
# no new dependency, no daemon and nothing to install. Any Nextcloud, Synology,
# 坚果云 or plain Apache/nginx WebDAV share works.
WEBDAV_URL_ENV = "INFE_PILOT_BACKUP_WEBDAV_URL"
WEBDAV_USER_ENV = "INFE_PILOT_BACKUP_WEBDAV_USER"
WEBDAV_PASSWORD_ENV = "INFE_PILOT_BACKUP_WEBDAV_PASSWORD"
OFFSITE_STATE_FILE = "offsite-state.json"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _human_size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KiB"
    return f"{count / (1024 * 1024):.1f} MiB"


def _human_age(moment: dt.datetime | None, now: dt.datetime) -> str:
    if moment is None:
        return "未知"
    seconds = max(0.0, (now - moment).total_seconds())
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} 小时前"
    return f"{seconds / 86400:.1f} 天前"


def safe_url(url: str) -> str:
    """A URL with any credential or token removed, for logs and alerts.

    Some WebDAV providers put an app password in the URL, some put a token in the
    query string. Printing either into a journal or an alert e-mail would leak
    exactly what the rest of this feature treats as secret.
    """
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = "?<已隐藏>" if parts.query else ""
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, query, ""))


def create_backup(source: Path, destination_dir: Path) -> Path:
    """One consistent copy. Raises if SQLite cannot produce it."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"pilot-{_stamp()}.sqlite3"
    with sqlite3.connect(source) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
    os.chmod(destination, 0o600)
    return destination


# The one definition of where a "back up now" request is left, shared by the code
# that writes it, the .path unit that watches for it, and the tests that pin the
# two together. This would otherwise be a *silent* failure: a path unit watching
# a file nobody writes loads fine, enables fine, and simply never runs anything.
REQUEST_FILE = "backup.request"
DEFAULT_DB = "/var/lib/cityu-mail-pilot/pilot.sqlite3"


def request_path(source: Path | None = None) -> Path:
    """Where a backup-on-demand request goes: beside the database.

    Deliberately not a privileged location. The worker runs with
    ``ProtectSystem=strict`` and may only write inside its own data directory, and
    that restriction is worth keeping: the worker holds the live database, and the
    backups are exactly what survives the worker being wrong.
    """
    database = source or Path(os.environ.get("INFE_PILOT_DB", DEFAULT_DB))
    return database.parent / REQUEST_FILE


def request_backup(source: Path | None = None) -> Path:
    """Ask for a backup by dropping a marker systemd is watching for.

    Returns the marker's path. The backup itself is performed by
    ``cityu-mail-pilot-backup.service`` -- the same audited unit the nightly timer
    runs, outside the worker's sandbox and with the paths it needs. Its failure is
    reported by that unit's ``OnFailure=`` and by the sentinel's freshness checks,
    not by this call, so the caller must describe the request rather than a result.
    """
    marker = request_path(source)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return marker


def prune(destination_dir: Path, *, now: dt.datetime | None = None) -> list[Path]:
    """Delete copies that are both old and beyond the floor. Returns what went."""
    now = now or _utc_now()
    copies = sorted(destination_dir.glob("pilot-*.sqlite3"),
                    key=lambda path: path.name, reverse=True)
    removed: list[Path] = []
    for index, path in enumerate(copies):
        if index >= BACKUP_KEEP_MAX:
            removed.append(path)
            continue
        if index < BACKUP_KEEP_MIN:
            continue
        try:
            made = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        except OSError:
            continue
        if (now - made).days > BACKUP_KEEP_DAYS:
            removed.append(path)
    for path in removed:
        try:
            path.unlink()
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# offsite
# ---------------------------------------------------------------------------


def webdav_config() -> dict[str, str] | None:
    """The target, with `user:password@` in the URL accepted as a fallback.

    Several providers hand out a URL that already contains the credentials, and
    that is a natural thing for an operator to paste. `urllib` refuses such a URL
    outright (`InvalidURL: nonnumeric port`), so the credentials are lifted out of
    it here and sent as a header instead -- the form people have works, and the
    password still never reaches a log line.
    """
    url = (os.environ.get(WEBDAV_URL_ENV) or "").strip()
    if not url:
        return None
    user = (os.environ.get(WEBDAV_USER_ENV) or "").strip()
    password = os.environ.get(WEBDAV_PASSWORD_ENV) or ""
    parts = urllib.parse.urlsplit(url)
    if parts.username and not user:
        user = urllib.parse.unquote(parts.username)
        password = urllib.parse.unquote(parts.password or "")
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        url = urllib.parse.urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return {"url": url.rstrip("/"), "user": user, "password": password}


def _put(url: str, payload: bytes, user: str, password: str, timeout: int = 120) -> None:
    request = urllib.request.Request(url, data=payload, method="PUT")
    request.add_header("Content-Type", "application/gzip")
    request.add_header("Content-Length", str(len(payload)))
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status not in (200, 201, 204):
            raise OSError(f"WebDAV 返回 HTTP {response.status}")


def _get(url: str, user: str, password: str, timeout: int = 120) -> bytes:
    """Read one file back off the WebDAV target. Never writes anything."""
    request = urllib.request.Request(url, method="GET")
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"WebDAV 返回 HTTP {response.status}")
        return response.read()


def verify_offsite(destination_dir: Path, *, name: str = "pilot-latest.sqlite3.gz",
                   now: dt.datetime | None = None,
                   fetch: Callable[[str, str, str], bytes] | None = None) -> dict[str, Any]:
    """Download the offsite copy and check it is real, complete, and ours.

    `--check` can only report what the *push* recorded, and a push that answered
    201 says one thing: the server accepted the bytes. It does not say the file is
    still there, that it is the whole file, or that it is a database rather than a
    truncated upload or an error page saved under our name. Only reading it back
    does -- which is what this does, and why it exists rather than an instruction
    to go and look at a web page.

    The comparison is deliberately byte-level against the **local** copies: the
    remote name is deterministic (`pilot-<day>.sqlite3.gz`, no listing needed), so
    "the remote file decompresses to exactly the bytes of local copy X" is a
    stronger statement than any timestamp comparison, and it needs no PROPFIND --
    the one WebDAV verb most likely to be locked down by a provider.
    """
    now = now or _utc_now()
    config = webdav_config()
    if config is None:
        return {"configured": False, "ok": False, "error": "", "name": name}
    fetch = fetch or _get
    result: dict[str, Any] = {"configured": True, "ok": False, "error": "", "name": name,
                             "target": safe_url(config["url"]), "at": now.isoformat(timespec="seconds")}
    try:
        raw = fetch(f"{config['url']}/{name}", config["user"], config["password"])
    except Exception as exc:  # noqa: BLE001 -- any failure here is "we could not read it back"
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result
    result["remote_bytes"] = len(raw)
    try:
        payload = gzip.decompress(raw)
    except Exception as exc:  # noqa: BLE001 -- not gzip at all
        result["error"] = f"远端那份不是 gzip：{type(exc).__name__}: {exc}"[:300]
        return result
    result["bytes"] = len(payload)
    result["sha256"] = hashlib.sha256(payload).hexdigest()
    if not payload.startswith(b"SQLite format 3\x00"):
        result["error"] = "远端那份解开后不是 SQLite 数据库（文件头不对）"
        return result

    matches: list[str] = []
    for path in sorted(destination_dir.glob("pilot-*.sqlite3"), key=lambda p: p.name, reverse=True):
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() == result["sha256"]:
                matches.append(path.name)
        except OSError:
            continue
    result["matches_local"] = matches
    result["integrity"] = _integrity_check(payload)
    result["ok"] = bool(matches) and result["integrity"] == "ok"
    if not matches:
        result["error"] = ("远端那份与本地现存任何一份都不同——可能是本地已按保留策略删掉了它，"
                           "也可能是另一台机器推上去的。")
    elif result["integrity"] != "ok":
        result["error"] = f"远端那份能解开、但数据库自检不通过：{result['integrity']}"
    return result


def _integrity_check(payload: bytes) -> str:
    """``PRAGMA integrity_check`` on the downloaded bytes, in a temp file.

    A file whose bytes match a local copy cannot be corrupt *and* match, so this
    only earns its keep when the match fails -- and then it is the difference
    between "not ours" and "ours, but damaged".
    """
    handle = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as box:
            box.write(payload)
            handle = Path(box.name)
        connection = sqlite3.connect(f"file:{handle}?mode=ro", uri=True, timeout=10)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        return str(row[0]) if row else "（没有返回）"
    except Exception as exc:  # noqa: BLE001 -- reported as a verdict, not raised
        return f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if handle is not None:
            try:
                handle.unlink()
            except OSError:
                pass



def push_offsite(database_file: Path, *, now: dt.datetime | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Upload a gzipped copy. Never raises; the local backup is what matters.

    Remote naming follows the rolling scheme in Litestream's cron guide -- the day
    of the month in the filename -- so the remote keeps about a month of copies
    and **no remote listing or deletion is needed**. That matters because WebDAV
    DELETE and PROPFIND are the two operations most likely to be locked down or to
    differ between providers, and a backup job that fails because it could not
    *clean up* would be a self-inflicted outage.
    """
    now = now or _utc_now()
    config = webdav_config()
    if config is None:
        # Same shape as the configured case: callers (and the alerting path) read
        # `ok` without first checking `configured`, and a KeyError in a backup
        # path is exactly the kind of failure that hides until it matters.
        return {"configured": False, "ok": False, "error": "", "bytes": 0}

    payload = gzip.compress(database_file.read_bytes(), compresslevel=6)
    names = [f"pilot-{now.strftime('%d')}.sqlite3.gz", "pilot-latest.sqlite3.gz"]
    result: dict[str, Any] = {
        "configured": True, "ok": False, "error": "", "bytes": len(payload),
        "target": safe_url(config["url"]), "at": now.isoformat(timespec="seconds"),
    }
    if dry_run:
        result.update(ok=True, dry_run=True, files=names)
        return result
    for name in names:
        try:
            _put(f"{config['url']}/{name}", payload, config["user"], config["password"])
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad. `http.client.InvalidURL`, a malformed header, a
            # TLS failure and a provider returning nonsense are all "the push did
            # not happen", and the local backup that this run already produced is
            # more important than the exception's class. The failure is recorded
            # in the state file, which is what the sentinel reports on.
            result["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return result
    result["ok"] = True
    result["files"] = names
    return result


def write_offsite_state(destination_dir: Path, result: dict[str, Any]) -> None:
    """Record the last attempt, so the sentinel can see a push that stopped."""
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / OFFSITE_STATE_FILE
        target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        os.chmod(target, 0o600)
    except OSError:
        pass


def read_offsite_state(destination_dir: Path) -> dict[str, Any] | None:
    try:
        raw = (destination_dir / OFFSITE_STATE_FILE).read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def newest_backup_age_hours(destination_dir: Path, *, now: dt.datetime | None = None) -> float | None:
    """How stale the newest local copy is, or None when there is none.

    Shared with the sentinel: "no backups at all" and "the newest is from three
    days ago" are the same emergency, and both must be visible without anybody
    opening a shell.
    """
    now = now or _utc_now()
    copies = sorted(destination_dir.glob("pilot-*.sqlite3"))
    if not copies:
        return None
    try:
        made = dt.datetime.fromtimestamp(copies[-1].stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None
    return (now - made).total_seconds() / 3600


def _local_summary(destination_dir: Path, now: dt.datetime) -> dict[str, Any]:
    copies = sorted(destination_dir.glob("pilot-*.sqlite3"),
                    key=lambda path: path.name, reverse=True)
    if not copies:
        return {"count": 0}
    newest, oldest = copies[0], copies[-1]
    try:
        stat = newest.stat()
    except OSError:
        return {"count": len(copies)}
    return {
        "count": len(copies),
        "newest": newest.name,
        "newest_age_hours": (now - dt.datetime.fromtimestamp(
            stat.st_mtime, dt.timezone.utc)).total_seconds() / 3600,
        "newest_bytes": stat.st_size,
        "oldest": oldest.name,
    }


def check(destination_dir: Path, master_key_present: bool, *,
          now: dt.datetime | None = None, env_file: Path | None = None,
          key_copy: dict[str, str] | None = None) -> int:
    """Print what is actually protected, and what is not. Exit 1 if unprotected.

    ``key_copy`` is what the operator last recorded with
    ``manage master-key-verified`` (``{"at": …, "fingerprint": …}``). The key
    itself is deliberately in no backup, so "do I still have the offline copy?"
    is the one question no machine can answer -- but "when did a human last say
    they checked?" can be recorded and aged, which is the difference between a
    single point of failure with a date on it and one nobody remembers.
    """
    now = now or _utc_now()
    local = _local_summary(destination_dir, now)
    problems: list[str] = []

    if not local.get("count"):
        print(f"本地备份：{destination_dir} 里一份都没有。")
        problems.append("本地没有备份")
    else:
        made = dt.datetime.fromtimestamp(
            (destination_dir / local["newest"]).stat().st_mtime, dt.timezone.utc)
        print(f"本地备份：{local['count']} 份，最新 {local['newest']}"
              f"（{_human_age(made, now)}，{_human_size(local['newest_bytes'])}），"
              f"最早 {local['oldest']}")
        print(f"          保留策略：{BACKUP_KEEP_DAYS} 天以内、最多 {BACKUP_KEEP_MAX} 份、"
              f"至少留 {BACKUP_KEEP_MIN} 份")
        if local["newest_age_hours"] > 36:
            problems.append("最新备份超过 36 小时")

    state = read_offsite_state(destination_dir)
    config = webdav_config()
    if config is None and not state:
        print("异地备份：**没有配置** —— 数据库和它的备份在同一台机器上，磁盘坏掉就一起没了。")
        print(f"          配置方法：在 pilot.env 里设 {WEBDAV_URL_ENV} 等三项，任何 WebDAV 空间都行。")
        problems.append("没有异地备份")
    else:
        target = safe_url(config["url"]) if config else str(state.get("target") or "（已移除）")
        if state is None:
            print(f"异地备份：已配置 {target}，但还没有成功推送过。")
            problems.append("异地从未成功推送")
        else:
            when = state.get("at") or ""
            if state.get("ok"):
                print(f"异地备份：{target} 最近一次成功 {when}"
                      f"（{_human_size(int(state.get('bytes') or 0))}）")
                # 推送成功只证明「服务器接受了这些字节」。内容还在不在、是不是完整的，
                # 只有读回来才知道——而这一步现在有命令，不必靠人去看网页。
                print("          （`--verify-offsite` 会把远端那份下载回来，对 sha256 并做数据库自检）")
            else:
                print(f"异地备份：{target} 最近一次**失败** {when}：{state.get('error') or '未知错误'}")
                problems.append("异地推送失败")

    if master_key_present:
        # The fingerprint is the point of this block: it lets the operator check
        # the copy in their password manager against the running server by reading
        # twelve characters, without the key ever being displayed or pasted.
        fingerprint = _master_key_fingerprint(env_file)
        print("主密钥：pilot.env 里有 INFE_PILOT_MASTER_KEY。"
              "**它不在任何备份里**——只备份数据库、没有它一样解不开，"
              "请确认你自己另有一份离线保存。")
        if fingerprint:
            print(f"        指纹 {fingerprint}（与你自己保存的那份对照；它不等于密钥本身）")
        print("        " + _key_copy_line(key_copy, now, fingerprint, problems))
    else:
        print("主密钥：**没找到 INFE_PILOT_MASTER_KEY**。没有它，数据库里的邮箱授权码与 "
              "API key 全部解不开，备份再多也没用。")
        problems.append("没有主密钥")

    print()
    if problems:
        print("需要处理：" + "；".join(problems))
        return 1
    print("结论：本地有新鲜备份、异地有推送、主密钥在你手上。")
    return 0


def key_copy_state(key_copy: dict[str, str] | None, now: dt.datetime, fingerprint: str,
                   ) -> tuple[str, str]:
    """Judge the offline copy of the master key. Returns ``(state, sentence)``.

    ``state`` is one of ``ok`` / ``missing`` / ``stale`` / ``mismatch`` /
    ``unreadable``. Two callers need this and they need the *same* answer:
    ``--check`` prints the sentence, and the sentinel turns the state into a
    finding. A second copy of these rules would let the command and the alert
    disagree about the one thing nobody can see from inside the machine -- whether
    the operator still holds a key that opens these backups.

    What is judged, and why exactly these three:

    * **no record, or a record older than a year** -- the copy is the one asset
      whose loss is invisible until the day it is needed;
    * **a fingerprint that is not the live one** -- that is what a restored or
      rotated master key looks like from here, and the copy on the shelf is then
      the wrong key for every backup in the directory. That one is not a
      reminder, it is a live data-loss risk, which is why the sentinel is allowed
      to mail about it while the first two only join the daily digest.
    """
    at = str((key_copy or {}).get("at") or "")
    recorded = str((key_copy or {}).get("fingerprint") or "")
    if recorded and fingerprint and recorded != fingerprint:
        return ("mismatch",
                f"离线副本：记录里核对过的是另一把钥匙（{recorded}），现在服务器上是 {fingerprint}"
                "——要么换过钥匙，要么恢复过一份旧备份；无论哪种，架子上那份都打不开现在的备份。")
    # 指纹必须**跟着句子走**。这句话会被印在 `backup --check` 里，也会作为一条发现项
    # 出现在管理后台的「巡检」面板里——而面板那一处没有别的地方能告诉他该拿什么去对。
    # 一句「请核对」而不给可比的东西，和「界面只说换个邮箱却没有按钮」是同一种死路。
    # 它是全项目唯一允许出现的密钥衍生值（截断哈希），`--check` 本来就在印它。
    how_to_compare = (f"对照当前指纹 {fingerprint}（它不等于密钥本身），对上之后在生产上跑 "
                      "`manage master-key-verified` 记下来" if fingerprint else
                      "跑 `manage backup --check` 看当前指纹（这一处读不到）")
    if not at:
        return ("missing",
                "离线副本：**没有任何核对记录**——主密钥不在任何备份里，丢了也不会有人知道。"
                f"拿你自己保存的那份，{how_to_compare}。")
    try:
        moment = dt.datetime.fromisoformat(at)
    except ValueError:
        return ("unreadable",
                f"离线副本：核对时间记录成了 {at!r}，读不出来——重新跑一次 `manage master-key-verified`。")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    days = (now - moment).days
    if days > 365:
        return ("stale", f"离线副本：上次核对是 {days} 天前（{at}）——**超过一年了**，"
                         f"现在就拿你自己保存的那份，{how_to_compare}。")
    return ("ok", f"离线副本：上次核对 {days} 天前（{at}），一年内再对一次即可。")


def _key_copy_line(key_copy: dict[str, str] | None, now: dt.datetime, fingerprint: str,
                   problems: list[str]) -> str:
    """One line about the offline copy for `--check`, and the failure it implies."""
    state, sentence = key_copy_state(key_copy, now, fingerprint)
    problem = {"missing": "主密钥离线副本从未核对",
               "stale": "离线副本超过一年没核对",
               "mismatch": "离线副本核对记录里的指纹与当前主密钥不一致",
               "unreadable": "离线副本核对时间读不出来"}.get(state)
    if problem:
        problems.append(problem)
    return sentence


def read_key_copy_check(database_file: Path) -> dict[str, str]:
    """What the operator recorded about the offline copy, or ``{}``.

    Read-only and best-effort: ``--check`` has to keep working on a host where
    the database is missing or locked (that is one of the states it exists to
    report), so a failure here means "no record", never an exception.
    """
    try:
        connection = sqlite3.connect(f"file:{database_file}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = dict(connection.execute(
            "SELECT key, value FROM app_settings WHERE key IN"
            " ('master_key_verified_at','master_key_verified_fingerprint')").fetchall())
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {"at": str(rows.get("master_key_verified_at") or ""),
            "fingerprint": str(rows.get("master_key_verified_fingerprint") or "")}


def _master_key_fingerprint(env_file: Path | None) -> str:
    """The live key's fingerprint, or "" when it cannot be read.

    Reads the value from the environment first (that is how the service runs) and
    falls back to the env file. Only the fingerprint is ever returned.
    """
    from .security import key_fingerprint

    value = (os.environ.get("INFE_PILOT_MASTER_KEY") or "").strip()
    if not value and env_file is not None and env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("INFE_PILOT_MASTER_KEY="):
                    value = line.split("=", 1)[1].strip()
                    break
        except OSError:
            return ""
    if not value:
        return ""
    try:
        return key_fingerprint(value)
    except Exception:  # noqa: BLE001 - a fingerprint must never break --check
        return ""


def report_offsite(result: dict[str, Any]) -> int:
    """Print the verdict of a read-back. Exit 1 when the offsite copy is not usable.

    Kept separate from :func:`verify_offsite` so the verdict can be asserted in a
    test without capturing stdout, and so the exit code has exactly one home: a
    check that reads a file back is only worth having if its answer can fail a
    script."""
    if not result.get("configured"):
        print("异地备份：**没有配置**，没有可核对的内容。")
        return 1
    print(f"异地内容核对：{result['target']} 上的 {result['name']}")
    if result.get("error") and not result.get("sha256"):
        print(f"  读不回来：{result['error']}")
        return 1
    print(f"  下载 {_human_size(int(result.get('remote_bytes') or 0))}"
          f"，解开后 {_human_size(int(result.get('bytes') or 0))}")
    print(f"  sha256（解开的）：{result.get('sha256') or '（没有）'}")
    matches = result.get("matches_local") or []
    if matches:
        print(f"  与本地逐字节相同：{'、'.join(matches)}")
    else:
        print("  与本地现存任何一份都不同。")
    print(f"  数据库自检：{result.get('integrity') or '（没有做）'}")
    if result.get("ok"):
        print("结论：异地那份是真的 —— 下载得回来、解得出、是完好的数据库、"
              "并且与本地某一份逐字节相同。")
        return 0
    print(f"结论：**这次核对没通过** —— {result.get('error') or '原因见上'}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库备份：本地 + 可选异地")
    parser.add_argument("--check", action="store_true",
                        help="只报告现状（本地/异地/主密钥），不备份")
    parser.add_argument("--no-offsite", action="store_true",
                        help="这次只做本地备份，不推送异地（升级前备份用得上）")
    parser.add_argument("--offsite-dry-run", action="store_true",
                        help="只打印会推送到哪里、多大，不真的上传")
    parser.add_argument("--verify-offsite", action="store_true",
                        help="把异地那份下载回来对 sha256 并做数据库自检（只读，不上传）")
    args = parser.parse_args(argv)

    source = Path(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
    destination_dir = Path(os.environ.get("INFE_PILOT_BACKUP_DIR", "/var/backups/cityu-mail-pilot"))
    env_file = Path(os.environ.get("INFE_PILOT_ENV_FILE", "/etc/cityu-mail-pilot/pilot.env"))

    if args.verify_offsite:
        return report_offsite(verify_offsite(destination_dir))

    if args.check:
        master_key = bool((os.environ.get("INFE_PILOT_MASTER_KEY") or "").strip())
        if not master_key and env_file.exists():
            try:
                master_key = "INFE_PILOT_MASTER_KEY=" in env_file.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                master_key = False
        return check(destination_dir, master_key, env_file=env_file,
                     key_copy=read_key_copy_check(source))

    if not source.exists():
        print(f"找不到数据库：{source}", file=sys.stderr)
        return 1

    destination = create_backup(source, destination_dir)
    removed = prune(destination_dir)
    print(f"backup created: {destination.name}"
          + (f"（清理了 {len(removed)} 份过期副本）" if removed else ""))

    if args.no_offsite:
        return 0

    result = push_offsite(destination, dry_run=args.offsite_dry_run)
    if not result.get("configured"):
        return 0
    if args.offsite_dry_run:
        print(f"[dry-run] 会推送到 {result['target']}："
              f"{'、'.join(result.get('files') or [])}（{_human_size(result['bytes'])}）")
        return 0
    write_offsite_state(destination_dir, result)
    if result.get("ok"):
        print(f"offsite ok: {result['target']}（{_human_size(result['bytes'])}）")
        return 0
    # The local copy is what the upgrade path depends on, so a failed push does
    # not fail the run. It is recorded, alerted on, and reported here.
    print(f"offsite FAILED: {result.get('error')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
