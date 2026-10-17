from django.test import SimpleTestCase

from waldur_mastermind.chat import providers


class CompletionKwargsTest(SimpleTestCase):
    def test_the_provider_s_defaults(self):
        self.assertEqual(
            providers.completion_kwargs("vllm", {}), providers.PROVIDER_DEFAULTS["vllm"]
        )

    def test_an_unknown_provider_gets_the_fallback(self):
        self.assertEqual(
            providers.completion_kwargs("custom", None), providers.FALLBACK_DEFAULTS
        )

    def test_the_admin_s_keys_win(self):
        kwargs = providers.completion_kwargs("ollama", {"temperature": 0.1})
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["top_p"], 0.8)

    def test_extra_body_is_merged_not_replaced(self):
        kwargs = providers.completion_kwargs("vllm", {"extra_body": {"min_p": 0.1}})
        self.assertEqual(kwargs["extra_body"], {"top_k": 20, "min_p": 0.1})
        # The provider's defaults themselves are left alone.
        self.assertEqual(
            providers.PROVIDER_DEFAULTS["vllm"]["extra_body"], {"top_k": 20}
        )

    def test_protected_keys_are_ignored(self):
        kwargs = providers.completion_kwargs("ollama", {"model": "evil", "tools": []})
        self.assertNotIn("model", kwargs)
        self.assertNotIn("tools", kwargs)

    def test_excluded_keys_are_left_out_of_both(self):
        kwargs = providers.completion_kwargs(
            "ollama",
            {"tool_choice": "required"},
            exclude=frozenset({"parallel_tool_calls", "tool_choice"}),
        )
        self.assertNotIn("parallel_tool_calls", kwargs)
        self.assertNotIn("tool_choice", kwargs)
