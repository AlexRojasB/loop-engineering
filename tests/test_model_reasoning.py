"""
Deterministic tests for the reviewer-observability change: core.models.ollama
must preserve a model's `thinking` output alongside its `response`, without
changing how the JSON verdict is parsed by callers.

The Ollama HTTP call is mocked; no local model/service is required.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import models  # noqa: E402


class FakeHttpResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class OllamaThinkingPreservationTests(unittest.TestCase):
    def test_preserves_thinking_alongside_a_present_response(self):
        payload = {
            "response": '{"decision": "APPROVE", "issues": []}',
            "thinking": "traced Withdraw's balance -= amount mutation"
        }

        with mock.patch.object(
            models.urllib.request,
            "urlopen",
            return_value=FakeHttpResponse(payload)
        ):
            result = models.ollama(
                "some-model",
                "prompt text",
                timeout=5,
                json_mode=True,
                think=True
            )

        self.assertTrue(result["ok"])
        # Verdict parsing must be unaffected: response is untouched.
        self.assertEqual(result["response"], payload["response"])
        self.assertEqual(result["thinking"], payload["thinking"])

    def test_existing_fallback_to_thinking_when_response_is_empty(self):
        payload = {
            "response": "",
            "thinking": "only reasoning, no final answer emitted"
        }

        with mock.patch.object(
            models.urllib.request,
            "urlopen",
            return_value=FakeHttpResponse(payload)
        ):
            result = models.ollama(
                "some-model",
                "prompt text",
                timeout=5
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], payload["thinking"])
        self.assertEqual(result["thinking"], payload["thinking"])

    def test_thinking_is_none_when_ollama_omits_it(self):
        payload = {"response": '{"decision": "APPROVE", "issues": []}'}

        with mock.patch.object(
            models.urllib.request,
            "urlopen",
            return_value=FakeHttpResponse(payload)
        ):
            result = models.ollama(
                "some-model",
                "prompt text",
                timeout=5,
                json_mode=True
            )

        self.assertTrue(result["ok"])
        self.assertIsNone(result["thinking"])

    def test_think_flag_is_forwarded_in_request_payload(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(
                request.data.decode()
            )
            return FakeHttpResponse(
                {"response": "{}", "thinking": None}
            )

        with mock.patch.object(
            models.urllib.request,
            "urlopen",
            side_effect=fake_urlopen
        ):
            models.ollama(
                "some-model",
                "prompt text",
                timeout=5,
                think=True
            )

        self.assertTrue(captured["body"].get("think"))

    def test_call_model_forwards_thinking_from_ollama(self):
        payload = {
            "response": '{"decision": "APPROVE", "issues": []}',
            "thinking": "chain of reasoning"
        }

        with mock.patch.object(
            models.urllib.request,
            "urlopen",
            return_value=FakeHttpResponse(payload)
        ):
            result = models.call_model(
                {"model_timeout_seconds": 5},
                "some-model",
                "prompt text",
                json_mode=True,
                think=True
            )

        self.assertEqual(result["thinking"], payload["thinking"])


if __name__ == "__main__":
    unittest.main()
