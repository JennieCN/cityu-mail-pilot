"""Turn an address into a country, offline.

The operator asked to see *where* visitors come from. The obvious way to do that
is to send each address to a geolocation API, and that is exactly what this
module avoids: the message board already refuses to hand visitor addresses to a
third party (see the anti-abuse notes on ``web.public_guest_message``), and doing
it here would mean every visitor's address leaving this server on every request.
So the lookup is a local SQLite range query against a database built from the
free DB-IP Lite CSV files — no account, no key, no network at lookup time.

DB-IP Lite is licensed CC BY 4.0, which requires attribution: the console shows
"Our IP geolocation by DB-IP" next to the numbers it produced. The database is a
build artifact, not source — it is generated on the machine that needs it (see
``manage geoip-update``) and deliberately lives outside the repository, outside
the release package and outside the source snapshot, for the same reason the
Android APK does.

Nothing here may raise on the request path: an unparseable address, a reserved
range, a missing file or a corrupt file all have to answer "unknown", because
the alternative is a 500 on a page a real person asked for.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import ipaddress
import os
import re
import sqlite3
from typing import Any, Iterable, Iterator, Optional

DEFAULT_PATH_ENV = "INFE_PILOT_GEOIP_DB"
DEFAULT_PATH = "/var/lib/cityu-mail-pilot/geoip.sqlite3"

DATASETS = ("country", "city")
DOWNLOAD_URL = "https://download.db-ip.com/free/dbip-{dataset}-lite-{month}.csv.gz"

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# DB-IP writes ZZ for "no country" -- reserved and unallocated ranges. Showing
# "ZZ" to the operator would look like a country code, so it gets a word.
UNKNOWN_CODE = "ZZ"
UNKNOWN_LABEL = "未知"

# ISO 3166-1 alpha-2 -> Chinese name, for the codes that actually turn up in a
# visitor log. Anything missing falls back to the raw code rather than to a
# guess: a wrong country name is worse than an unfamiliar two-letter one, and
# the fallback is visible in the console.
_COUNTRY_NAMES = {
    "AD": "安道尔", "AE": "阿联酋", "AF": "阿富汗", "AG": "安提瓜和巴布达", "AI": "安圭拉",
    "AL": "阿尔巴尼亚", "AM": "亚美尼亚", "AO": "安哥拉", "AR": "阿根廷", "AS": "美属萨摩亚",
    "AT": "奥地利", "AU": "澳大利亚", "AW": "阿鲁巴", "AZ": "阿塞拜疆", "BA": "波黑",
    "BB": "巴巴多斯", "BD": "孟加拉国", "BE": "比利时", "BF": "布基纳法索", "BG": "保加利亚",
    "BH": "巴林", "BI": "布隆迪", "BJ": "贝宁", "BM": "百慕大", "BN": "文莱",
    "BO": "玻利维亚", "BR": "巴西", "BS": "巴哈马", "BT": "不丹", "BW": "博茨瓦纳",
    "BY": "白俄罗斯", "BZ": "伯利兹", "CA": "加拿大", "CD": "刚果（金）", "CF": "中非",
    "CG": "刚果（布）", "CH": "瑞士", "CI": "科特迪瓦", "CL": "智利", "CM": "喀麦隆",
    "CN": "中国", "CO": "哥伦比亚", "CR": "哥斯达黎加", "CU": "古巴", "CV": "佛得角",
    "CW": "库拉索", "CY": "塞浦路斯", "CZ": "捷克", "DE": "德国", "DJ": "吉布提",
    "DK": "丹麦", "DM": "多米尼克", "DO": "多米尼加", "DZ": "阿尔及利亚", "EC": "厄瓜多尔",
    "EE": "爱沙尼亚", "EG": "埃及", "ER": "厄立特里亚", "ES": "西班牙", "ET": "埃塞俄比亚",
    "FI": "芬兰", "FJ": "斐济", "FO": "法罗群岛", "FR": "法国", "GA": "加蓬",
    "GB": "英国", "GD": "格林纳达", "GE": "格鲁吉亚", "GH": "加纳", "GI": "直布罗陀",
    "GL": "格陵兰", "GM": "冈比亚", "GN": "几内亚", "GP": "瓜德罗普", "GQ": "赤道几内亚",
    "GR": "希腊", "GT": "危地马拉", "GU": "关岛", "GY": "圭亚那", "HK": "中国香港",
    "HN": "洪都拉斯", "HR": "克罗地亚", "HT": "海地", "HU": "匈牙利", "ID": "印度尼西亚",
    "IE": "爱尔兰", "IL": "以色列", "IM": "马恩岛", "IN": "印度", "IQ": "伊拉克",
    "IR": "伊朗", "IS": "冰岛", "IT": "意大利", "JM": "牙买加", "JO": "约旦",
    "JP": "日本", "KE": "肯尼亚", "KG": "吉尔吉斯斯坦", "KH": "柬埔寨", "KM": "科摩罗",
    "KN": "圣基茨和尼维斯", "KP": "朝鲜", "KR": "韩国", "KW": "科威特", "KY": "开曼群岛",
    "KZ": "哈萨克斯坦", "LA": "老挝", "LB": "黎巴嫩", "LC": "圣卢西亚", "LI": "列支敦士登",
    "LK": "斯里兰卡", "LR": "利比里亚", "LS": "莱索托", "LT": "立陶宛", "LU": "卢森堡",
    "LV": "拉脱维亚", "LY": "利比亚", "MA": "摩洛哥", "MC": "摩纳哥", "MD": "摩尔多瓦",
    "ME": "黑山", "MG": "马达加斯加", "MK": "北马其顿", "ML": "马里", "MM": "缅甸",
    "MN": "蒙古", "MO": "中国澳门", "MQ": "马提尼克", "MR": "毛里塔尼亚", "MT": "马耳他",
    "MU": "毛里求斯", "MV": "马尔代夫", "MW": "马拉维", "MX": "墨西哥", "MY": "马来西亚",
    "MZ": "莫桑比克", "NA": "纳米比亚", "NC": "新喀里多尼亚", "NE": "尼日尔", "NG": "尼日利亚",
    "NI": "尼加拉瓜", "NL": "荷兰", "NO": "挪威", "NP": "尼泊尔", "NZ": "新西兰",
    "OM": "阿曼", "PA": "巴拿马", "PE": "秘鲁", "PF": "法属波利尼西亚", "PG": "巴布亚新几内亚",
    "PH": "菲律宾", "PK": "巴基斯坦", "PL": "波兰", "PR": "波多黎各", "PS": "巴勒斯坦",
    "PT": "葡萄牙", "PY": "巴拉圭", "QA": "卡塔尔", "RE": "留尼汪", "RO": "罗马尼亚",
    "RS": "塞尔维亚", "RU": "俄罗斯", "RW": "卢旺达", "SA": "沙特阿拉伯", "SC": "塞舌尔",
    "SD": "苏丹", "SE": "瑞典", "SG": "新加坡", "SI": "斯洛文尼亚", "SK": "斯洛伐克",
    "SL": "塞拉利昂", "SM": "圣马力诺", "SN": "塞内加尔", "SO": "索马里", "SR": "苏里南",
    "SS": "南苏丹", "ST": "圣多美和普林西比", "SV": "萨尔瓦多", "SY": "叙利亚", "SZ": "斯威士兰",
    "TC": "特克斯和凯科斯群岛", "TD": "乍得", "TG": "多哥", "TH": "泰国", "TJ": "塔吉克斯坦",
    "TL": "东帝汶", "TM": "土库曼斯坦", "TN": "突尼斯", "TO": "汤加", "TR": "土耳其",
    "TT": "特立尼达和多巴哥", "TW": "中国台湾", "TZ": "坦桑尼亚", "UA": "乌克兰", "UG": "乌干达",
    "US": "美国", "UY": "乌拉圭", "UZ": "乌兹别克斯坦", "VA": "梵蒂冈", "VC": "圣文森特和格林纳丁斯",
    "VE": "委内瑞拉", "VG": "英属维尔京群岛", "VI": "美属维尔京群岛", "VN": "越南", "VU": "瓦努阿图",
    "WS": "萨摩亚", "YE": "也门", "ZA": "南非", "ZM": "赞比亚", "ZW": "津巴布韦",
}


def country_name(code: str) -> str:
    """ISO 3166-1 alpha-2 -> Chinese name; unknown codes pass through."""
    text = str(code or "").strip().upper()
    if not text:
        return UNKNOWN_LABEL
    if text == UNKNOWN_CODE:
        return UNKNOWN_LABEL
    return _COUNTRY_NAMES.get(text, text)


def default_path() -> str:
    """Where the built database lives.

    Outside the application database on purpose: this file is tens of megabytes
    of public reference data that can be rebuilt in a minute, and the daily
    backup of ``pilot.sqlite3`` should not grow by that much to carry it.
    """
    value = str(os.environ.get(DEFAULT_PATH_ENV) or "").strip()
    return value or DEFAULT_PATH


def fetch_url(dataset: str, month: str) -> str:
    """The DB-IP Lite download URL for a dataset and month ("2026-09")."""
    name = str(dataset or "").strip().lower()
    if name not in DATASETS:
        raise ValueError("未知的数据集。")
    stamp = str(month or "").strip()
    if not _MONTH_RE.match(stamp):
        raise ValueError("月份格式应为 YYYY-MM。")
    return DOWNLOAD_URL.format(dataset=name, month=stamp)


def _pack(value: str) -> Optional[bytes]:
    """One address as 16 bytes, IPv4 mapped, so both families share a column."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.version == 4:
        address = ipaddress.IPv6Address("::ffff:" + str(address))
    return address.packed


def available(path: Optional[str] = None) -> bool:
    """Whether a usable database exists. Never raises."""
    target = path or default_path()
    if not target or not os.path.isfile(target):
        return False
    try:
        # `with sqlite3.connect(...)` is a *transaction* context manager: it
        # commits or rolls back, it does not close. Closing explicitly is not
        # tidiness -- see lookup() for what leaking one descriptor per call did.
        with contextlib.closing(sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=2)) as connection:
            connection.execute("SELECT 1 FROM ranges LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def lookup(ip: str, path: Optional[str] = None) -> dict[str, Any]:
    """Country (and city, when built) for an address, or ``{}``.

    Returns ``{"country": "CN", "country_name": "中国", "city": "", "continent": "AS"}``.
    Empty for a private address, an unparseable one, a gap in the data, or a
    database that is missing or broken -- all of which are "we do not know",
    never an error.
    """
    target = path or default_path()
    packed = _pack(ip)
    if packed is None or not target or not os.path.isfile(target):
        return {}
    try:
        address = ipaddress.ip_address(str(ip).strip())
        if address.is_private or address.is_loopback or address.is_reserved or address.is_multicast:
            return {}
    except ValueError:
        return {}
    try:
        # Read-only, so a missing or empty file cannot be created by a lookup.
        #
        # Exiting a `with sqlite3.connect(...)` block commits the transaction; it
        # does **not** close the connection. Written that way, this function held
        # one descriptor per call open long enough to matter: 2,000 lookups hit
        # the process's 1,024-descriptor limit, and on a request path that is a
        # web process which stops serving after roughly a thousand page views.
        # It was found by a 1,919-row log import that quietly stored 586 rows --
        # every row after the limit failed to open the database, and the importer
        # swallowed the error. Measured on the production box: 2,000 lookups,
        # 1,024 descriptors, "Too many open files".
        with contextlib.closing(
            sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=2)
        ) as connection:
            row = connection.execute(
                "SELECT cc, city, continent, end FROM ranges WHERE start <= ? ORDER BY start DESC LIMIT 1",
                (sqlite3.Binary(packed),),
            ).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    code, city, continent, end = row
    if end is None or bytes(end) < packed:
        return {}  # a gap between ranges: the closest start does not cover it
    return {
        "country": str(code or ""),
        "country_name": country_name(str(code or "")),
        "city": str(city or ""),
        "continent": str(continent or ""),
    }


def _rows(path: str) -> Iterator[list[str]]:
    """CSV rows from a plain or gzipped file, detected by content."""
    with open(path, "rb") as probe:
        magic = probe.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.reader(handle):
            if row:
                yield row


def _iter_batches(rows: Iterable[tuple], size: int = 20000) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def build(v4_csv: str, v6_csv: str, out_path: str, *, dataset: str = "country") -> dict[str, int]:
    """Build the lookup database from DB-IP Lite CSV files.

    Writes to ``<out_path>.building`` and renames on success, so a build that
    dies half way -- a truncated download, a full disk -- cannot leave a
    partial file where ``lookup()`` would read wrong answers out of it.
    """
    name = str(dataset or "").strip().lower()
    if name not in DATASETS:
        raise ValueError("未知的数据集。")
    target = str(out_path or "").strip()
    if not target:
        raise ValueError("缺少输出路径。")
    building = target + ".building"
    counts = {"rows": 0, "v4": 0, "v6": 0, "skipped": 0}
    if os.path.exists(building):
        os.unlink(building)
    try:
        connection = sqlite3.connect(building)
        try:
            connection.execute(
                "CREATE TABLE ranges (start BLOB NOT NULL, end BLOB NOT NULL,"
                " cc TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',"
                " continent TEXT NOT NULL DEFAULT '')"
            )
            for path in (v4_csv, v6_csv):
                if not path or not os.path.isfile(path):
                    continue
                batch: list[tuple] = []
                for row in _rows(path):
                    parsed = _range_row(row, dataset=name, counts=counts)
                    if parsed is None:
                        continue
                    batch.append(parsed)
                    if len(batch) >= 20000:
                        connection.executemany(
                            "INSERT INTO ranges(start,end,cc,city,continent) VALUES(?,?,?,?,?)", batch)
                        batch = []
                if batch:
                    connection.executemany(
                        "INSERT INTO ranges(start,end,cc,city,continent) VALUES(?,?,?,?,?)", batch)
            # The lookup is "closest start at or below this address", so the
            # index is on start; without it every visit scans 700k rows.
            connection.execute("CREATE INDEX idx_ranges_start ON ranges(start)")
            connection.commit()
        finally:
            connection.close()
        os.replace(building, target)
    except Exception:
        if os.path.exists(building):
            try:
                os.unlink(building)
            except OSError:  # pragma: no cover
                pass
        raise
    return counts


def _range_row(row: list[str], *, dataset: str, counts: dict[str, int]) -> Optional[tuple]:
    """One CSV row as a (start, end, cc, city, continent) tuple, or None."""
    if len(row) < 3:
        counts["skipped"] += 1
        return None
    start, end, code = _pack(row[0]), _pack(row[1]), str(row[2] or "").strip().upper()[:8]
    if start is None or end is None:
        counts["skipped"] += 1
        return None
    city = ""
    continent = ""
    if dataset == "city":
        # start,end,continent,country,subdivision,city,lat,lon
        if len(row) < 6:
            counts["skipped"] += 1
            return None
        continent = str(row[2] or "").strip().upper()[:8]
        code = str(row[3] or "").strip().upper()[:8]
        region = str(row[4] or "").strip()
        locality = str(row[5] or "").strip()
        # "Fujian / Wenquan" reads better than either half alone, and the
        # subdivision is what makes a city name unambiguous.
        city = f"{region} · {locality}" if region and locality else (locality or region)
    counts["rows"] += 1
    counts["v4" if len(start) == 16 and start[:12] == b"\x00" * 10 + b"\xff\xff" else "v6"] += 1
    return (sqlite3.Binary(start), sqlite3.Binary(end), code, city[:80], continent)
