import unittest
from unittest import mock

from pilot_app import pricing, providers


class ProviderTests(unittest.TestCase):
    def test_catalog_includes_common_and_custom_providers(self):
        ids = {item["id"] for item in providers.public_catalog()["models"]}
        self.assertTrue({"openai", "anthropic", "gemini", "volcengine_ark", "deepseek", "qwen", "custom_openai"}.issubset(ids))
        self.assertEqual(providers.MODEL_PRESETS["together"].base_url, "https://api.together.ai/v1")

    def test_openai_responses_disables_storage(self):
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as request:
            self.assertEqual(providers.generate_text(provider="openai", model="gpt-test", api_key="secret", prompt="hello"), "ok")
        payload = request.call_args.kwargs["payload"]
        self.assertIs(payload["store"], False)

    def test_gemini_keeps_key_out_of_url(self):
        response = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            providers.generate_text(provider="gemini", model="gemini-test", api_key="top-secret", prompt="hello")
        self.assertNotIn("top-secret", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["headers"]["x-goog-api-key"], "top-secret")

    def test_search_filters_unsafe_result_urls(self):
        response = {"results": [
            {"title": "good", "url": "https://example.com/a", "content": "x"},
            {"title": "bad", "url": "javascript:alert(1)", "content": "x"},
        ]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            results = providers.web_search("tavily", "secret", "CityU communication engineering")
        self.assertEqual([item["title"] for item in results], ["good"])

    # -- 「答案是被砍断的」要能从返回值里看出来 ------------------------------

    def test_a_chat_answer_cut_off_at_the_cap_reports_length(self):
        """文本本身看不出「写完了」和「被砍了」——半句译文和短信长得一样。

        「看原信」的翻译要么把这件事说出来，要么用户以为信就到这里。所以
        `Generation.finish` 把各家自己的说法归一到 `"length"`。
        """
        response = {"choices": [{"finish_reason": "length", "message": {"content": "半句话"}}]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="deepseek", model="deepseek-flash",
                                        api_key="k", prompt="hello")
        self.assertEqual(result.finish, "length")

    def test_the_responses_protocol_says_it_differently(self):
        response = {"output_text": "半句话", "incomplete_details": {"reason": "max_output_tokens"}}
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="openai", model="gpt-test", api_key="k", prompt="hello")
        self.assertEqual(result.finish, "length")

    def test_a_normal_stop_is_not_mistaken_for_truncation(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "完整"}}]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="deepseek", model="deepseek-flash",
                                        api_key="k", prompt="hello")
        self.assertEqual(result.finish, "stop")


    # -- an empty answer is a failure, not a blank report ------------------

    def test_reasoning_model_that_burns_its_budget_is_reported_clearly(self):
        """deepseek-flash really does this: all 4000 max_tokens came back as
        reasoning_tokens, finish_reason "length", content "". Returning "" would
        be rendered as a seven-section report whose every line says "无"."""
        response = {
            "choices": [{"finish_reason": "length",
                         "message": {"role": "assistant", "content": "",
                                     "reasoning_content": "We need answer strictly format..."}}],
            "usage": {"completion_tokens": 4000,
                      "completion_tokens_details": {"reasoning_tokens": 4000}},
        }
        with mock.patch.object(providers, "_json_request", return_value=response):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate(provider="deepseek", model="deepseek-flash",
                                   api_key="k", prompt="p", max_output_tokens=4000)
        message = str(caught.exception)
        self.assertIn("隐藏推理", message)
        self.assertIn("thinking", message, "错误信息必须给出可执行的下一步："
                      "关掉思考开关，而不是让人去换模型名")

    def test_a_few_stray_characters_after_exhausted_reasoning_also_fail(self):
        """Measured on the real API: one run returned 5 characters after 3995
        reasoning tokens. "Not empty" is not the same as "usable"."""
        response = {
            "choices": [{"finish_reason": "length",
                         "message": {"content": "\n\n无。", "reasoning_content": "thinking..."}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 3995}},
        }
        with mock.patch.object(providers, "_json_request", return_value=response):
            with self.assertRaises(providers.ProviderError):
                providers.generate(provider="deepseek", model="deepseek-flash",
                                   api_key="k", prompt="p", max_output_tokens=4000)

    def test_a_long_answer_that_used_reasoning_is_not_rejected(self):
        """A reasoning model that thinks and then writes a real report is fine —
        the guard is about wasted budgets, not about reasoning itself."""
        response = {
            "choices": [{"finish_reason": "stop", "message": {"content": "结论：" + "内容" * 300}}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 3500}},
        }
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="deepseek", model="m", api_key="k",
                                        prompt="p", max_output_tokens=4000)
        self.assertGreater(len(result.text), 400)

    def test_plain_empty_answer_is_also_an_error(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate(provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertIn("空正文", str(caught.exception))

    def test_a_normal_answer_still_returns(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": " 结论：通过 "}}]}
        with mock.patch.object(providers, "_json_request", return_value=response):
            result = providers.generate(provider="deepseek", model="deepseek-chat", api_key="k", prompt="p")
        self.assertEqual(result.text, "结论：通过")

    def test_a_missing_choices_key_does_not_crash(self):
        with mock.patch.object(providers, "_json_request", return_value={"error": "weird body"}):
            with self.assertRaises(providers.ProviderError):
                providers.generate(provider="deepseek", model="m", api_key="k", prompt="p")

    # -- native web search -------------------------------------------------

    def test_native_search_capability_is_declared_and_published(self):
        self.assertTrue(providers.supports_native_search("openai"))
        self.assertTrue(providers.supports_native_search("anthropic"))
        self.assertTrue(providers.supports_native_search("gemini"))
        # OpenAI-compatible providers have no built-in search of their own.
        for provider in ("deepseek", "qwen", "openrouter", "custom_openai", "azure_openai"):
            self.assertFalse(providers.supports_native_search(provider), provider)
        catalog = {item["id"]: item for item in providers.public_catalog()["models"]}
        self.assertTrue(catalog["openai"]["native_search"])
        self.assertFalse(catalog["deepseek"]["native_search"])
        self.assertIn("native_search", catalog["custom_openai"])

    def test_openai_native_search_sends_tool_and_reads_citations(self):
        response = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "report body", "annotations": [
                {"type": "url_citation", "title": "Source A", "url": "https://example.com/a"},
                {"type": "url_citation", "title": "dup", "url": "https://example.com/a"},
                {"type": "url_citation", "title": "bad", "url": "javascript:alert(1)"},
            ]},
        ]}]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(provider="openai", model="gpt-test", api_key="secret",
                                        prompt="hello", native_search=True)
        self.assertEqual(result.text, "report body")
        self.assertEqual(result.search_mode, "native")
        self.assertEqual([item["url"] for item in result.sources], ["https://example.com/a"])
        self.assertEqual(request.call_args.kwargs["payload"]["tools"], [{"type": "web_search"}])

    def test_anthropic_native_search_sends_server_tool(self):
        response = {"content": [
            {"type": "text", "text": "report body"},
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "title": "Source B", "url": "https://example.com/b"},
            ]},
        ]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(provider="anthropic", model="claude-test", api_key="secret",
                                        prompt="hello", native_search=True)
        self.assertEqual(result.text, "report body")
        self.assertEqual([item["url"] for item in result.sources], ["https://example.com/b"])
        self.assertEqual(
            request.call_args.kwargs["payload"]["tools"],
            [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        )

    def test_gemini_native_search_sends_grounding_tool(self):
        response = {"candidates": [{
            "content": {"parts": [{"text": "report body"}]},
            "groundingMetadata": {"groundingChunks": [{"web": {"title": "Source C", "uri": "https://example.com/c"}}]},
        }]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(provider="gemini", model="gemini-test", api_key="secret",
                                        prompt="hello", native_search=True)
        self.assertEqual(result.text, "report body")
        self.assertEqual([item["url"] for item in result.sources], ["https://example.com/c"])
        self.assertEqual(request.call_args.kwargs["payload"]["tools"], [{"google_search": {}}])

    def test_native_search_is_not_sent_unless_requested_or_supported(self):
        response = {"choices": [{"message": {"content": "ok"}}]}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            providers.generate(provider="deepseek", model="deepseek-chat", api_key="secret",
                               prompt="hello", native_search=True)
        self.assertNotIn("tools", request.call_args.kwargs["payload"])

        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as request:
            result = providers.generate(provider="openai", model="gpt-test", api_key="secret", prompt="hello")
        self.assertNotIn("tools", request.call_args.kwargs["payload"])
        self.assertEqual(result.sources, [])
        self.assertEqual(result.search_mode, "none")

    def test_model_calls_allow_slow_report_generation(self):
        # A real Doubao run exceeded the 120s default, so the generation calls
        # must pass an explicit, longer timeout.
        self.assertGreaterEqual(providers.MODEL_TIMEOUT_SECONDS, 180)
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as request:
            providers.generate(provider="openai", model="gpt-test", api_key="secret", prompt="hello")
        self.assertEqual(request.call_args.kwargs["timeout"], providers.MODEL_TIMEOUT_SECONDS)

    def test_read_timeout_becomes_an_actionable_error(self):
        import socket
        with mock.patch.object(providers.urllib.request, "urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(providers.ProviderError) as ctx:
                providers._json_request("https://example.com/x", headers={}, payload={"a": 1}, timeout=7)
        self.assertIn("超时", str(ctx.exception))

    def test_generate_text_still_returns_plain_text(self):
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}):
            self.assertEqual(
                providers.generate_text(provider="openai", model="gpt-test", api_key="secret", prompt="hello"),
                "ok",
            )


class LegacyModelAliasTests(unittest.TestCase):
    """A name the provider stopped documenting must not be what we send.

    Measured 2026-09-15 on the real API: only ``deepseek-flash`` and
    ``deepseek-v4-pro`` are listed, and asking for ``deepseek-chat`` is answered
    by the Flash model. Continuing to *request* the retired name works today and
    breaks the day the provider drops it — with the failure landing on real
    users' reports.
    """

    def test_the_alias_is_mapped_forward(self):
        self.assertEqual(providers.official_model_name("deepseek", "deepseek-chat"), "deepseek-flash")
        # Case and stray whitespace come from a text box, not from a machine.
        self.assertEqual(providers.official_model_name("DeepSeek", " DeepSeek-Chat "), "deepseek-flash")

    def test_everything_else_is_left_alone(self):
        for provider, model in (("deepseek", "deepseek-v4-pro"), ("deepseek", "deepseek-flash"),
                                ("openai", "deepseek-chat"), ("custom_openai", "my-deployment"),
                                ("qwen", "qwen-max")):
            self.assertEqual(providers.official_model_name(provider, model), model)

    def test_normalized_config_returns_the_official_name(self):
        preset, model, base = providers.normalized_model_config("deepseek", "deepseek-chat")
        self.assertEqual(preset.id, "deepseek")
        self.assertEqual(model, "deepseek-flash")
        self.assertEqual(base, "https://api.deepseek.com")

    def test_the_wire_payload_carries_the_official_name(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": "可用"}}], "usage": {}}
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            providers.generate(provider="deepseek", model="deepseek-chat", api_key="k", prompt="p")
        self.assertEqual(request.call_args.kwargs["payload"]["model"], "deepseek-flash")
        # The alias is not a second request: one call, one model name.
        self.assertEqual(request.call_args.kwargs["payload"].get("thinking"), {"type": "disabled"})

    def test_a_stored_alias_still_prices_as_flash(self):
        """Old rows keep the alias, so both names must stay in the price table."""
        for name in ("deepseek-chat", "deepseek-flash"):
            price = pricing.lookup("deepseek", name)
            self.assertIsNotNone(price, name)
            self.assertEqual(price["output"], pricing.lookup("deepseek", "deepseek-flash")["output"], name)


if __name__ == "__main__":
    unittest.main()


class ArkNativeSearchTests(unittest.TestCase):
    """火山方舟的「联网内容插件」（第 16 项）。

    它**只**存在于方舟原生的 `/api/v3/responses` 上：OpenAI 兼容的
    `/chat/completions` 那条路上没有这个服务端工具（官方工具说明与第三方实测一致
    ——兼容层只翻译基础对话）。所以这一组测试盯三件事：

    * 走 Responses 协议、打到 `/responses`、带上 `tools:[{"type":"web_search"}]`；
    * **两种**来源形状都能解析（`annotations[].url_citation` 与 `web_search_call` 里
      挂的来源列表）——因为**这条路径没有 Ark key 可以真机验证**（实测：本机
      pilot.env 里的三把 key 对 `ark.cn-beijing.volces.com/api/v3/responses` 全部
      返回 `AuthenticationError: The API key format is incorrect`），押注单一形状
      一旦猜错，表现和「这个供应商不会搜索」一模一样；
    * 可选参数的边界：`max_keyword` 只在方舟且只在配置里显式给了合法值时才发。
    """

    DOCUMENTED_SHAPE = {
        "output": [
            {"type": "web_search_call", "id": "ws_1", "status": "completed",
             "action": {"type": "search", "query": "CityU"}},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "答案在这里。", "annotations": [
                    {"type": "url_citation", "url": "https://www.cityu.edu.hk/a", "title": "城大 A"},
                    {"type": "url_citation", "url": "https://www.cityu.edu.hk/a", "title": "城大 A（重复）"},
                ]},
            ]},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    }

    SOURCES_ON_THE_CALL_SHAPE = {
        "output": [
            {"type": "web_search_call", "id": "ws_2", "status": "completed",
             "action": {"type": "search", "query": "CityU",
                        "sources": [{"name": "城大 B", "link": "https://www.cityu.edu.hk/b"},
                                    {"name": "坏的", "link": "javascript:alert(1)"}]}},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "答案在这里。"}]},
        ],
    }

    def _generate(self, response, **kwargs):
        with mock.patch.object(providers, "_json_request", return_value=response) as request:
            result = providers.generate(
                provider="volcengine_ark_responses", model="doubao-seed-test",
                api_key="secret", prompt="hello", native_search=True, **kwargs)
        return result, request

    def test_it_goes_to_responses_with_the_web_search_tool(self):
        result, request = self._generate(self.DOCUMENTED_SHAPE)
        self.assertTrue(request.call_args.args[0].endswith("/api/v3/responses"))
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertIs(payload["store"], False)
        self.assertEqual(result.text, "答案在这里。")
        self.assertEqual(result.search_mode, "native")

    def test_url_citation_annotations_become_sources(self):
        result, _ = self._generate(self.DOCUMENTED_SHAPE)
        self.assertEqual([item["url"] for item in result.sources], ["https://www.cityu.edu.hk/a"])
        self.assertEqual(result.sources[0]["title"], "城大 A")

    def test_sources_hanging_off_the_search_call_are_also_read(self):
        """The shape we could not verify against a live key. If Ark reports its
        plugin sources here instead, the feature still works -- and a javascript:
        link is dropped either way."""
        result, _ = self._generate(self.SOURCES_ON_THE_CALL_SHAPE)
        self.assertEqual([item["url"] for item in result.sources], ["https://www.cityu.edu.hk/b"])
        self.assertEqual(result.sources[0]["title"], "城大 B")

    def test_no_citations_means_no_sources_not_a_crash(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        result, _ = self._generate(response)
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.sources, [])

    def test_a_search_tool_that_is_not_an_ark_tool_gets_no_max_keyword(self):
        """OpenAI does not know this field; sending it would break the request."""
        _, request = self._generate(
            {"output_text": "ok"}, )
        self.assertEqual(request.call_args.kwargs["payload"]["tools"], [{"type": "web_search"}])
        with mock.patch.object(providers, "_json_request", return_value={"output_text": "ok"}) as openai_request:
            providers.generate(provider="openai", model="gpt-test", api_key="k", prompt="p",
                               native_search=True, config={"search_max_keyword": 5})
        self.assertEqual(openai_request.call_args.kwargs["payload"]["tools"], [{"type": "web_search"}])

    def test_the_keyword_limit_is_passed_only_when_it_makes_sense(self):
        for value, expected in ((7, 7), ("7", 7), ("0", 0), ("51", 0), ("many", 0), ("", 0)):
            with self.subTest(value=value):
                _, request = self._generate(
                    {"output_text": "ok"}, config={"search_max_keyword": value})
                tool = request.call_args.kwargs["payload"]["tools"][0]
                if expected:
                    self.assertEqual(tool["max_keyword"], expected)
                else:
                    self.assertNotIn("max_keyword", tool)

    def test_the_chat_compatible_ark_preset_still_cannot_search(self):
        """Adding the Responses preset must not quietly claim the chat one can."""
        self.assertFalse(providers.supports_native_search("volcengine_ark_openai"))
        self.assertFalse(providers.supports_native_search("volcengine_ark"))
        with mock.patch.object(providers, "_json_request", return_value={"choices": [{"message": {"content": "ok"}}]}) as req:
            providers.generate(provider="volcengine_ark_openai", model="doubao-test", api_key="k",
                               prompt="p", native_search=True)
        self.assertNotIn("tools", req.call_args.kwargs["payload"])

    def test_the_catalog_tells_the_console_which_preset_can_search(self):
        catalog = {item["id"]: item for item in providers.public_catalog()["models"]}
        self.assertTrue(catalog["volcengine_ark_responses"]["native_search"])
        self.assertFalse(catalog["volcengine_ark_openai"]["native_search"])
