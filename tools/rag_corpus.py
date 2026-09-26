#!/usr/bin/env python3
"""Offline-only public CityU corpus experiment. Never imports the mail application.

No credentials, proxies, recursive crawl, model calls, or production DB access.
Artifacts are new files only; existing files are never overwritten.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
from email.message import Message
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

HOSTS = frozenset({"www.cityu.edu.hk"})
UA = "CityUMailPilotResearch/0.1"
MAX_BYTES = 2_000_000
APP_ID = 0x43555247
SCHEMA = 1
STOP = frozenset("a an the i my me can could would should how what where when is are do does to of for in on at and or with about please find university cityu students student".split())


class CorpusError(ValueError):
    """Safe diagnostic: never includes response bodies or user inputs."""


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def valid_url(url):
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\s\\\x00-\x1f]", url):
        raise CorpusError("invalid source URL")
    p = urlsplit(url)
    if (p.scheme != "https" or p.netloc not in HOSTS or p.username or p.password
            or p.query or p.fragment or not p.path.startswith("/")):
        raise CorpusError("source must be an exact approved HTTPS host without query/fragment")
    return p


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1 or not 1 <= len(data.get("sources", [])) <= 50:
        raise CorpusError("invalid manifest version or source count")
    ids, urls = set(), set()
    for s in data["sources"]:
        valid_url(s.get("url"))
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", s.get("id", "")):
            raise CorpusError("invalid source id")
        if s["id"] in ids or s["url"] in urls:
            raise CorpusError("duplicate manifest id or URL")
        for key in ("title", "audience", "scope_note", "reviewed_on"):
            if not isinstance(s.get(key), str) or not s[key].strip():
                raise CorpusError("missing source review metadata")
        dt.date.fromisoformat(s["reviewed_on"])
        if s.get("approved") is not True:
            raise CorpusError("unapproved source")
        ids.add(s["id"])
        urls.add(s["url"])
    return data["sources"]


def public_addresses(host):
    addresses = sorted({r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise CorpusError("DNS contains a non-public address")
    return addresses


class PinnedPublicHTTPS(http.client.HTTPSConnection):
    """Connect to the validated IP, but validate TLS against the original hostname."""
    def connect(self):
        address = public_addresses(self.host)[0]
        sock = socket.create_connection((address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _fetch_once(url, limit=MAX_BYTES):
    # Parent process enforces a hard total deadline, including DNS and headers.
    p = valid_url(url)
    conn = PinnedPublicHTTPS(p.hostname, timeout=15, context=ssl.create_default_context())
    deadline = time.monotonic() + 25
    try:
        conn.request("GET", p.path, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
        response = conn.getresponse()
        if 300 <= response.status < 400:
            # No redirect is safer than silently approving a new source or login page.
            raise CorpusError("redirect requires manual manifest review")
        if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
            raise CorpusError("compressed response not accepted")
        content_length = response.getheader("Content-Length")
        if content_length and int(content_length) > limit:
            raise CorpusError("response too large")
        parts, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CorpusError("response deadline exceeded")
            if conn.sock:
                conn.sock.settimeout(min(15, remaining))
            chunk = response.read1(min(65536, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise CorpusError("response too large")
            parts.append(chunk)
        return response.status, response.headers, b"".join(parts)
    finally:
        conn.close()


def fetch(url, limit=MAX_BYTES):
    valid_url(url)
    if not 1 <= limit <= MAX_BYTES:
        raise CorpusError("invalid fetch limit")
    try:
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "_fetch", url, str(limit)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise CorpusError("fetch exceeded 30 second total deadline") from None
    if run.returncode:
        raise CorpusError("fetch failed; no source collected")
    result = json.loads(run.stdout)
    headers = Message()
    for key, value in result["headers"]:
        headers[key] = value
    return result["status"], headers, base64.b64decode(result["body"], validate=True)


def fetch_child(url, limit):
    try:
        limit = int(limit)
        if not 1 <= limit <= MAX_BYTES:
            return 2
        status, headers, body = _fetch_once(url, limit)
        # Cookies and other incidental headers are not part of the corpus protocol.
        selected = [(k, v) for k, v in headers.items() if k.lower() in ("content-type", "x-robots-tag")]
        print(json.dumps({"status": status, "headers": selected, "body": base64.b64encode(body).decode("ascii")}))
        return 0
    except (CorpusError, OSError, ValueError, http.client.HTTPException):
        return 2


class MainText(HTMLParser):
    """Keep main content only; never index global menus or execute markup."""
    SKIP = {"script", "style", "nav", "header", "footer", "form", "iframe", "svg", "template", "noscript"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.found_main = False
        self.noindex = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and (a.get("name") or "").lower() in ("robots", UA.lower()):
            self.noindex |= bool({"noindex", "none"}.intersection(re.split(r"[\s,]+", (a.get("content") or "").lower())))
        inside = (self.stack[-1][1] if self.stack else False) or tag == "main"
        hidden = ((self.stack[-1][2] if self.stack else False) or tag in self.SKIP
                  or "hidden" in a or a.get("aria-hidden") == "true"
                  or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", a.get("style") or "", re.I)))
        self.found_main |= tag == "main"
        if inside and not hidden and tag in self.BLOCK:
            self.parts.append("\n")
        if tag not in self.VOID:
            self.stack.append((tag, inside, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.parts.append(data)

    def text(self):
        if self.noindex:
            raise CorpusError("page opts out of indexing")
        if not self.found_main:
            raise CorpusError("no main element; needs a reviewed extractor")
        text = "\n".join(" ".join(line.split()) for line in "".join(self.parts).splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 100:
            raise CorpusError("main content too short; possible challenge/login page")
        return text


def collect(sources, get=fetch):
    """Sequential, bounded, allowlisted fetch. All-or-nothing corpus build."""
    robots, records = {}, []
    for s in sources:
        host = valid_url(s["url"]).hostname
        if host not in robots:
            status, _, body = get("https://" + host + "/robots.txt", 256_000)
            if status not in (200, 404):
                raise CorpusError("robots unavailable; refusing fetch")
            lines = body.decode("utf-8", "strict").splitlines() if status == 200 else []
            # robotparser does not implement RFC wildcard/end anchors or longest
            # match precedence. Decline unsupported policies instead of overcrawl.
            if any(re.match(r"\s*(?:allow|disallow)\s*:[^#]*[\*$]", line, re.I) for line in lines):
                raise CorpusError("robots wildcard policy needs a reviewed parser")
            if any(re.match(r"\s*allow\s*:", line, re.I) for line in lines):
                raise CorpusError("robots allow precedence needs a reviewed parser")
            policy = RobotFileParser()
            policy.parse(lines)
            robots[host] = policy
        policy = robots[host]
        if not policy.can_fetch(UA, s["url"]):
            raise CorpusError("robots disallows source " + s["id"])
        # Honour declared spacing; never wait arbitrarily long on this CLI.
        rate = policy.request_rate(UA)
        delay = max(1, policy.crawl_delay(UA) or 0, rate.seconds / rate.requests if rate and rate.requests else 0)
        if delay > 60:
            raise CorpusError("robots crawl delay requires manual scheduling")
        time.sleep(delay)
        status, headers, body = get(s["url"])
        if status != 200 or headers.get_content_type() != "text/html":
            raise CorpusError("expected public HTML for " + s["id"])
        directives = ",".join(headers.get_all("X-Robots-Tag", []))
        if {"noindex", "none"}.intersection(re.split(r"[\s,:]+", directives.lower())):
            raise CorpusError("source opts out of indexing")
        html = body.decode(headers.get_content_charset() or "utf-8", "strict")
        parser = MainText()
        parser.feed(html)
        text = parser.text()
        records.append(dict(s, text=text, fetched_at=utcnow(), content_sha256=hashlib.sha256(text.encode()).hexdigest()))
    return records


def terms(text, consumed=None):
    """Tokens for indexing/querying. `consumed` masks characters a reviewed term replaced.

    A CJK bigram lying inside an expanded span is dropped rather than kept: it is an
    artifact of not having a Chinese segmenter, and on this English corpus it can
    never match. Dropping it changes no retrieval result (0/30 measured) but it stops
    the noise from making any coverage figure meaningless.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    result = []
    for match in re.finditer(r"[a-z0-9]+|[\u3400-\u9fff]+", text):
        word, start = match.group(), match.start()
        if word[0] >= "\u3400":
            result.extend(text[i:i+2] for i in range(start, start + len(word) - 1)
                          if consumed is None or not any(consumed[i:i+2]))
        elif word not in STOP and len(word) > 1:
            result.append(word)
    return result


def chunks(text, size=1200):
    # Overlap preserves boundary context; never advertise this as semantic chunking.
    for start in range(0, len(text), size - 200):
        yield text[start:start + size]


def build(records, path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise CorpusError("output already exists; choose a new snapshot path")
    if not records:
        raise CorpusError("empty corpus")
    fd, temporary = tempfile.mkstemp(prefix=".rag-build-", dir=path.parent)
    os.close(fd)
    conn = None
    try:
        conn = sqlite3.connect(temporary)
        conn.executescript(f"""
            PRAGMA application_id={APP_ID}; PRAGMA user_version={SCHEMA};
            CREATE TABLE sources(id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT,
                audience TEXT, scope_note TEXT, reviewed_on TEXT, fetched_at TEXT, content_sha256 TEXT);
            CREATE TABLE chunks(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, ordinal INTEGER, text TEXT);
            CREATE VIRTUAL TABLE search USING fts5(title, body, tokenize='unicode61');
        """)
        seen = set()
        for r in records:
            valid_url(r["url"])
            fetched = dt.datetime.fromisoformat(r["fetched_at"])
            if fetched.tzinfo is None:
                raise CorpusError("snapshot timestamp must include timezone")
            fetched_at = fetched.astimezone(dt.timezone.utc).isoformat()
            digest = hashlib.sha256(r["text"].encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)", (
                r["id"], r["url"], r["title"], r["audience"], r["scope_note"], r["reviewed_on"], fetched_at, digest))
            for ordinal, passage in enumerate(chunks(r["text"])):
                cursor = conn.execute("INSERT INTO chunks(source_id,ordinal,text) VALUES (?,?,?)", (r["id"], ordinal, passage))
                conn.execute("INSERT INTO search(rowid,title,body) VALUES (?,?,?)", (cursor.lastrowid, " ".join(terms(r["title"])), " ".join(terms(passage))))
        conn.execute("INSERT INTO search(search) VALUES ('integrity-check')")
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CorpusError("corpus integrity check failed")
        conn.close()
        conn = None
        # Exclusive publication: even a concurrent writer cannot be overwritten.
        os.link(temporary, path)
    finally:
        if conn:
            conn.close()
        os.unlink(temporary)


def open_corpus(path):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        if (conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                or conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA):
            raise CorpusError("not a supported public corpus database")
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        conn.close()
        raise


def query_terms(query):
    """The raw tokens of a query, before any reviewed expansion."""
    if not isinstance(query, str) or len(query) > 2000:
        raise CorpusError("query outside bounds")
    return list(dict.fromkeys(terms(query)))[:32]


GLOSSARY_PATH = Path(__file__).resolve().parent / "rag_data" / "glossary.json"


def load_glossary(path=GLOSSARY_PATH):
    """Reviewed Chinese term -> English terms that the collected pages actually use.

    Small and hand-curated on purpose. It is not a translation system and not a
    synonym dictionary: a Chinese term absent here keeps its CJK bigrams, which
    cannot match an English corpus. Expansion stays opt-in so the baseline stays
    reproducible and no retrieval behaviour changes silently.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise CorpusError("invalid glossary version")
    dt.date.fromisoformat(data.get("reviewed_on") or "")
    if not isinstance(data.get("scope_note"), str) or not data["scope_note"].strip():
        raise CorpusError("missing glossary scope note")
    entries = data.get("terms")
    if not isinstance(entries, dict) or not 1 <= len(entries) <= 500:
        raise CorpusError("invalid glossary size")
    cleaned = {}
    for key, value in entries.items():
        if not isinstance(key, str) or not re.fullmatch(r"[\u3400-\u9fff]{2,12}", key):
            raise CorpusError("invalid glossary key")
        if (not isinstance(value, list) or not 1 <= len(value) <= 6
                or any(not isinstance(t, str) or not re.fullmatch(r"[a-z][a-z0-9 ]{0,40}", t) for t in value)):
            raise CorpusError("invalid glossary expansion")
        cleaned[key] = list(value)
    return cleaned


def expand_terms(query, glossary):
    """(remaining raw terms, English terms from the glossary). Longest reviewed term wins.

    Longest first keeps 成绩单 (transcript) from also firing 成绩 (grade), and consuming
    the span stops 学籍 inside 终止学籍 from expanding twice. Consumption also **replaces**:
    the CJK bigrams of a reviewed term are dropped, because on this English corpus they
    can never match and they would otherwise drown any coverage measurement.
    """
    base = query_terms(query)
    if not glossary:
        return base, []
    normalized = unicodedata.normalize("NFKC", query if isinstance(query, str) else "").lower()
    consumed = [False] * len(normalized)
    added = []
    for key in sorted(glossary, key=len, reverse=True):
        start = 0
        while True:
            index = normalized.find(key, start)
            if index < 0:
                break
            start = index + 1
            if any(consumed[index:index + len(key)]):
                continue
            for position in range(index, index + len(key)):
                consumed[position] = True
            for english in glossary[key]:
                if english not in added:
                    added.append(english)
    base = list(dict.fromkeys(terms(query, consumed)))[:32]
    return base, added


def search_terms(query, glossary=None):
    """Terms actually handed to BM25. Expansion leads, so the 32-term cap cannot drop it."""
    base, added = expand_terms(query, glossary or {})
    return list(dict.fromkeys(added + base))[:32]


def window(now=None, max_age_days=30):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or not 1 <= max_age_days <= 365:
        raise CorpusError("invalid snapshot time window")
    now = now.astimezone(dt.timezone.utc)
    return now, (now - dt.timedelta(days=max_age_days)).isoformat()


def cjk(token):
    return bool(token) and token[0] >= "\u3400"


def corpus_script(conn):
    """'cjk' if any indexed passage contains CJK, else 'latin'.

    Coverage uses this so it never treats an unmatchable script as evidence either way:
    on a corpus with no CJK at all a Chinese bigram is an artifact of not having a
    segmenter, not a term the query was "missing". Detected from the data, so adding a
    Chinese source automatically restores the stricter behaviour.
    """
    for (text,) in conn.execute("SELECT text FROM chunks"):
        for character in text:
            if character >= "\u3400":
                return "cjk"
    return "latin"


def term_stats(path, tokens, now=None, max_age_days=30):
    """(matched tokens, idf coverage). Diagnostic, never a score and never a verdict.

    Coverage is the share of the query's inverse-document-frequency mass that the corpus
    actually contains. A term the corpus has never seen gets the **highest** idf on
    purpose: "this corpus has never used this word" is the strongest available evidence
    that it cannot answer, and a df=0 -> 0 weighting hides exactly that (measured: it
    made a 2-of-5 match report as full coverage).
    """
    tokens = list(dict.fromkeys(tokens))[:32]
    if not tokens:
        return [], 0.0
    now, cutoff = window(now, max_age_days)
    conn = open_corpus(path)
    try:
        total = conn.execute(
            """SELECT COUNT(*) FROM chunks c JOIN sources s ON c.source_id=s.id
               WHERE s.fetched_at>=? AND s.fetched_at<=?""", (cutoff, now.isoformat())).fetchone()[0]
        if not total:
            return [], 0.0
        if corpus_script(conn) == "latin":
            # Fall back to every token when nothing is left, so an all-Chinese query the
            # glossary cannot carry still reports zero coverage instead of a free 1.0.
            tokens = [t for t in tokens if not cjk(t)] or tokens
        matched, mass, matched_mass = [], 0.0, 0.0
        for token in tokens:
            df = conn.execute(
                """SELECT COUNT(*) FROM search JOIN chunks c ON search.rowid=c.id
                   JOIN sources s ON c.source_id=s.id
                   WHERE search MATCH ? AND s.fetched_at>=? AND s.fetched_at<=?""",
                ('"' + token + '"', cutoff, now.isoformat())).fetchone()[0]
            weight = math.log(1.0 + total / (df + 1.0))
            mass += weight
            if df:
                matched.append(token)
                matched_mass += weight
        return matched, (matched_mass / mass if mass else 0.0)
    finally:
        conn.close()


def matched_terms(path, tokens, now=None, max_age_days=30):
    """Which tokens match at least one in-window passage. Diagnostic, never a score.

    This exists to separate the two ways a query returns nothing: tokens that match
    no passage at all (a vocabulary/language gap) versus tokens that match but rank
    outside the limit. A matched token is still not evidence that the corpus answers
    the question, and it is not a confidence value.
    """
    return term_stats(path, tokens, now=now, max_age_days=max_age_days)[0]


def diagnose(path, query, now=None, max_age_days=30, glossary=None):
    """Explain one query without changing retrieval: terms, matches and ranked ids."""
    raw = query_terms(query)
    tokens = search_terms(query, glossary)
    matched, coverage = term_stats(path, tokens, now=now, max_age_days=max_age_days)
    return {"query_terms": raw, "expanded_terms": tokens,
            "added_terms": [t for t in tokens if t not in set(raw)],
            "matched_terms": matched,
            "unmatched_terms": [t for t in tokens if t not in set(matched)],
            "coverage": round(coverage, 4),
            "hits": [h["id"] for h in search(path, query, now=now, max_age_days=max_age_days, glossary=glossary)],
            "note": "matched terms explain retrieval, not answerability; not a confidence score"}


def search(path, query, limit=3, now=None, max_age_days=30, glossary=None, min_coverage=None):
    if not isinstance(query, str) or len(query) > 2000 or not 1 <= limit <= 10:
        raise CorpusError("query or limit outside bounds")
    tokens = search_terms(query, glossary)
    if not tokens:
        return []
    if min_coverage is not None:
        # Opt-in abstention on a stated rule, never a hidden default: below the caller's
        # coverage floor the query is mostly about words this corpus has never used.
        if not 0.0 < min_coverage <= 1.0:
            raise CorpusError("invalid minimum coverage")
        if term_stats(path, tokens, now=now, max_age_days=max_age_days)[1] < min_coverage:
            return []
    expression = " OR ".join('"' + t + '"' for t in tokens)
    now, cutoff = window(now, max_age_days)
    conn = open_corpus(path)
    try:
        # All matches are snapshot candidates, not a confidence/answer score.
        rows = conn.execute("""SELECT s.*, c.ordinal, c.text, bm25(search, 3.0, 1.0) AS rank
            FROM search JOIN chunks c ON search.rowid=c.id JOIN sources s ON c.source_id=s.id
            WHERE search MATCH ? AND s.fetched_at>=? AND s.fetched_at<=?
            ORDER BY rank, s.id, c.ordinal LIMIT 100""", (expression, cutoff, now.isoformat())).fetchall()
        result, seen = [], set()
        for row in rows:
            if row["id"] in seen:
                continue
            valid_url(row["url"])
            seen.add(row["id"])
            result.append(dict(row, evidence_type="official_snapshot", date_status="applicability_unverified"))
            if len(result) == limit:
                break
        return result
    finally:
        conn.close()


def evaluate(path, cases, glossary=None, min_coverage=None):
    if len(cases) < 30 or len({c["id"] for c in cases}) != len(cases):
        raise CorpusError("evaluation needs at least 30 uniquely identified cases")
    conn = open_corpus(path)
    try:
        source_ids = {r[0] for r in conn.execute("SELECT id FROM sources")}
    finally:
        conn.close()
    if any(not set(c["expected"]).issubset(source_ids) for c in cases):
        raise CorpusError("evaluation references a missing source")
    results, latencies = [], []
    for c in cases:
        start = time.perf_counter()
        hits = search(path, c["query"], glossary=glossary, min_coverage=min_coverage)
        latencies.append((time.perf_counter() - start) * 1000)
        ids = [h["id"] for h in hits]
        expected = set(c["expected"])
        raw = query_terms(c["query"])
        tokens = search_terms(c["query"], glossary)
        matched, coverage = term_stats(path, tokens)
        results.append({"id": c["id"], "language": c["language"], "expected": sorted(expected), "actual": ids,
                        "hit1": bool(expected.intersection(ids[:1])), "hit3": bool(expected.intersection(ids)),
                        "negative_false_hit": not expected and bool(ids),
                        "query_terms": raw, "expanded_terms": tokens, "matched_terms": matched,
                        "unmatched_terms": [t for t in tokens if t not in set(matched)],
                        "coverage": round(coverage, 4)})
    positives = [r for r in results if r["expected"]]
    negatives = [r for r in results if not r["expected"]]
    by_language = {}
    for language in sorted({r["language"] for r in positives}):
        group = [r for r in positives if r["language"] == language]
        by_language[language] = {"cases": len(group), "source_hit_rate_at_3": sum(r["hit3"] for r in group) / len(group),
                                 "no_matched_term": sum(not r["matched_terms"] for r in group)}
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss = rss if sys.platform == "darwin" else rss * 1024
    except ImportError:
        peak_rss = None
    return {"scope": "source retrieval only; not answer correctness or production readiness",
            "expected_semantics": "alternative acceptable sources, not an exhaustive relevance set; hit rate is not recall",
            "cases": len(cases), "positive_cases": len(positives), "negative_cases": len(negatives),
            "source_hit_rate_at_1": sum(r["hit1"] for r in positives) / len(positives) if positives else None,
            "source_hit_rate_at_3": sum(r["hit3"] for r in positives) / len(positives) if positives else None,
            "negative_false_hits": sum(r["negative_false_hit"] for r in negatives),
            "no_matched_term_cases": [r["id"] for r in results if r["expanded_terms"] and not r["matched_terms"]],
            "glossary_applied": bool(glossary), "min_coverage": min_coverage,
            "coverage_of_weakest_positive": min((r["coverage"] for r in positives), default=None),
            "coverage_of_strongest_false_hit": max((r["coverage"] for r in negatives if r["negative_false_hit"]),
                                                   default=None),
            "p95_ms": sorted(latencies)[math.ceil(len(latencies)*.95)-1],
            "peak_process_rss_bytes": peak_rss, "by_language": by_language,
            "corpus_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "corpus_bytes": Path(path).stat().st_size, "results": results}


def calibrate(path, cases, glossary=None, thresholds=None):
    """Sweep the coverage gate and report the trade it makes, instead of picking a number.

    Returns, per threshold: which positives it refuses and which false hits it removes.
    The point is that the caller chooses an operating point **knowing the cost**, and
    that no threshold is baked in as a default anywhere.
    """
    thresholds = thresholds or [round(0.05 * n, 2) for n in range(1, 20)]
    if not thresholds or any(not 0.0 < t <= 1.0 for t in thresholds):
        raise CorpusError("invalid calibration thresholds")
    scored = []
    for c in cases:
        tokens = search_terms(c["query"], glossary)
        coverage = term_stats(path, tokens)[1]
        hits = search(path, c["query"], glossary=glossary)
        scored.append({"id": c["id"], "expected": bool(c["expected"]), "coverage": round(coverage, 4),
                       "hit": bool(hits)})
    positives = [s for s in scored if s["expected"]]
    negatives = [s for s in scored if not s["expected"] and s["hit"]]
    curve = []
    for t in sorted(thresholds):
        curve.append({"min_coverage": t,
                      "positives_refused": [s["id"] for s in positives if s["coverage"] < t],
                      "false_hits_removed": [s["id"] for s in negatives if s["coverage"] < t]})
    return {"scope": "development set; the caller picks a threshold and owns the trade",
            "cases": len(cases), "positives": len(positives), "false_hits": len(negatives),
            "weakest_positive_coverage": min((s["coverage"] for s in positives), default=None),
            "strongest_false_hit_coverage": max((s["coverage"] for s in negatives), default=None),
            "curve": curve}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--manifest", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--fetch", action="store_true", help="Actually fetch public sources; default is manifest-only dry run")
    s = sub.add_parser("search")
    s.add_argument("--corpus", required=True)
    s.add_argument("--query", required=True, help="Public/synthetic query only; do not put email text in argv")
    d = sub.add_parser("diagnose")
    d.add_argument("--corpus", required=True)
    d.add_argument("--query", required=True, help="Public/synthetic query only; do not put email text in argv")
    e = sub.add_parser("evaluate")
    e.add_argument("--corpus", required=True)
    e.add_argument("--cases", required=True)
    e.add_argument("--min-coverage", type=float, default=None,
                   help="Opt-in abstention: refuse a query whose idf coverage is below this")
    c = sub.add_parser("calibrate")
    c.add_argument("--corpus", required=True)
    c.add_argument("--cases", required=True)
    c.add_argument("--thresholds", default=None,
                   help="Comma separated coverage thresholds to sweep; default 0.05..0.95")
    for parser in (s, d, e, c):
        parser.add_argument("--glossary", nargs="?", const=str(GLOSSARY_PATH), default=None,
                            help="Enable the reviewed bilingual expansion; optional path overrides the packaged one")
    args = p.parse_args(argv)
    glossary = load_glossary(args.glossary) if getattr(args, "glossary", None) else None
    try:
        if args.command == "build":
            sources = load_manifest(args.manifest)
            if Path(args.output).exists():
                raise CorpusError("output already exists")
            if args.fetch:
                records = collect(sources)
                build(records, args.output)
            output = {"mode": "built" if args.fetch else "dry-run", "sources": len(sources)}
        elif args.command == "search":
            output = search(args.corpus, args.query, glossary=glossary)
        elif args.command == "diagnose":
            output = diagnose(args.corpus, args.query, glossary=glossary)
        elif args.command == "calibrate":
            cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
            thresholds = [float(t) for t in args.thresholds.split(",")] if args.thresholds else None
            output = calibrate(args.corpus, cases, glossary=glossary, thresholds=thresholds)
        else:
            output = evaluate(args.corpus, json.loads(Path(args.cases).read_text(encoding="utf-8")),
                              glossary=glossary, min_coverage=args.min_coverage)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (CorpusError, OSError, ValueError, sqlite3.Error, http.client.HTTPException) as exc:
        # Only our own fixed diagnostics are safe to print; network errors may carry URLs.
        print(json.dumps({"error": str(exc) if isinstance(exc, CorpusError) else type(exc).__name__}))
        return 2


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_fetch":
        raise SystemExit(fetch_child(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
