import unittest

from ai_provider import chat_completions_url, prepare_alibaba_payload


class AIProviderTests(unittest.TestCase):
    def test_chat_completions_url_normalizes_trailing_slash(self):
        self.assertEqual(
            chat_completions_url("https://example.test/compatible-mode/v1/"),
            "https://example.test/compatible-mode/v1/chat/completions",
        )

    def test_prepare_alibaba_payload_selects_qwen_and_removes_groq_controls(self):
        original = {
            "model": "openai/gpt-oss-120b",
            "temperature": 0,
            "top_p": 0.8,
            "reasoning_effort": "low",
            "reasoning_format": "hidden",
            "max_completion_tokens": 350,
            "messages": [{"role": "user", "content": "هلا"}],
        }

        prepared = prepare_alibaba_payload(original, model="qwen3.7-plus")

        self.assertEqual(prepared["model"], "qwen3.7-plus")
        self.assertIs(prepared["enable_thinking"], False)
        self.assertNotIn("reasoning_effort", prepared)
        self.assertNotIn("reasoning_format", prepared)
        self.assertNotIn("top_p", prepared)
        self.assertEqual(original["model"], "openai/gpt-oss-120b")

    def test_prepare_alibaba_payload_can_enable_thinking(self):
        prepared = prepare_alibaba_payload(
            {"messages": [{"role": "user", "content": "حلل"}]},
            model="qwen3.7-plus",
            enable_thinking=True,
        )
        self.assertIs(prepared["enable_thinking"], True)


if __name__ == "__main__":
    unittest.main()
