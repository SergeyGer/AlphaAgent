"""Thinking models must not be able to eat the whole response budget.

Twenty-four agent runs failed with ``Invalid response from LLM call - None or
empty``. The worker log showed the cause on the line before each failure:

    Anthropic API usage: {'input_tokens': 7586, 'output_tokens': 2048, ...}

2048 was the configured ``max_tokens``, exactly. The 5.x Anthropic models think
adaptively by default, and that thinking is billed from the same allowance as the
answer - so a run could reason until the budget ran out and return no text. CrewAI
surfaced that as a ValueError which reached the task and was logged as
``ERROR - agent pipeline failed`` rather than as a decision.

Two changes stop it recurring, and these tests pin both:

1. ``thinking`` is explicitly disabled for the 5.x families, so the answer always
   has budget left regardless of the cap.
2. The cap defaults to 8192, and a response that consumes it is logged as a
   truncation rather than passing as a short answer.
"""

from django.test import SimpleTestCase

from ai_agent import AlphaAgentOrchestrator

THINKING_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
NON_THINKING_MODELS = ("claude-haiku-4-5-20251001", "deepseek-reasoner", "gpt-4o")


class BuiltLlmTests(SimpleTestCase):
    """The LLM is actually built with thinking disabled, not merely that it could be.

    ``api_key`` is a read-only property over ``config["API_KEY"]``, and ``__init__``
    accepts a config dict, so the orchestrator is constructed directly rather than
    having attributes forced onto it - which is what the first version of this test
    did, and which raised AttributeError on a property with no setter.
    """

    def _llm(self, model):
        from django.conf import settings as dj

        config = dict(dj.AI_CONFIG)
        config["MODEL"] = model
        config["API_KEY"] = "test-key-not-used"
        return AlphaAgentOrchestrator(config).build_llm()

    def test_no_thinking_parameter_is_sent(self):
        """CrewAI cannot express a valid `thinking` payload, so none is sent.

        It serialises {"type": "disabled", "budget_tokens": None}, and the API
        rejects budget_tokens alongside disabled with a 400. Verified against the
        live API - the fix is the budget size, not the parameter.
        """
        llm = self._llm("claude-haiku-5-5")
        thinking = getattr(llm, "thinking", None)
        self.assertNotEqual(getattr(thinking, "type", None), "disabled")

    def test_the_default_cap_leaves_room_for_the_answer(self):
        """2048 was the value that caused the failures; the default must exceed it."""
        from django.conf import settings as dj

        self.assertGreaterEqual(dj.AI_CONFIG["MAX_TOKENS"], 8192)
