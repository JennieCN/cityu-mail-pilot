"""多语言的页面级判据：无损、覆盖、协商、以及**「英文页上不该再有中文」**。

分工说清楚：`test_i18n.py`（另一位写者的）盯 `t()` 自己的占位符行为；
这个文件盯**页面**。它证明四件事：

1. **中文路径逐字节没变。** 词典为空时 `translate_html(原文) == 原文`。这是
   「不推倒重来」在本模块里的可验证形式：两千多条既有测试里大量断言直接盯着中文
   原文，只要这条成立，它们就不可能因为这次改造变红。
2. **覆盖率。** 已校对的语言必须 100% 覆盖 `keys.json`；词典里也不许剩下源码里
   已经找不到的条目（孤儿）。少一条、多一条都点名说出来。
3. **译文没有破坏结构。** 链接守恒、占位符守恒。丢掉一个 `href` 就是丢掉一个法律
   页面入口，而这种事在 diff 里看不出来——两边都是通顺的句子。
4. **端到端真的换了语言。** 起一个真服务，按 `Accept-Language` / cookie /
   `?lang=` 三种入口各取一次，断言 HTTP 回执里的 `<html lang>` 与标题。

第 4 条里那条「英文页上不许有中文」是**唯一不依赖抽取器**的判据。2026-09-23 一天
里抽取器静默漏过两次（`i18n.mark(` 那一支被覆盖、局部别名 `say(` 不被认），两次
覆盖率都报 100%。抽取器只是工作清单；**渲染出来的页面才是判据**。
"""

import http.cookiejar
import json
import os
import pathlib
import re
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/i18n.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import i18n  # noqa: E402
from pilot_app import web  # noqa: E402

STATIC = pathlib.Path(web.__file__).resolve().parent / "static"
PAGES = ("landing.html", "privacy.html", "terms.html", "index.html")

#: 汉字里**本来就该留着**的那些：
#: * 版权人行名——术语表要求保留汉字，不音译；
#: * 切换器里的语言名——每种语言用**它自己的写法**列出来（日本語 / 한국어），
#:   译成 "Japanese" 反而让只看得懂日文的人找不到自己那一项。
ALLOWED_CJK = ("余剑篪", "简体中文", "繁體中文", "日本語", "한국어")


#: 「只在简体里出现」的字（我们语料里出现过的汉字 ∩ OpenCC「简→繁会变」的那些；
#: 由 `tools/i18n_proofread.py` 的同一份表生成，见 `docs/i18n-2026-09-23.md`）。
SIMPLIFIED_ONLY = set(
    "与专业东丢两个临为么义习书争于产仅从仓们价优会传伪体余侧偿储儿关内册写决况准几凭"
    "击则刚创删别剑办务动区协单卖占却压参双发变台号后吗启员响团园围国图场坏块声处备复"
    "够头奖学宁实审对导将尔尝尽届属岁师带帮并广库应开异弃张弯归当录态总惯户托执扫扰护"
    "报担择损换据携摄数断无旧时昵显暂术机权条来构标栏样检楼槛欧残毕汇没泄测浏滚滞满滤"
    "点状独现琐电画监盖盘着码础确离种称窥笔筛签简类紧约级纪纯线练组细终经结绕给络绝统"
    "继绩续维综绿编缩缴网罚联脚节苹范获营补装见规视览觉触计认让训议记讲许论设访证识诉"
    "词译试诚话询该详语误说请诺读课谁调负责败账质贴费资赔赖跃踪转软轻载辖辞边达过运还"
    "这进远违连迟适选遗遥邮采里钟钥钮钱链销错键长门闭问间阅队阶际陈险随隐隶雇静页顶项"
    "顺须频题颜额风馆马验"
)

def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def visible_text(markup: str) -> str:
    """页面上**看得见**的文字（去掉 script/style/注释/标签）。"""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return re.sub(r"<[^>]+>", " ", text)


#: 页面上**用户自己写的内容**那几块：留言板、公告栏。它们不翻译（见下），
#: 所以「英文页上不许有中文」这条要把它们摘出去。
USER_CONTENT_SECTIONS = ("guestbook", "board")


def without_user_content(markup: str) -> str:
    """把留言板与公告栏整块挖掉。

    这两块里是**用户与运营者自己写的字**，一个字都不该翻译——替用户改口供比
    不翻译严重得多。生产上它们本来就有中文（真实留言），所以要按 id 挖掉，
    否则这条测试只在「空库」上是绿的，一接真实数据就红。
    """
    for section in USER_CONTENT_SECTIONS:
        markup = re.sub(r'<section id="%s".*?</section>' % section, " ", markup, flags=re.S)
    return markup


def chinese_left(markup: str) -> list[str]:
    """页面上还剩哪些中文（去掉用户内容与允许保留的那几个词）。"""
    text = visible_text(without_user_content(markup))
    for allowed in ALLOWED_CJK:
        text = text.replace(allowed, " ")
    return sorted(set(re.findall(r"[\u4e00-\u9fff]+", text)))


class LosslessTests(unittest.TestCase):
    """中文路径必须与改造前逐字节一致。"""

    def test_the_default_language_is_the_source_itself(self):
        for name in PAGES:
            source = read(name)
            self.assertEqual(i18n.translate_html(source, "zh-Hans"), source, name)

    def test_the_rewriter_is_lossless_even_when_it_runs(self):
        """**强行让解析器跑满**，仍然逐字节不变。

        上面那条走的是快路径（词典为空直接返回）。快路径会掩盖解析器的错误：
        重新序列化时多一个引号、少一个斜杠，都不影响它。所以这里喂一个非空、
        但一条都命不中的词典，逼它走完「解析 → 找单元 → 替换」的全程。
        """
        dummy = {"__这一条不存在__": "x"}
        for name in PAGES:
            source = read(name)
            self.assertEqual(i18n.translate_html(source, "en", table=dummy), source, name)

    def test_a_replacement_does_not_shift_the_rest_of_the_document(self):
        """替换一处之后，其余部分仍逐字节等于原文。

        区间替换是从后往前做的；顺序写反的话，前面每换一次、后面的偏移就全错位，
        而结果往往还是一份「能看的 HTML」——所以这里逐字节比。
        """
        source = read("landing.html")
        keys, _fallback = i18n.unit_keys(source)
        target = next(k for k in keys if 8 < len(k) < 60 and "<" not in k and "{{" not in k)
        out = i18n.translate_html(source, "en", table={target: "ZZZZ"})
        self.assertIn("ZZZZ", out)
        self.assertEqual(out.replace("ZZZZ", target), source)


class ExtractorTests(unittest.TestCase):
    """抽取器自己也会坏，而且坏起来是静默的。"""

    def test_server_side_markers_are_found(self):
        """`i18n.mark(...)` 与 `_say(...)` 两支必须一直认得。

        2026-09-23 在这里栽过两次：改正则时覆盖了 `i18n.mark(`，于是 web.py 里
        30 条服务端文案一条都没抽到；`render_wechat_section` 用了局部别名 `say(`，
        8 句没抽到——而英文页上它们**就是中文**，覆盖率却报 0 缺。所以这条按
        「有没有这几句」断言，不按条数。
        """
        found = set(i18n._keys_document().get("code", {}).get("pilot_app/web.py", []))
        for probe in ("当前名额已满。", "邮箱或密码错误。", "字段 {name} 太短。",
                      "请先阅读并同意《隐私政策》与《服务条款》。",
                      "找到我们", "这张码 {when}有效", "下载安卓安装包（{size}）"):
            self.assertIn(probe, found, probe)

    def test_comments_and_css_are_not_units(self):
        """注释与 `<style>` 的内容不是待译原文。

        它们含中文（注释都是用中文写的），所以 `has_cjk` 拦不住；第一版把它们算进
        「兜底清单」，21 条注释混在 8 段真文案里，看上去像 21 句没译的界面文案。
        """
        source = (
            "<html><head><style>/* 中文注释 */ .a{color:red}</style></head>"
            "<body><!-- 中文注释 --><p>真的一句话</p></body></html>")
        required, fallback = i18n.unit_keys(source)
        self.assertEqual(required, ["真的一句话"])
        self.assertEqual(fallback, [])

    def test_a_container_is_not_one_unit(self):
        """纯容器的整块不能当一条 key，否则 `href` 会被吞进译文里。"""
        source = '<nav><a href="#how">它做什么</a><a href="#x">收件箱演示</a></nav>'
        required, _ = i18n.unit_keys(source)
        self.assertEqual(required, ["它做什么", "收件箱演示"])
        for key in required:
            self.assertNotIn("href", key)

    def test_one_sentence_with_inline_markup_is_one_unit(self):
        """被 `<b>` 切开的一句话**要**当一个单元：英文才能把语序整个倒过来。"""
        source = "<p>来信<b>转发</b>到你的私人邮箱。</p>"
        required, _ = i18n.unit_keys(source)
        self.assertEqual(required, ["来信<b>转发</b>到你的私人邮箱。"])

    def test_meta_content_is_translatable(self):
        """`<meta name="description">` 的 **content** 是待译的。

        第一版把 `description` 当成属性名写进白名单，于是一条 meta 都匹配不上：
        英文页的搜索摘要和分享卡片一直是中文，而**页面上看不见**。
        """
        source = ('<html><head><meta name="description" content="城市大学邮件助手">'
                  '<meta property="og:description" content="分享卡片那句话">'
                  '<meta name="viewport" content="width=device-width"></head></html>')
        required, _ = i18n.unit_keys(source)
        self.assertIn("城市大学邮件助手", required)
        self.assertIn("分享卡片那句话", required)
        self.assertNotIn("width=device-width", required)


class CoverageTests(unittest.TestCase):
    """已校对的语言必须译齐；词典里不许留孤儿。"""

    def test_reviewed_languages_are_complete(self):
        for item in i18n.locales():
            if not item["reviewed"] or item["code"] == i18n.DEFAULT_LOCALE:
                continue
            missing = i18n.missing(item["code"])
            self.assertEqual(missing, [], "%s 还差 %d 条：%s"
                             % (item["code"], len(missing), missing[:5]))

    def test_no_orphan_entries(self):
        """源码里已经找不到的条目要点名。

        界面改了一句话，旧译文就永远匹配不上。留着它不出错，但会让人以为
        「这句译过了」，也会让覆盖率的分母看起来比真实的大。
        """
        for item in i18n.locales():
            if item["code"] == i18n.DEFAULT_LOCALE:
                continue
            orphans = i18n.orphans(item["code"])
            self.assertEqual(orphans, [], "%s 有 %d 条孤儿：%s"
                             % (item["code"], len(orphans), orphans[:5]))

    def test_link_targets_survive_translation(self):
        """原文里每个 `href`，译文里都得在。

        丢掉一个链接就是丢掉一个法律页面入口（注册页那句「我已阅读并同意」正指着
        它们），而这种丢失在 diff 里**看不出来**——两边都是通顺的句子。
        """
        patterns = (re.compile(r'href="([^"]*)"'), re.compile(r"href='([^']*)'"))
        placeholders = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
        for item in i18n.locales():
            if item["code"] == i18n.DEFAULT_LOCALE:
                continue
            for key, value in i18n.catalog(item["code"]).items():
                want = sorted(h for p in patterns for h in p.findall(key))
                got = sorted(h for p in patterns for h in p.findall(value))
                self.assertEqual(got, want, "%s：%r 的链接对不上" % (item["code"], key[:40]))
                # 占位符同理：`{count}` 被译掉，页面上就会出现一个字面的 `{count}`。
                self.assertEqual(sorted(placeholders.findall(value)),
                                 sorted(placeholders.findall(key)),
                                 "%s：%r 的占位符对不上" % (item["code"], key[:40]))

    def test_english_values_carry_no_chinese(self):
        """英文词典里除专有名词外不该再有汉字——有就说明漏译了半句。"""
        for key, value in i18n.catalog("en").items():
            leftover = value
            for allowed in ALLOWED_CJK:
                leftover = leftover.replace(allowed, " ")
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", leftover),
                              "en 词典里 %r 还有汉字：%s" % (key[:30], value[:60]))


class TranslationTests(unittest.TestCase):
    """`t()` 自己的行为（占位符那一组在 `test_i18n.py` 里）。"""

    def test_unknown_text_falls_back_to_the_source(self):
        self.assertEqual(i18n.t("这句话还没译过。", "en"), "这句话还没译过。")
        self.assertEqual(i18n.t("这句话还没译过。", "zh-Hans"), "这句话还没译过。")

    def test_parameters_are_substituted_from_the_translation(self):
        """占位符按**译文**那一份来填：译文可以换顺序、可以丢掉中文的量词。

        这条以前拿「现在有 {count} 个账号接好了邮箱…」当载体；那句 2026-09-24 随 PR #10
        从页面上删掉了（四本词典里的孤儿也一起清了），所以换成还在用的客服群日期那句 ——
        它的英文是 `before {month}/{day}`：顺序换了、中文的「月/日」也没了，
        正好是这条测试要钉的东西。
        """
        self.assertEqual(i18n.t("{month} 月 {day} 日前", "en", month=9, day=29), "before 9/29")


class NegotiationTests(unittest.TestCase):
    """语言是怎么选出来的。"""

    def test_chinese_is_split_by_script_not_by_country(self):
        # 香港/台湾/澳门是繁体；这条判断写错，香港用户打开就是简体。
        for tag in ("zh-HK", "zh-TW", "zh-MO", "zh-Hant", "zh-Hant-HK"):
            self.assertEqual(i18n.match_locale(tag), "zh-Hant", tag)
        for tag in ("zh-CN", "zh-SG", "zh", "zh-Hans"):
            self.assertEqual(i18n.match_locale(tag), "zh-Hans", tag)

    def test_region_falls_back_to_the_language(self):
        self.assertEqual(i18n.match_locale("en-GB"), "en")
        self.assertEqual(i18n.match_locale("en"), "en")
        self.assertEqual(i18n.match_locale("de-DE"), "")

    def test_quality_values_decide_the_order(self):
        self.assertEqual(i18n.negotiate("en;q=0.3, zh-Hant;q=0.9, fr;q=0.8"), "zh-Hant")
        # q=0 是「明确不要」：整条跳过，落到下一个认得的。
        self.assertEqual(i18n.negotiate("zh-Hant;q=0, en;q=0.5"), "en")

    def test_the_account_beats_the_cookie_beats_the_header(self):
        self.assertEqual(i18n.negotiate("en", "zh-Hant", "zh-Hans"), "zh-Hans")
        self.assertEqual(i18n.negotiate("en", "zh-Hant", ""), "zh-Hant")
        self.assertEqual(i18n.negotiate("en", "", ""), "en")
        # 认不出来的偏好当作没说，而不是当作「中文」把后面两个入口一起短路掉。
        self.assertEqual(i18n.negotiate("en", "xx", "yy"), "en")

    def test_nothing_known_falls_back_to_chinese(self):
        self.assertEqual(i18n.negotiate(""), "zh-Hans")
        self.assertEqual(i18n.negotiate("de-DE,fr;q=0.7"), "zh-Hans")

    def test_the_default_locale_is_never_counted_as_zero_translated(self):
        """中文是**原文**，不是「译了 0 条」——第一版如实报 0，切换器上就成了
        「简体中文 0/526」，一句既难看又不对的话。"""
        row = next(x for x in i18n.registry()["locales"] if x["code"] == "zh-Hans")
        self.assertEqual(row["translated"], row["total"])
        self.assertGreater(row["total"], 0)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        # `ProxyHandler({})` 不是装饰：本机（或 CI）环境里设了 http_proxy 时，
        # 默认 opener 会把请求发给代理，而这些客户端打的是 127.0.0.1 上的预览服务——
        # 一旦走代理，测试就成了在测代理的行为。`test_ci.TestClientProxyTests` 会拦。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))

    def get_json(self, path: str, headers=None):
        status, body, response_headers = self.get(path, headers)
        return status, json.loads(body), response_headers

    def get(self, path: str, headers=None):
        request = urllib.request.Request(self.base + path)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8"), dict(error.headers)

    def post(self, path: str, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        with self.opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8")), dict(response.headers)


class ServedPageTests(unittest.TestCase):
    """端到端：真起一个服务，按三种入口各取一次。"""

    @classmethod
    def setUpClass(cls):
        cls.database = web.get_db()
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def client(self) -> Client:
        return Client(self.base)

    def test_accept_language_switches_the_page(self):
        cases = [("en-US,en;q=0.9", "en", "Turn your CityU email"),
                 ("zh-HK,zh;q=0.9", "zh-Hant", "把學校電郵變成"),
                 ("zh-CN,zh;q=0.9", "zh-Hans", "把学校邮件变成")]
        for header, lang, needle in cases:
            _status, body, headers = self.client().get("/", {"Accept-Language": header})
            self.assertIn('<html lang="%s"' % lang, body, header)
            self.assertIn(needle, body, header)
            self.assertEqual(headers.get("Content-Language"), lang)
            # 同一个 URL、不同语言 ⇒ 缓存必须按这两个头分开。
            self.assertIn("Accept-Language", headers.get("Vary", ""))

    def test_the_english_page_has_no_chinese_left(self):
        """**最要紧的一条：英文页上不该再有中文。**

        这是唯一不依赖抽取器的判据——它直接看渲染出来的页面，所以别名、正则、
        覆盖率的谎都瞒不过它。（允许保留的只有版权行名与切换器里的语言自称。）
        """
        _status, body, _headers = self.client().get("/", {"Accept-Language": "en"})
        self.assertEqual(chinese_left(body), [])

    def test_japanese_and_korean_pages_have_no_untranslated_chinese(self):
        """日文/韩文页面上也不该**整段**留着中文 —— 而这一条抽取器永远抓不到。

        `keys.json` 是覆盖率棘轮的基准，而基准来自 `tools/i18n_extract.py`。抽取器有盲区：
        `<li><b>下载</b>：用手机上的浏览器打开这一页…<figure>…</figure></li>` 这种
        「行内标签后面接一段文字、块级子元素又在同一个 `li` 里」的写法，整条不被当成单元、
        那段文字也没登记 —— 于是 keys.json 里根本没有它们，覆盖率报 100%，
        **而日文/韩文页面上那 8 条一直是中文**（2026-09-23 真实发生过：安卓安装步骤）。

        判据不看 keys.json，直接看页面：日文用汉字、韩文偶有汉字，所以不能像英文那样
        「见汉字就判错」。这里用的是**连续 ≥3 个「只该出现在简体里」的字**：
        日文新字体与简体同形的那些（体/国/会/学/当/数…）单字不算，成串的才是漏译。
        """
        tables = {
            lang: json.loads((pathlib.Path("pilot_app/static/i18n") / f"{lang}.json")
                             .read_text(encoding="utf-8"))
            for lang in ("ja", "ko")
        }
        for lang in ("ja", "ko"):
            # 这份表 = 「OpenCC 认为简→繁会变」∩「该语言的译文里从没用过」：
            # 一个字符如果在这门语言的词典里从没出现过，它出现在**页面**上就很可疑。
            used = set("".join(tables[lang].values()))
            suspicious = {c for c in SIMPLIFIED_ONLY if c not in used}
            for path in ("/", "/privacy", "/terms"):
                _status, body, _headers = self.client().get(
                    path, {"Accept-Language": lang})
                # 语言切换器**整块跳过**：那里面每种语言用自己的文字写自己的名字
                # （「简体中文」「繁體中文」「日本語」），是**故意**留着的中文，
                # 一个中文用户来读日文页面时正需要看见它。
                body = re.sub(r"<form[^>]*lang-switch.*?</form>", " ", body, flags=re.S)
                text = visible_text(body)
                runs = re.findall(r"[%s]{2,}" % "".join(sorted(suspicious)), text)
                self.assertEqual(runs, [],
                                 f"{lang} {path} 上还有成串的简体字（多半是没翻的段落）：{runs[:3]}")


    def test_the_english_legal_pages_have_no_chinese_left(self):
        for path in ("/privacy", "/terms"):
            _status, body, _headers = self.client().get(path, {"Accept-Language": "en"})
            self.assertEqual(chinese_left(body), [], path)

    def test_user_written_content_is_never_translated(self):
        """留言正文与昵称是用户写的，翻译它等于替用户改口供。

        判据不是「看一下」，而是**塞一条中文留言进去**，再断言英文页上它一字未改
        ——同时周围的界面文案是英文。「留言不等于注册」是隐私政策里的承诺，
        这条测试是那句承诺在 i18n 这一层的对应物。
        """
        body = "这是一条只有中文的测试留言"
        nickname = "测试昵称"
        row = self.database.create_guest_message(body=body, nickname=nickname)
        self.database.set_guest_message_status(row["id"], "published", actor="test")
        i18n.reset_cache()
        _status, page, _headers = self.client().get("/", {"Accept-Language": "en"})
        self.assertIn(body, page)
        self.assertIn(nickname, page)
        # 而同一页的界面文案是英文的。
        self.assertIn("Turn your CityU email", page)

    def test_a_translated_option_keeps_its_value(self):
        """**会被翻译的表单控件，必须显式带 `value`。**（2026-09-23 生产级回归）

        没有 `value` 的 `<option>`，它的 `.value` 就是**它的文字**；而文字会被改写器
        翻掉。于是英文页上选 "Undergraduate" 提交，`app.js` 送出的是
        `identity=Undergraduate`，服务端 `SIGNUP_IDENTITIES=('本科生','研究生','其他')`
        不认 → **注册 422**。繁体因为繁简同形侥幸没露，日韩会同样中招。

        属性值不在翻译白名单里，所以 `value="本科生"` 里是中文没关系——**要的正是它**。
        """
        wanted = set(web.SIGNUP_IDENTITIES)
        self.assertTrue(wanted, "服务端至少得有一个可选身份，否则这条测试没有意义")
        for lang in ("zh-Hans", "en", "zh-Hant"):
            page = i18n.translate_html(read("index.html"), lang)
            select = re.search(r'<select id="reg-identity".*?</select>', page, re.S)
            self.assertIsNotNone(select, lang)
            values = re.findall(r'<option([^>]*)>([^<]*)</option>', select.group(0))
            self.assertEqual(len(values), len(wanted) + 1, lang)   # +1 是「不填」
            for attrs, text in values:
                explicit = re.search(r'value="([^"]*)"', attrs)
                self.assertIsNotNone(explicit, "%s：option %r 没有 value 属性" % (lang, text))
                value = explicit.group(1)
                if value:
                    self.assertIn(value, wanted, "%s：option 的 value 不是服务端认的取值" % lang)
            # 中文那边文字与取值仍然一致（没被这次修复改坏）。
            if lang == "zh-Hans":
                self.assertEqual({re.search(r'value="([^"]*)"', attrs).group(1)
                                  for attrs, _text in values}, {""} | wanted)

    def test_list_parameters_are_rendered_in_the_target_language(self):
        """报错参数里的**列表**要各自过词典。

        「身份只能是：本科生、研究生、其他」——那三个中文既是显示文字、也是存进库的
        取值，所以代码里必须留着中文；但拼好的字符串在译文里没人翻，英文页上就成了
        「one of: 本科生、研究生、其他」。传元组、由 `dispatch` 拼，两边都成立。
        """
        self.assertEqual(web._render_param(web.SIGNUP_IDENTITIES, "en"),
                         "Undergraduate, Postgraduate, Other")
        self.assertEqual(web._render_param(web.SIGNUP_IDENTITIES, "zh-Hans"),
                         "本科生、研究生、其他")
        # 普通字符串原样传过去（调用方已经拼好了）。
        self.assertEqual(web._render_param("abc", "en"), "abc")

    def test_the_optional_sections_are_translated_too(self):
        """**默认不渲染的那些分支也要跟着翻译。**

        客服群那一节要有配了图片 + 日期才出现，所以「英文页上没有中文」那条盖不到它。
        实测踩过：那两句在 `web.py` 里是**相邻字面量拼接**写的

            _say("前半句。"
                 "后半句。", locale)

        抽取器只抓到第一个字面量，词典里于是存了一条**永远匹配不上的半句**，而覆盖率
        报「0 缺」——是日语译者问「这句话在清单里找不到」才发现的。这条测试把两种
        分支（码还有效 / 码已过期）都渲染一遍。
        """
        live = {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                "INFE_PILOT_WECHAT_GROUP_UNTIL": "2099-12-31"}
        expired = {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                   "INFE_PILOT_WECHAT_GROUP_UNTIL": "2000-01-01"}
        for label, env, needle in (("有效", live, "Scan to join the group"),
                                   ("过期", expired, "To reach us")):
            with mock.patch.dict(os.environ, env):
                page = web.render_landing_page(STATIC / "landing.html", "en").decode("utf-8")
            self.assertIn(needle, page, label)
            self.assertEqual(chinese_left(page), [], label)
            # 中文那边也得是中文（别把「翻译」做成「两边都被替换」）。
            with mock.patch.dict(os.environ, env):
                zh = web.render_landing_page(STATIC / "landing.html", "zh-Hans").decode("utf-8")
            self.assertIn("扫码进群", zh, label)

    def test_the_login_screen_follows_the_language(self):
        _status, body, _headers = self.client().get("/app", {"Accept-Language": "en"})
        self.assertIn('<html lang="en"', body)
        self.assertIn("Sign in / Sign up", body)
        # 切换器必须在登录屏上：还没登录的人正是要选语言的人。
        self.assertIn('id="lang-switch"', body)

    def test_the_signed_in_shell_stays_chinese_on_purpose(self):
        """第一轮只做未登录可见的公开面，所以 `#dashboard` 带 `data-i18n-skip`。

        这条不是为了赞美现状，而是**把现状钉住**：第二轮删掉那个属性时它会红，
        那时棘轮会要求补齐登录后那三百多句——正是我们想要的提醒。
        """
        self.assertIn('data-i18n-skip', read("index.html"))
        _status, body, _headers = self.client().get("/app", {"Accept-Language": "en"})
        self.assertIn("data-i18n-skip", body)
        self.assertNotIn('<html lang="zh-Hans"', body)

    def test_an_explicit_choice_is_remembered(self):
        """`?lang=` 的意义是「以后都用它」，所以它必须写 cookie。"""
        client = self.client()
        _status, body, headers = client.get("/?lang=en", {"Accept-Language": "zh-CN"})
        self.assertIn('<html lang="en"', body)
        self.assertIn("cityu_mail_lang=en", headers.get("Set-Cookie", ""))
        # 下一次请求只带 cookie（浏览器会自动带），不该又变回中文。
        _status, body, _headers = client.get("/", {"Accept-Language": "zh-CN"})
        self.assertIn('<html lang="en"', body)

    def test_the_switch_form_survives_without_scripting(self):
        """切换器是一个表单 + `<noscript>` 里的按钮。

        不能靠 `onchange="…"`：CSP 是 `script-src 'self'`，内联事件处理器会被
        浏览器静默拦掉——页面上有切换器，选了没反应。也不用 `<a>`：`?lang=` 是
        可分享的地址，而链接要每个语言手写一份 URL。
        """
        _status, body, _headers = self.client().get("/")
        self.assertIn('<form class="lang-switch" method="get"', body)
        self.assertIn('<select id="lang-switch" name="lang">', body)
        self.assertIn('<noscript><button type="submit">', body)
        self.assertIn('src="/i18n.js"', body)
        self.assertNotIn("onchange=", body)

    def test_language_metadata_is_advertised(self):
        _status, body, _headers = self.client().get("/")
        self.assertIn('hreflang="en"', body)
        self.assertIn('hreflang="zh-Hant"', body)
        self.assertIn('hreflang="x-default"', body)

    def test_a_language_with_no_dictionary_is_not_offered(self):
        """**一门词典都没写的语言，不许出现在切换器上。**

        2026-09-23 实测出来的缺陷：`locales.json` 里先加了 `ja`/`ko` 而词典还没写，
        于是切换器提供「日本語」，选了之后拿到的是 `<html lang="ja">` + **中文正文**
        ——那不只是没用，是对读屏软件说了假话（它按 `lang` 选发音）。宁可暂时不提供。

        这条测的是**机制**（不是「现在有没有日语」）：把某一门的词典假装成空的，
        它就该从切换器和 hreflang 里消失，而其余几门不受影响。
        """
        original = i18n.catalog
        i18n.catalog = lambda code: ({} if code == "ko" else original(code))  # type: ignore[assignment]
        try:
            markup = web.render_language_switch("en")
            hrefs = [item["hreflang"] for item in i18n.alternates("/")]
        finally:
            i18n.catalog = original  # type: ignore[assignment]
        self.assertNotIn("한국어", markup)
        self.assertNotIn("ko", hrefs)
        for label in ("简体中文", "English", "繁體中文", "日本語"):
            self.assertIn(label, markup, label)

    def test_the_draft_languages_are_offered_and_labelled(self):
        """有词典但没人校对过的语言：**要提供，但要自称「初译」**。

        并排放着而不加标记，等于替它担保——而这些语言的译文没有人从头读过。
        """
        _status, body, _headers = self.client().get("/", {"Accept-Language": "en"})
        self.assertIn("日本語", body)
        self.assertIn("한국어", body)
        self.assertIn("(draft)", body)

    def test_the_optional_sections_are_translated_too(self):
        """**默认不渲染的那些分支也要跟着翻译。**

        客服群那一节要有配了图片 + 日期才出现，所以「英文页上没有中文」那条盖不到它。
        实测踩过：那两句在 `web.py` 里是**相邻字面量拼接**写的

            _say("前半句。"
                 "后半句。", locale)

        抽取器只抓到第一个字面量，词典里于是存了一条**永远匹配不上的半句**，而覆盖率
        报「0 缺」——是日语译者问「这句话在清单里找不到」才发现的。这条测试把两种
        分支（码还有效 / 码已过期）都渲染一遍。
        """
        live = {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                "INFE_PILOT_WECHAT_GROUP_UNTIL": "2099-12-31"}
        expired = {"INFE_PILOT_WECHAT_GROUP_IMG": "/wechat-group.png",
                   "INFE_PILOT_WECHAT_GROUP_UNTIL": "2000-01-01"}
        for label, env, needle in (("有效", live, "Scan to join the group"),
                                   ("过期", expired, "To reach us")):
            with mock.patch.dict(os.environ, env):
                page = web.render_landing_page(STATIC / "landing.html", "en").decode("utf-8")
            self.assertIn(needle, page, label)
            self.assertEqual(chinese_left(page), [], label)
            # 中文那边也得是中文（别把「翻译」做成「两边都被替换」）。
            with mock.patch.dict(os.environ, env):
                zh = web.render_landing_page(STATIC / "landing.html", "zh-Hans").decode("utf-8")
            self.assertIn("扫码进群", zh, label)

    def test_the_login_screen_follows_the_language(self):
        _status, body, _headers = self.client().get("/app", {"Accept-Language": "en"})
        self.assertIn('<html lang="en"', body)
        self.assertIn("Sign in / Sign up", body)
        # 切换器必须在登录屏上：还没登录的人正是要选语言的人。
        self.assertIn('id="lang-switch"', body)

    def test_the_signed_in_shell_stays_chinese_on_purpose(self):
        """第一轮只做未登录可见的公开面，所以 `#dashboard` 带 `data-i18n-skip`。

        这条不是为了赞美现状，而是**把现状钉住**：第二轮删掉那个属性时它会红，
        那时棘轮会要求补齐登录后那三百多句——正是我们想要的提醒。
        """
        self.assertIn('data-i18n-skip', read("index.html"))
        _status, body, _headers = self.client().get("/app", {"Accept-Language": "en"})
        self.assertIn("data-i18n-skip", body)
        self.assertNotIn('<html lang="zh-Hans"', body)

    def test_an_explicit_choice_is_remembered(self):
        """`?lang=` 的意义是「以后都用它」，所以它必须写 cookie。"""
        client = self.client()
        _status, body, headers = client.get("/?lang=en", {"Accept-Language": "zh-CN"})
        self.assertIn('<html lang="en"', body)
        self.assertIn("cityu_mail_lang=en", headers.get("Set-Cookie", ""))
        # 下一次请求只带 cookie（浏览器会自动带），不该又变回中文。
        _status, body, _headers = client.get("/", {"Accept-Language": "zh-CN"})
        self.assertIn('<html lang="en"', body)

    def test_the_switch_form_survives_without_scripting(self):
        """切换器是一个表单 + `<noscript>` 里的按钮。

        不能靠 `onchange="…"`：CSP 是 `script-src 'self'`，内联事件处理器会被
        浏览器静默拦掉——页面上有切换器，选了没反应。也不用 `<a>`：`?lang=` 是
        可分享的地址，而链接要每个语言手写一份 URL。
        """
        _status, body, _headers = self.client().get("/")
        self.assertIn('<form class="lang-switch" method="get"', body)
        self.assertIn('<select id="lang-switch" name="lang">', body)
        self.assertIn('<noscript><button type="submit">', body)
        self.assertIn('src="/i18n.js"', body)
        self.assertNotIn("onchange=", body)

    def test_language_metadata_is_advertised(self):
        _status, body, _headers = self.client().get("/")
        self.assertIn('hreflang="en"', body)
        self.assertIn('hreflang="zh-Hant"', body)
        self.assertIn('hreflang="x-default"', body)

    def test_a_language_with_no_dictionary_is_not_offered(self):
        """**一门词典都没写的语言，不许出现在切换器上。**

        2026-09-23 实测出来的缺陷：`locales.json` 里先加了 `ja`/`ko` 而词典还没写，
        于是切换器提供「日本語」，选了之后拿到的是 `<html lang="ja">` + **中文正文**
        ——那不只是没用，是对读屏软件说了假话（它按 `lang` 选发音）。宁可暂时不提供。

        这条测的是**机制**（不是「现在有没有日语」）：把某一门的词典假装成空的，
        它就该从切换器和 hreflang 里消失，而其余几门不受影响。
        """
        original = i18n.catalog
        i18n.catalog = lambda code: ({} if code == "ko" else original(code))  # type: ignore[assignment]
        try:
            markup = web.render_language_switch("en")
            hrefs = [item["hreflang"] for item in i18n.alternates("/")]
        finally:
            i18n.catalog = original  # type: ignore[assignment]
        self.assertNotIn("한국어", markup)
        self.assertNotIn("ko", hrefs)
        for label in ("简体中文", "English", "繁體中文", "日本語"):
            self.assertIn(label, markup, label)

    def test_the_draft_languages_are_offered_and_labelled(self):
        """有词典但没人校对过的语言：**要提供，但要自称「初译」**。

        并排放着而不加标记，等于替它担保——而这些语言的译文没有人从头读过。
        """
        _status, body, _headers = self.client().get("/", {"Accept-Language": "en"})
        self.assertIn("日本語", body)
        self.assertIn("한국어", body)
        self.assertIn("(draft)", body)

    def test_an_unreviewed_language_is_labelled_as_a_draft(self):
        """有词典但**没人校对过**的语言，要自称「初译」。

        并排放着而不加标记，等于替它担保——而这些语言的译文没有人看过。
        这里用「给 ja 造一份假词典」来验（真实的 ja/ko 现在也确实是未校对的），
        并断言**校对过的那几门不带这个标记**。
        """
        original = i18n.catalog
        i18n.catalog = lambda code: ({"语言": "言語"} if code == "ja" else original(code))  # type: ignore[assignment]
        try:
            markup = web.render_language_switch("en")
            unreviewed = [item["code"] for item in i18n.offered() if not item["reviewed"]]
        finally:
            i18n.catalog = original  # type: ignore[assignment]
        self.assertIn("日本語", markup)
        self.assertIn("(draft)", markup)
        # 标记的**份数**与「未校对语言的个数」一致：不多标一门，也不少标一门。
        self.assertEqual(markup.count("(draft)"), len(unreviewed))
        self.assertNotIn("简体中文(draft)", markup)
        self.assertNotIn("English(draft)", markup)
        self.assertNotIn("繁體中文(draft)", markup)


class LocaleApiTests(unittest.TestCase):
    """`/api/locale` 与词典路由。"""

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def client(self) -> Client:
        return Client(self.base)

    def test_it_answers_without_signing_in(self):
        """切换器长在介绍页和登录屏上——看这两块的人正是还没登录的人。"""
        status, body, _headers = self.client().get_json("/api/locale")
        self.assertEqual(status, 200)
        self.assertEqual(body["default"], "zh-Hans")
        self.assertEqual(body["current"], "zh-Hans")
        self.assertTrue(any(x["code"] == "en" for x in body["locales"]))

    def test_it_reports_the_language_the_request_is_in(self):
        _status, body, _headers = self.client().get_json("/api/locale", {"Accept-Language": "en-GB"})
        self.assertEqual(body["current"], "en")

    def test_posting_a_language_sets_the_cookie(self):
        status, body, headers = self.client().post("/api/locale", {"locale": "zh-Hant"})
        self.assertEqual(status, 200)
        self.assertEqual(body["current"], "zh-Hant")
        self.assertIn("cityu_mail_lang=zh-Hant", headers.get("Set-Cookie", ""))

    def test_an_unknown_language_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.client().post("/api/locale", {"locale": "klingon"})
        self.assertEqual(caught.exception.code, 422)

    def test_the_catalog_is_served_for_known_languages_only(self):
        status, body, _headers = self.client().get("/i18n/en.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body).get("语言"), "Language")

    def test_an_unknown_catalog_is_a_404_not_a_path(self):
        """`name` 来自 URL，直接拼路径就是一次目录穿越。"""
        for path in ("/i18n/zz.json", "/i18n/en.txt", "/i18n/passwd.json"):
            status, _body, _headers = self.client().get(path)
            self.assertIn(status, (400, 404), path)

    def test_the_source_language_serves_an_empty_catalog(self):
        """中文没有词典文件（它就是原文）——空表让 JS 原样显示中文。"""
        status, body, _headers = self.client().get("/i18n/zh-Hans.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})


if __name__ == "__main__":
    unittest.main()
