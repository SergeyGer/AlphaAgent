"""LLM transport-hook tests.

The regression these guard against: an Anthropic key that is not scoped to a
workspace is rejected with HTTP 400 unless every request carries an
``anthropic-workspace-id`` header, which neither CrewAI nor LiteLLM sends.

The important test is :class:`WorkspaceHeaderEndToEndTests`, which runs a mock
Anthropic endpoint on localhost and asserts the header actually arrives on the
wire. Unit-testing the interceptor in isolation would not catch the failure mode
that matters - an interceptor that is built correctly but never invoked.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

from django.test import SimpleTestCase, override_settings

from core.tests.helpers import TEST_CACHES
from services.llm_hooks import WORKSPACE_HEADER, build_llm_interceptor


class InterceptorConstructionTests(SimpleTestCase):
    def test_no_config_produces_no_interceptor(self):
        self.assertIsNone(build_llm_interceptor({}))
        self.assertIsNone(build_llm_interceptor({"WORKSPACE_ID": "  "}))

    def test_workspace_id_produces_the_anthropic_header(self):
        interceptor = build_llm_interceptor({"WORKSPACE_ID": "ws_abc123"})
        self.assertIsNotNone(interceptor)
        self.assertEqual(interceptor.headers, {WORKSPACE_HEADER: "ws_abc123"})

    def test_extra_headers_are_parsed_from_json(self):
        interceptor = build_llm_interceptor(
            {"EXTRA_HEADERS": '{"x-gateway": "gw-1", "x-tenant": "acme"}'}
        )
        self.assertEqual(interceptor.headers, {"x-gateway": "gw-1", "x-tenant": "acme"})

    def test_extra_headers_accept_a_mapping(self):
        interceptor = build_llm_interceptor({"EXTRA_HEADERS": {"x-gateway": "gw-1"}})
        self.assertEqual(interceptor.headers, {"x-gateway": "gw-1"})

    def test_malformed_json_is_ignored_not_fatal(self):
        self.assertIsNone(build_llm_interceptor({"EXTRA_HEADERS": "{not json"}))

    def test_non_object_json_is_ignored(self):
        self.assertIsNone(build_llm_interceptor({"EXTRA_HEADERS": '["a", "b"]'}))

    def test_explicit_extras_win_over_the_workspace_shorthand(self):
        interceptor = build_llm_interceptor(
            {
                "WORKSPACE_ID": "ws_from_shorthand",
                "EXTRA_HEADERS": json.dumps({WORKSPACE_HEADER: "ws_from_extras"}),
            }
        )
        self.assertEqual(interceptor.headers[WORKSPACE_HEADER], "ws_from_extras")

    def test_interceptor_subclasses_the_crewai_base(self):
        """CrewAI validates isinstance(..., BaseInterceptor); duck typing fails."""
        from crewai.llms.hooks.base import BaseInterceptor

        interceptor = build_llm_interceptor({"WORKSPACE_ID": "ws_abc"})
        self.assertIsInstance(interceptor, BaseInterceptor)


class InterceptorBehaviourTests(SimpleTestCase):
    def test_on_outbound_adds_the_header(self):
        import httpx

        interceptor = build_llm_interceptor({"WORKSPACE_ID": "ws_abc123"})
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

        returned = interceptor.on_outbound(request)

        self.assertEqual(returned.headers[WORKSPACE_HEADER], "ws_abc123")

    def test_on_outbound_preserves_existing_headers(self):
        import httpx

        interceptor = build_llm_interceptor({"WORKSPACE_ID": "ws_abc"})
        request = httpx.Request(
            "POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "k"}
        )
        interceptor.on_outbound(request)
        self.assertEqual(request.headers["x-api-key"], "k")

    def test_on_inbound_survives_a_response_without_text(self):
        """A 400 with no readable body must not raise inside the hook."""

        class Bare:
            status_code = 400

        interceptor = build_llm_interceptor({"WORKSPACE_ID": "ws_abc"})
        response = Bare()
        self.assertIs(interceptor.on_inbound(response), response)

    def test_on_inbound_returns_the_response_unchanged(self):
        import httpx

        interceptor = build_llm_interceptor({"WORKSPACE_ID": "ws_abc"})
        response = httpx.Response(200, json={"ok": True})
        self.assertIs(interceptor.on_inbound(response), response)


class _MockAnthropicHandler(BaseHTTPRequestHandler):
    """Records the headers of every request and returns a valid Messages reply."""

    # ClassVar: a shared recorder, deliberately not a per-instance default.
    recorded: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).recorded.append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        body = json.dumps(
            {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": [{"type": "text", "text": "HOLD"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the test output
        return


@override_settings(CACHES=TEST_CACHES)
class WorkspaceHeaderEndToEndTests(SimpleTestCase):
    """The decisive test: does the header reach the wire through CrewAI?"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _MockAnthropicHandler.recorded = []
        cls.server = HTTPServer(("127.0.0.1", 0), _MockAnthropicHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def _last_request(self) -> dict:
        self.assertTrue(_MockAnthropicHandler.recorded, "no request reached the mock provider")
        return _MockAnthropicHandler.recorded[-1]

    def _llm(self, **config):
        from crewai import LLM

        kwargs = {
            "model": "claude-sonnet-4-5-20250929",
            "api_key": "sk-ant-test-key",
            "base_url": f"http://127.0.0.1:{self.port}",
            "max_tokens": 64,
        }
        interceptor = build_llm_interceptor(config)
        if interceptor is not None:
            kwargs["interceptor"] = interceptor
        return LLM(**kwargs)

    def test_baseline_without_the_interceptor_omits_the_header(self):
        """Documents the bug this feature exists to fix."""
        self._llm().call("Reply with HOLD")
        self.assertNotIn(WORKSPACE_HEADER, self._last_request()["headers"])

    def test_interceptor_puts_the_workspace_header_on_the_wire(self):
        self._llm(WORKSPACE_ID="ws_e2e_42").call("Reply with HOLD")

        headers = self._last_request()["headers"]
        self.assertEqual(headers.get(WORKSPACE_HEADER), "ws_e2e_42")
        # Authentication is untouched by the hook.
        self.assertIn("x-api-key", headers)

    def test_arbitrary_extra_headers_also_reach_the_wire(self):
        self._llm(EXTRA_HEADERS='{"x-gateway-token": "gw-e2e"}').call("Reply with HOLD")

        headers = self._last_request()["headers"]
        self.assertEqual(headers.get("x-gateway-token"), "gw-e2e")

    def test_the_request_still_targets_the_messages_endpoint(self):
        self._llm(WORKSPACE_ID="ws_e2e").call("Reply with HOLD")
        self.assertEqual(self._last_request()["path"], "/v1/messages")


@override_settings(CACHES=TEST_CACHES)
class BuildLlmAttachesInterceptorTests(SimpleTestCase):
    """``build_llm()`` must attach the hook when the config asks for it."""

    def _orchestrator(self, **overrides):
        from ai_agent import AlphaAgentOrchestrator

        config = {
            "PROVIDER": "anthropic",
            "MODEL": "claude-sonnet-4-5-20250929",
            "API_KEY": "sk-ant-test",
            "BASE_URL": "",
            "TEMPERATURE": 0.2,
            "MAX_TOKENS": 128,
            "REQUEST_TIMEOUT": 30,
            "MAX_RETRIES": 0,
            "WORKSPACE_ID": "",
            "EXTRA_HEADERS": "",
        }
        config.update(overrides)
        return AlphaAgentOrchestrator(config)

    def test_no_workspace_id_means_no_interceptor(self):
        llm = self._orchestrator().build_llm()
        self.assertIsNone(getattr(llm, "interceptor", None))

    def test_workspace_id_is_propagated_to_the_llm(self):
        from crewai.llms.hooks.base import BaseInterceptor

        llm = self._orchestrator(WORKSPACE_ID="ws_build_1").build_llm()
        self.assertIsInstance(llm.interceptor, BaseInterceptor)
        self.assertEqual(llm.interceptor.headers[WORKSPACE_HEADER], "ws_build_1")

    def test_extra_headers_are_propagated_to_the_llm(self):
        llm = self._orchestrator(EXTRA_HEADERS='{"x-proxy": "p1"}').build_llm()
        self.assertEqual(llm.interceptor.headers["x-proxy"], "p1")
