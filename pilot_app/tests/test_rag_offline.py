"""The experimental offline corpus must stay separate from mail and user data."""
from __future__ import annotations
import datetime as dt
from email.message import Message
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("rag_corpus", ROOT / "tools/rag_corpus.py")
rag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rag)
NOW = dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc)
#: The glossary the held-out numbers were measured against. Changing the glossary
#: invalidates them, so the pin has to move deliberately, in the same commit.
PINNED_GLOSSARY_SHA256 = "44fd944c5241cd48b40e9d4dc563d108cb599946c3dd8db550b267eaa4fe0f31"


def record(key="registration", text=None, fetched=None):
    return dict(id=key, url="https://www.cityu.edu.hk/arro/" + key + ".htm", title=key,
                audience="synthetic", scope_note="synthetic fixture, not university policy",
                reviewed_on="2026-09-26", fetched_at=fetched or NOW.isoformat(),
                text=text or "Synthetic registration prerequisite credit transfer guidance. " * 8)


class OfflineCorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "corpus.sqlite"

    def test_manifest_is_small_approved_and_unique(self):
        sources = rag.load_manifest(ROOT / "tools/rag_data/sources.json")
        self.assertEqual(len(sources), 5)

    def test_manifest_rejects_unapproved(self):
        p = Path(self.tmp.name) / "sources.json"
        p.write_text(json.dumps({"version":1,"sources":[dict(record(),approved=False)]}))
        with self.assertRaises(rag.CorpusError): rag.load_manifest(p)

    def test_url_boundary(self):
        for url in ("http://www.cityu.edu.hk/a", "https://www.cityu.edu.hk.evil.com/a",
                    "https://evil.com/a", "https://www.cityu.edu.hk:443/a", "https://user@www.cityu.edu.hk/a",
                    "https://www.cityu.edu.hk/a?key=x", "https://www.cityu.edu.hk/a#fragment",
                    "https://127.0.0.1/a", "https://www.cityu.edu.hk/\\evil", "https://www.cityu.edu.hk/a\n"):
            with self.subTest(url=url), self.assertRaises(rag.CorpusError): rag.valid_url(url)

    def test_private_dns_rejected(self):
        for addr in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"):
            with patch.object(rag.socket,"getaddrinfo",return_value=[(0,0,0,"",(addr,443))]):
                with self.assertRaises(rag.CorpusError): rag.public_addresses("www.cityu.edu.hk")

    def test_mixed_dns_rejected(self):
        with patch.object(rag.socket,"getaddrinfo",return_value=[(0,0,0,"",("8.8.8.8",443)),(0,0,0,"",("127.0.0.1",443))]):
            with self.assertRaises(rag.CorpusError): rag.public_addresses("www.cityu.edu.hk")

    def test_connection_uses_checked_ip_and_original_tls_name(self):
        context, sock = Mock(), Mock()
        with patch.object(rag,"public_addresses",return_value=["8.8.8.8"]), patch.object(rag.socket,"create_connection",return_value=sock) as connect:
            c = rag.PinnedPublicHTTPS("www.cityu.edu.hk",context=context)
            c.connect()
            self.assertEqual(connect.call_args[0][0],("8.8.8.8",443))
            context.wrap_socket.assert_called_once_with(sock,server_hostname="www.cityu.edu.hk")

    def test_extract_main_only(self):
        p=rag.MainText()
        p.feed('<nav>OUTSIDE</nav><main><h1>Title</h1><script>SECRET</script><div hidden>HIDDEN</div><style>CSS</style><p>'+"Useful content. "*20+'</p><!-- COMMENT --><nav>MENU</nav></main><footer>FOOTER</footer>')
        text=p.text()
        for forbidden in ("OUTSIDE","SECRET","HIDDEN","CSS","COMMENT","MENU","FOOTER"):
            self.assertNotIn(forbidden,text)
        self.assertIn("Useful content",text)

    def test_missing_main_and_short_challenge_rejected(self):
        for html in ("<body>"+"X"*200+"</body>","<main>Access denied</main>"):
            p=rag.MainText(); p.feed(html)
            with self.assertRaises(rag.CorpusError): p.text()

    def test_boolean_style_attribute_real_cityu_html(self):
        p=rag.MainText(); p.feed('<main><p style>'+"Useful content. "*20+'</p></main>')
        self.assertIn("Useful",p.text())

    def test_noindex_attribute_order_and_unquoted_value(self):
        for tag in ('<meta content="noindex, nofollow" name="robots">', '<meta name=ROBOTS content=NOINDEX>', '<meta name=robots content=none>'):
            p=rag.MainText(); p.feed(tag+'<main>'+"Useful content. "*20+'</main>')
            with self.assertRaises(rag.CorpusError): p.text()

    def test_naive_timestamp_rejected_and_offset_normalized(self):
        with self.assertRaises(rag.CorpusError): rag.build([record(fetched="2026-09-26T00:00:00")],self.path)
        rag.build([record(fetched="2026-09-26T08:00:00+08:00")],self.path)
        self.assertEqual(rag.search(self.path,"credit",now=NOW)[0]["fetched_at"],NOW.isoformat())

    def test_robots_fail_closed(self):
        for status, body in ((403,b""),(500,b""),(200,b"User-agent: *\nDisallow: /")):
            get=Mock(return_value=(status,Message(),body))
            with self.assertRaises(rag.CorpusError): rag.collect([record()],get=get)
            self.assertEqual(get.call_count,1)

    def test_robots_unsupported_matching_fails_closed(self):
        for policy in (b'User-agent: *\nDisallow: /*.htm$', b'User-agent: *\nAllow: /\nDisallow: /private/'):
            get=Mock(return_value=(200,Message(),policy))
            with self.assertRaises(rag.CorpusError): rag.collect([record()],get)
            self.assertEqual(get.call_count,1)

    def test_non_html_rejected(self):
        headers=Message(); headers["Content-Type"]="application/pdf"
        get=Mock(side_effect=[(404,Message(),b""),(200,headers,b"%PDF")])
        with patch.object(rag.time,"sleep"), self.assertRaises(rag.CorpusError): rag.collect([record()],get)

    def test_duplicate_noindex_headers_rejected(self):
        headers=Message(); headers['Content-Type']='text/html'
        headers['X-Robots-Tag']='nofollow'; headers['X-Robots-Tag']='noindex'
        get=Mock(side_effect=[(404,Message(),b''),(200,headers,b'<main>content</main>')])
        with patch.object(rag.time,'sleep'), self.assertRaises(rag.CorpusError): rag.collect([record()],get)

    def test_total_deadline_includes_dns_and_headers(self):
        with patch.object(rag.subprocess,'run',side_effect=rag.subprocess.TimeoutExpired('fetch',30)) as run:
            with self.assertRaisesRegex(rag.CorpusError,'total deadline'): rag.fetch(record()['url'])
        self.assertEqual(run.call_args.kwargs['timeout'],30)

    def test_fetch_child_protocol_hides_cookies(self):
        headers=Message(); headers['Content-Type']='text/html'; headers['Set-Cookie']='not-for-corpus'
        with patch.object(rag,'_fetch_once',return_value=(200,headers,b'hello')),patch('builtins.print') as output:
            self.assertEqual(rag.fetch_child(record()['url'],'100'),0)
        self.assertNotIn('not-for-corpus',output.call_args.args[0])

    def test_fetch_rejects_redirect_large_and_compressed(self):
        for status, headers in ((302,{}),(200,{"Content-Length":"999999999"}),(200,{"Content-Encoding":"gzip"})):
            response=Mock(status=status); response.getheader.side_effect=lambda k,d=None:headers.get(k,d)
            conn=Mock(); conn.getresponse.return_value=response
            with patch.object(rag,"PinnedPublicHTTPS",return_value=conn), self.assertRaises(rag.CorpusError):
                rag._fetch_once(record()["url"])
            conn.close.assert_called_once()

    def test_build_search_is_readonly(self):
        rag.build([record()],self.path)
        before=self.path.read_bytes()
        hits=rag.search(self.path,"credit transfer",now=NOW)
        self.assertEqual(hits[0]["id"],"registration")
        self.assertEqual(hits[0]["evidence_type"],"official_snapshot")
        self.assertEqual(hits[0]["date_status"],"applicability_unverified")
        self.assertEqual(before,self.path.read_bytes())
        c=rag.open_corpus(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError): c.execute("DELETE FROM sources")
        finally: c.close()

    def test_existing_database_never_overwritten(self):
        self.path.write_bytes(b"do not touch")
        with self.assertRaises(rag.CorpusError): rag.build([record()],self.path)
        self.assertEqual(self.path.read_bytes(),b"do not touch")

    def test_unrelated_database_rejected(self):
        sqlite3.connect(self.path).close()
        with self.assertRaises(rag.CorpusError): rag.search(self.path,"credit",now=NOW)

    def test_missing_database_not_created(self):
        with self.assertRaises(FileNotFoundError): rag.search(self.path,"credit",now=NOW)
        self.assertFalse(self.path.exists())

    def test_duplicate_content_and_sources_deduplicated(self):
        rag.build([record(),record("copy")],self.path)
        self.assertEqual(len(rag.search(self.path,"credit",now=NOW)),1)

    def test_expired_and_future_snapshots_not_returned(self):
        for fetched in ((NOW-dt.timedelta(days=31)).isoformat(),(NOW+dt.timedelta(days=1)).isoformat()):
            with self.subTest(fetched=fetched):
                path=Path(self.tmp.name)/str(len(list(Path(self.tmp.name).iterdir())))
                rag.build([record(fetched=fetched)],path)
                self.assertEqual(rag.search(path,"credit",now=NOW),[])

    def test_fts_syntax_is_not_executed(self):
        rag.build([record()],self.path)
        for query in ('"','*','NEAR(',"credit' OR 1=1 --","body:credit"):
            rag.search(self.path,query,now=NOW)

    def test_cjk_and_english_normalization(self):
        self.assertEqual(rag.terms("ＡＢＣ 选课规则"),["abc","选课","课规","规则"])

    def test_query_limits(self):
        for query,limit in (("x"*2001,3),("credit",0),("credit",11)):
            with self.assertRaises(rag.CorpusError): rag.search(self.path,query,limit=limit,now=NOW)

    def test_failed_build_cleans_temp_and_does_not_publish(self):
        with self.assertRaises(KeyError): rag.build([{}],self.path)
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[])

    def test_eval_has_30_synthetic_cases_and_negatives(self):
        cases=json.loads((ROOT/"tools/rag_data/eval_cases.json").read_text())
        self.assertEqual(len(cases),30)
        self.assertTrue(any(not c["expected"] for c in cases))
        rag.build([record(s['id'],text=s['title']+' synthetic content '*20,fetched=rag.utcnow()) for s in rag.load_manifest(ROOT/'tools/rag_data/sources.json')],self.path)
        result=rag.evaluate(self.path,cases)
        self.assertEqual(result["positive_cases"],26)
        self.assertEqual(result["negative_cases"],4)
        self.assertNotIn('recall_at_3',result)

    def test_eval_missing_expected_source_fails(self):
        rag.build([record()],self.path)
        cases=[dict(id=str(i),language='en',query='credit',expected=['missing']) for i in range(30)]
        with self.assertRaises(rag.CorpusError): rag.evaluate(self.path,cases)

    def test_default_build_is_dry_run_no_network(self):
        with patch.object(rag,"fetch") as network:
            self.assertEqual(rag.main(["build","--manifest",str(ROOT/"tools/rag_data/sources.json"),"--output",str(self.path)]),0)
        network.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_diagnose_separates_vocabulary_gap_from_ranking(self):
        """Two ways to get nothing back must not be reported as the same failure."""
        rag.build([record()],self.path)
        gap=rag.diagnose(self.path,"选课先修条件限制",now=NOW)
        self.assertTrue(gap["query_terms"])
        self.assertEqual(gap["matched_terms"],[])
        self.assertEqual(gap["unmatched_terms"],gap["query_terms"])
        self.assertEqual(gap["hits"],[])
        ranked=rag.diagnose(self.path,"credit transfer",now=NOW)
        self.assertEqual(ranked["matched_terms"],["credit","transfer"])
        self.assertEqual(ranked["unmatched_terms"],[])
        self.assertEqual(ranked["hits"],["registration"])

    def test_diagnose_is_read_only(self):
        rag.build([record()],self.path)
        before=self.path.read_bytes()
        rag.diagnose(self.path,"credit transfer",now=NOW)
        self.assertEqual(before,self.path.read_bytes())

    def test_evaluate_names_the_language_gap_instead_of_leaving_it_implicit(self):
        """Every Chinese case misses because no Chinese term occurs in an English corpus.

        Assertions are deliberately one-sided: on this synthetic corpus some English
        queries also have no matching term, so the claim is "all Chinese cases are
        named", not "only Chinese cases are named".
        """
        rag.build([record(s['id'],text=s['title']+' synthetic content '*20,fetched=rag.utcnow()) for s in rag.load_manifest(ROOT/'tools/rag_data/sources.json')],self.path)
        cases=json.loads((ROOT/"tools/rag_data/eval_cases.json").read_text())
        chinese={c["id"] for c in cases if c["language"]=="zh"}
        result=rag.evaluate(self.path,cases)
        self.assertTrue(chinese.issubset(set(result["no_matched_term_cases"])))
        self.assertEqual(result["by_language"]["zh"]["no_matched_term"],8)
        self.assertEqual(result["by_language"]["zh"]["source_hit_rate_at_3"],0.0)
        by_id={r["id"]:r for r in result["results"]}
        self.assertTrue(by_id["en02"]["matched_terms"],"diagnostic must not report every query as unmatched")
        self.assertTrue(by_id["zh01"]["query_terms"],"a Chinese query must produce terms, just none that match")
        for r in result["results"]:
            self.assertEqual(set(r["expanded_terms"]),set(r["matched_terms"])|set(r["unmatched_terms"]))
            self.assertFalse(set(r["matched_terms"])&set(r["unmatched_terms"]))
        self.assertFalse(result["glossary_applied"])

    def test_glossary_rejects_malformed_input(self):
        for data in ({"version":2,"reviewed_on":"2026-09-26","scope_note":"x","terms":{"退课":["drop"]}},
                     {"version":1,"reviewed_on":"2026-09-26","scope_note":"","terms":{"退课":["drop"]}},
                     {"version":1,"reviewed_on":"2026-09-26","scope_note":"x","terms":{"drop":["drop"]}},
                     {"version":1,"reviewed_on":"2026-09-26","scope_note":"x","terms":{"退课":["Drop!"]}},
                     {"version":1,"reviewed_on":"2026-09-26","scope_note":"x","terms":{}}):
            path=Path(self.tmp.name)/f"g{len(list(Path(self.tmp.name).iterdir()))}.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(rag.CorpusError): rag.load_glossary(path)

    def test_glossary_expansion_is_opt_in_and_replaces_the_covered_span(self):
        glossary={"成绩单":["transcript"],"成绩":["grade"]}
        base,added=rag.expand_terms("成绩单",glossary)
        self.assertEqual(base,[],"bigrams inside a reviewed span are replaced, not kept alongside")
        self.assertEqual(added,["transcript"],"成绩 inside 成绩单 must not also expand to grade")
        leftover,_=rag.expand_terms("成绩单 条件",glossary)
        self.assertIn("条件",leftover,"Chinese outside any reviewed span still keeps its bigram")
        self.assertNotIn("成绩",leftover,"the reviewed span itself stays replaced")
        self.assertEqual(rag.search_terms("退课",None),rag.query_terms("退课"))
        self.assertEqual(rag.search_terms("退课",{"退课":["drop"]})[0],"drop",
                         "expansion must lead so the 32-term cap cannot drop it")

    def test_glossary_recovers_a_chinese_query_that_otherwise_returns_nothing(self):
        rag.build([record()],self.path)
        query="转学分与课程豁免"
        self.assertEqual(rag.search(self.path,query,now=NOW),[],"baseline: CJK bigrams cannot match English")
        glossary={"转学分":["credit transfer"],"豁免":["exemption"]}
        self.assertEqual([h["id"] for h in rag.search(self.path,query,now=NOW,glossary=glossary)],["registration"])
        explained=rag.diagnose(self.path,query,now=NOW,glossary=glossary)
        self.assertEqual(sorted(explained["added_terms"]),["credit transfer","exemption"])

    def test_shipped_glossary_is_wellformed_and_replaces_reviewed_spans(self):
        glossary=rag.load_glossary()
        self.assertGreaterEqual(len(glossary),20)
        for key,english in glossary.items():
            self.assertTrue(all(t==t.lower() and t.isascii() for t in english),(key,english))
        base,added=rag.expand_terms("选课先修条件限制",glossary)
        self.assertTrue({"registration","prerequisite","restriction"}.issubset(set(added)),added)
        self.assertNotIn("选课",base,"a reviewed term is replaced by its English terms")
        self.assertIn("条件",base,"Chinese that no reviewed term covers keeps its bigram")

    def test_coverage_weights_an_unseen_term_as_heaviest(self):
        """df=0 must not be free: 'the corpus never used this word' is the signal."""
        rag.build([record(text="drop transcript credit transfer registration prerequisite guidance. "*8)],self.path)
        glossary={"退课":["drop"]}
        self.assertEqual(rag.term_stats(self.path,rag.search_terms("退课",glossary),now=NOW)[1],1.0)
        self.assertEqual(rag.term_stats(self.path,rag.search_terms("彩票号码",glossary),now=NOW)[0],[])
        self.assertEqual(rag.term_stats(self.path,rag.search_terms("彩票号码",glossary),now=NOW)[1],0.0)
        partial=rag.term_stats(self.path,rag.search_terms("drop penguin"),now=NOW)
        self.assertEqual(partial[0],["drop"])
        self.assertTrue(0.0<partial[1]<1.0,"an unseen term must cost coverage, not be free")

    def test_opt_in_coverage_gate_refuses_a_mostly_unseen_query_only_when_asked(self):
        rag.build([record(text="drop transcript credit transfer registration prerequisite guidance. "*8)],self.path)
        mixed="drop penguin submarine"
        self.assertTrue(rag.search(self.path,mixed,now=NOW),
                        "without the gate a partially matching query still returns hits")
        self.assertEqual(rag.search(self.path,mixed,now=NOW,min_coverage=0.5),[])
        self.assertTrue(rag.search(self.path,"drop transcript",now=NOW,min_coverage=0.5),
                        "a fully covered query must survive the same threshold")
        self.assertTrue(rag.search(self.path,mixed,now=NOW),
                        "the gate is opt-in: passing it once must not change later calls")
        for bad in (0, -0.1, 1.5):
            with self.assertRaises(rag.CorpusError): rag.search(self.path,mixed,now=NOW,min_coverage=bad)

    def test_coverage_ignores_a_script_the_corpus_never_uses(self):
        """A bigram of a script the snapshot has none of is not evidence either way."""
        rag.build([record(text="drop transcript credit transfer registration prerequisite guidance. "*8)],self.path)
        conn=rag.open_corpus(self.path)
        try: self.assertEqual(rag.corpus_script(conn),"latin")
        finally: conn.close()
        glossary={"退课":["drop"]}
        self.assertEqual(rag.term_stats(self.path,rag.search_terms("退课 彩票号码",glossary),now=NOW)[1],1.0,
                         "on an English corpus the Chinese bigrams must not drag coverage down")
        cjk_path=Path(self.tmp.name)/"cjk.sqlite"
        rag.build([record(text="退课与成绩单相关规定说明。"*20)],cjk_path)
        conn=rag.open_corpus(cjk_path)
        try: self.assertEqual(rag.corpus_script(conn),"cjk")
        finally: conn.close()
        coverage=rag.term_stats(cjk_path,rag.search_terms("退课 彩票号码"),now=NOW)[1]
        self.assertTrue(0.0<coverage<1.0,"on a Chinese corpus the bigram counts and partially matches")

    def test_calibrate_reports_the_trade_instead_of_picking_a_threshold(self):
        rag.build([record(s['id'],text=s['title']+' synthetic content '*20,fetched=rag.utcnow()) for s in rag.load_manifest(ROOT/'tools/rag_data/sources.json')],self.path)
        cases=json.loads((ROOT/"tools/rag_data/eval_cases.json").read_text())
        result=rag.calibrate(self.path,cases,thresholds=[0.05,0.5,0.95])
        self.assertEqual([p["min_coverage"] for p in result["curve"]],[0.05,0.5,0.95])
        refused=[len(p["positives_refused"]) for p in result["curve"]]
        self.assertEqual(refused,sorted(refused),"a higher threshold can only refuse more")
        self.assertIsNone(result.get("recommended_threshold"))

    def test_the_reviewed_glossary_is_frozen(self):
        """A held-out score only means something against one exact glossary."""
        digest=hashlib.sha256((ROOT/"tools/rag_data/glossary.json").read_bytes()).hexdigest()
        self.assertEqual(digest,PINNED_GLOSSARY_SHA256,
                         "the glossary changed, so every held-out number recorded in "
                         "tools/rag_data/README.md is now stale: re-measure, then move "
                         "this pin in the same commit")

    def test_both_case_sets_are_wellformed_and_share_the_same_sources(self):
        sources={s["id"] for s in rag.load_manifest(ROOT/'tools/rag_data/sources.json')}
        seen=set()
        for name in ("eval_cases.json","heldout_cases.json"):
            cases=json.loads((ROOT/"tools/rag_data"/name).read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(cases),30,name)
            self.assertEqual(len({c["id"] for c in cases}),len(cases),name)
            self.assertTrue(seen.isdisjoint({c["id"] for c in cases}),"case ids must not repeat across sets")
            seen|={c["id"] for c in cases}
            self.assertTrue(any(not c["expected"] for c in cases),name)
            for c in cases:
                self.assertTrue(set(c["expected"]).issubset(sources),(name,c["id"]))
                self.assertIn(c["language"],("en","zh","mixed"),(name,c["id"]))
                self.assertTrue(c["query"].strip(),(name,c["id"]))


if __name__ == "__main__": unittest.main()
