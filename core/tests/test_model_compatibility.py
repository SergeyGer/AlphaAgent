"""Model-specific request compatibility.

Claude Haiku 5.5 dropped caller-controlled sampling: it rejects `temperature`
with `400 invalid_request_error: temperature is deprecated for this model`
rather than clamping it. That makes a model swap a breaking change disguised as a
one-line config edit — every agent run would fail, and the failure would look like
a provider outage rather than a misconfiguration.

The rejection was verified against the live API rather than taken from release
notes, which is why these tests pin the *rule* rather than the wording of an
error message.
"""

from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from ai_agent import TokenUsage, _accepts_sampling_params, estimate_cost

D = Decimal


class SamplingParameterCompatibilityTests(SimpleTestCase):
    def test_a_model_that_rejects_temperature_is_recognised(self):
        self.assertFalse(_accepts_sampling_params("claude-haiku-5-5"))

    def test_a_dated_variant_resolves_against_its_family(self):
        # The API currently serves the undated id, but a future dated release
        # must inherit the same rule rather than silently starting to 400.
        self.assertFalse(_accepts_sampling_params("claude-haiku-5-5-20260101"))

    def test_a_provider_prefixed_id_resolves_too(self):
        # LiteLLM routes on "<provider>/<model>", so the prefix must not defeat
        # the check — otherwise the same model behaves differently depending on
        # which provider path built it.
        self.assertFalse(_accepts_sampling_params("anthropic/claude-haiku-5-5"))

    def test_the_previous_model_still_accepts_temperature(self):
        # Haiku 4.5 is not deprecated and still accepts sampling params, so a
        # rollback must not silently lose them.
        self.assertTrue(_accepts_sampling_params("claude-haiku-4-5-20251001"))

    def test_other_providers_are_unaffected(self):
        for model in ("deepseek-reasoner", "gpt-4o", "claude-sonnet-5-5"):
            with self.subTest(model=model):
                self.assertTrue(_accepts_sampling_params(model))

    def test_an_empty_model_does_not_crash(self):
        self.assertTrue(_accepts_sampling_params(""))
        self.assertTrue(_accepts_sampling_params(None))


class HaikuFiveFivePricingTests(SimpleTestCase):
    """Haiku 5.5 is ~90% cheaper. The table has to say so, or the dashboard lies."""

    _usage = TokenUsage(total=100_000, input_tokens=90_000, output_tokens=10_000)

    def test_it_resolves_by_exact_id(self):
        from django.conf import settings

        self.assertIn("claude-haiku-5-5", settings.AI_MODEL_PRICING)

    def test_it_is_an_order_of_magnitude_cheaper_than_the_previous_model(self):
        new = estimate_cost(self._usage, "claude-haiku-5-5")
        old = estimate_cost(self._usage, "claude-haiku-4-5-20251001")

        self.assertLess(new, old / 5)

    def test_it_is_not_priced_at_the_fallback_rate(self):
        """An unpriced model silently falls back; this one must not."""
        resolved = estimate_cost(self._usage, "claude-haiku-5-5")
        fallback = estimate_cost(self._usage, "definitely-not-a-real-model")

        self.assertNotEqual(resolved, fallback)

    def test_a_dated_variant_resolves_by_prefix(self):
        self.assertEqual(
            estimate_cost(self._usage, "claude-haiku-5-5"),
            estimate_cost(self._usage, "claude-haiku-5-5-20260101"),
        )

    @override_settings(
        AI_MODEL_PRICING={"claude-haiku-5-5": {"input": 0.10, "output": 0.50}},
        AI_DEFAULT_PRICING={"input": 99.0, "output": 99.0},
    )
    def test_cache_rates_default_to_the_input_rate_when_unlisted(self):
        """A provider with no cache pricing is charged as if there were no
        discount, rather than being credited one that was never published.

        Equal, not less: assuming a discount a provider never advertised would
        under-report cost, which is the failure this whole table exists to avoid.
        """
        with_cache = TokenUsage(
            total=1000, input_tokens=1000, output_tokens=0, cached_read_tokens=500
        )
        without = TokenUsage(total=1000, input_tokens=1000, output_tokens=0)

        self.assertEqual(
            estimate_cost(with_cache, "claude-haiku-5-5"),
            estimate_cost(without, "claude-haiku-5-5"),
        )
