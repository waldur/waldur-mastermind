from unittest import mock

from constance.test.unittest import override_config
from django.test import TestCase

from waldur_mastermind.chat import health_checks


@override_config(
    AI_ASSISTANT_API_URL="https://llm.example.com/v1",
    AI_ASSISTANT_API_TOKEN="token",
    AI_ASSISTANT_MODEL="model",
    AI_ASSISTANT_BACKEND_TYPE="vllm",
    AI_ASSISTANT_COMPLETION_KWARGS={"extra_body": {"min_p": 0.1}},
)
@mock.patch.object(health_checks.openai, "OpenAI")
class LLMResponseHealthCheckTest(TestCase):
    def _check(self, openai_class):
        chunk = mock.Mock()
        chunk.choices = [mock.Mock(delta=mock.Mock(content="OK"))]
        create = openai_class.return_value.chat.completions.create
        create.return_value.__enter__.return_value = [chunk]
        check = health_checks.LLMResponseHealthCheck()
        check.check_status()
        return check, create.call_args.kwargs

    def test_asks_with_production_s_completion_settings(self, openai_class):
        check, kwargs = self._check(openai_class)
        self.assertEqual(check.errors, [])
        self.assertEqual(kwargs["presence_penalty"], 1.5)
        # Merged into the provider's extra_body, as the streamer does.
        self.assertEqual(kwargs["extra_body"], {"top_k": 20, "min_p": 0.1})
