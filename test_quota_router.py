import unittest
from unittest.mock import patch

from quota_router import QuotaAwareRouter
from visual_retry import visual_retry_policy, redact_provider_error


class FakeResponse:
    def __init__(self, status_code=200, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def make_route(provider, name, route_id, scope, credential, **extra):
    return {
        "provider": provider,
        "name": name,
        "id": route_id,
        "scope": scope,
        "credential_id": credential,
        "run": lambda: None,
        **extra,
    }


class QuotaRouterTests(unittest.TestCase):
    def test_groq_uses_lowest_remaining_dimension(self):
        router = QuotaAwareRouter()
        response = FakeResponse(headers={
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "35",
            "x-ratelimit-limit-tokens": "10000",
            "x-ratelimit-remaining-tokens": "8000",
        })
        router.observe_response("Groq", "Groq::org", response, "groq::key1")
        self.assertAlmostEqual(router.pools["Groq::org"]["remaining_ratio"], 0.35)

    def test_openrouter_preflight_ranks_by_remaining_spend_limit(self):
        router = QuotaAwareRouter()
        low = make_route("OpenRouter", "low", "or-low", "OpenRouter::low", "cred-low", probe_key="key-low")
        high = make_route("OpenRouter", "high", "OpenRouter::high", "OpenRouter::high", "cred-high", probe_key="key-high")

        def fake_get(url, headers=None, timeout=None):
            key = headers["Authorization"].split()[-1]
            if key == "key-low":
                return FakeResponse(payload={"data": {"limit": 100, "limit_remaining": 10}})
            return FakeResponse(payload={"data": {"limit": 100, "limit_remaining": 85}})

        with patch("quota_router.requests.get", side_effect=fake_get):
            ranked = router.rank_routes([low, high], log=False)
        self.assertEqual(ranked[0]["id"], "OpenRouter::high")

    def test_account_level_429_blocks_sibling_keys(self):
        router = QuotaAwareRouter()
        response = FakeResponse(status_code=429, headers={"Retry-After": "120"})
        router.observe_response("Speechify", "Speechify::account", response, "speechify::key1")
        one = make_route("Speechify", "speechify key 1", "sp1", "Speechify::account", "speechify::key1")
        two = make_route("Speechify", "speechify key 2", "sp2", "Speechify::account", "speechify::key2")
        self.assertEqual(router.rank_routes([one, two], log=False), [])

    def test_auth_error_disables_only_the_bad_credential(self):
        router = QuotaAwareRouter()
        router.observe_response("Cloudflare", "Cloudflare::account", FakeResponse(401), "cf::bad-token")
        bad = make_route("Cloudflare", "bad token", "bad", "Cloudflare::account", "cf::bad-token")
        good = make_route("Cloudflare", "valid token", "good", "Cloudflare::account", "cf::good-token")
        ranked = router.rank_routes([bad, good], log=False)
        self.assertEqual([route["id"] for route in ranked], ["good"])

    def test_unknown_quota_is_not_fabricated_as_a_percentage(self):
        router = QuotaAwareRouter()
        route = make_route("Gemini", "Gemini project", "gemini-1", "Gemini::project", "gemini::key1")
        ranked = router.rank_routes([route], log=False)
        self.assertEqual(len(ranked), 1)
        self.assertIsNone(router.pools["Gemini::project"]["remaining_ratio"])
        self.assertEqual(router.pools["Gemini::project"]["quota_detail"], "not yet observed")


    def test_openrouter_free_tier_per_minute_blocks_all_free_model_keys_in_account(self):
        router = QuotaAwareRouter()
        response = FakeResponse(
            status_code=429,
            headers={"Retry-After": "45"},
            payload={"error": {"metadata": {"limit_source": "openrouter_free_tier_per_minute"}}},
        )
        router.observe_response(
            "OpenRouter", "OpenRouterKey::key1", response, "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::free-a",
        )
        free_one = make_route(
            "OpenRouter", "free key 1", "or-free-1", "OpenRouterKey::key1", "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::free-a", uses_free_model=True,
        )
        free_two = make_route(
            "OpenRouter", "free key 2", "or-free-2", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::free-b", uses_free_model=True,
        )
        paid = make_route(
            "OpenRouter", "paid model", "or-paid", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::paid", uses_free_model=False,
        )
        self.assertFalse(router.route_available(free_one))
        self.assertFalse(router.route_available(free_two))
        self.assertTrue(router.route_available(paid))

    def test_openrouter_free_tier_daily_cap_blocks_free_routes_for_run(self):
        router = QuotaAwareRouter()
        response = FakeResponse(
            status_code=429,
            payload={"error": {"metadata": {"limit_source": "openrouter_free_tier_daily"}}},
        )
        router.observe_response(
            "OpenRouter", "OpenRouterKey::key1", response, "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::free-a",
        )
        route = make_route(
            "OpenRouter", "free route", "or-free", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::free-b", uses_free_model=True,
        )
        paid = make_route(
            "OpenRouter", "paid route", "or-paid", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::paid", uses_free_model=False,
        )
        self.assertFalse(router.route_available(route))
        self.assertTrue(router.route_available(paid))

    def test_openrouter_upstream_shared_pool_blocks_only_same_model(self):
        router = QuotaAwareRouter()
        response = FakeResponse(
            status_code=429,
            headers={"Retry-After": "75"},
            payload={"error": {"metadata": {"limit_source": "upstream_provider_shared_pool"}}},
        )
        router.observe_response(
            "OpenRouter", "OpenRouterKey::key1", response, "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        same_model_other_key = make_route(
            "OpenRouter", "same model sibling key", "or-same", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        alternate_model = make_route(
            "OpenRouter", "alternate model", "or-alt", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-b",
        )
        self.assertFalse(router.route_available(same_model_other_key))
        self.assertTrue(router.route_available(alternate_model))

    def test_openrouter_key_spending_limit_blocks_only_that_key(self):
        router = QuotaAwareRouter()
        response = FakeResponse(
            status_code=402,
            payload={"error": {"metadata": {"limit_source": "openrouter_key_limit"}}},
        )
        router.observe_response(
            "OpenRouter", "OpenRouterKey::key1", response, "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        blocked = make_route(
            "OpenRouter", "limited key", "or-limited", "OpenRouterKey::key1", "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        sibling = make_route(
            "OpenRouter", "other key", "or-other", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        self.assertFalse(router.route_available(blocked))
        self.assertTrue(router.route_available(sibling))

    def test_openrouter_account_credits_blocks_all_keys(self):
        router = QuotaAwareRouter()
        response = FakeResponse(
            status_code=402,
            payload={"error": {"metadata": {"limit_source": "openrouter_credits"}}},
        )
        router.observe_response(
            "OpenRouter", "OpenRouterKey::key1", response, "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        sibling = make_route(
            "OpenRouter", "other key", "or-other", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-b",
        )
        self.assertFalse(router.route_available(sibling))


    def test_openrouter_inflight_budget_cools_shared_account(self):
        router = QuotaAwareRouter()
        response = FakeResponse(
            status_code=429,
            headers={"Retry-After": "12"},
            payload={"error": {"metadata": {"limit_source": "openrouter_in_flight_budget"}}},
        )
        router.observe_response(
            "OpenRouter", "OpenRouterKey::key1", response, "or::key1",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-a",
        )
        sibling = make_route(
            "OpenRouter", "sibling key", "or-sibling", "OpenRouterKey::key2", "or::key2",
            shared_scope="OpenRouterAccount::acct", model_scope="OpenRouterModel::model-b",
        )
        self.assertFalse(router.route_available(sibling))

    def test_groq_uses_header_reset_duration_instead_of_generic_sixty_seconds(self):
        router = QuotaAwareRouter()
        before = __import__("time").monotonic()
        response = FakeResponse(
            status_code=429,
            headers={
                "x-ratelimit-reset-requests": "4s",
                "x-ratelimit-reset-tokens": "7s",
            },
        )
        router.observe_response("Groq", "Groq::org", response, "groq::key1")
        cooldown = router.pools["Groq::org"]["cooldown_until"] - before
        self.assertGreaterEqual(cooldown, 6.5)
        self.assertLess(cooldown, 9.0)

    def test_groq_429_cools_shared_organization_pool(self):
        router = QuotaAwareRouter()
        router.observe_response("Groq", "Groq::org", FakeResponse(429, {"Retry-After": "90"}), "groq::key1")
        first = make_route("Groq", "Groq key 1", "g1", "Groq::org", "groq::key1")
        second = make_route("Groq", "Groq key 2", "g2", "Groq::org", "groq::key2")
        self.assertFalse(router.route_available(first))
        self.assertFalse(router.route_available(second))


class VisualRetryPolicyTests(unittest.TestCase):
    def test_groq_token_minute_limit_retries_after_provider_hint(self):
        retry, delay, reason = visual_retry_policy(
            "Groq key 1", 429,
            "Rate limit reached. Please try again in 22.097142857s.",
            attempt_number=1,
            max_attempts=3,
        )
        self.assertTrue(retry)
        self.assertGreaterEqual(delay, 30.0)
        self.assertIn("rate limit", reason)

    def test_openrouter_daily_free_cap_is_not_retried(self):
        retry, delay, reason = visual_retry_policy(
            "OpenRouter key 1", 429,
            "free-models-per-day; limit_source=openrouter_free_tier_daily",
            attempt_number=1,
            max_attempts=3,
        )
        self.assertFalse(retry)
        self.assertEqual(delay, 0.0)
        self.assertIn("daily", reason)

    def test_gemini_503_is_retried(self):
        retry, delay, reason = visual_retry_policy(
            "Gemini key 1", 503, "UNAVAILABLE: high demand",
            attempt_number=1,
            max_attempts=3,
        )
        self.assertTrue(retry)
        self.assertEqual(delay, 20.0)
        self.assertIn("503", reason)

    def test_cloudflare_403_is_not_retried(self):
        retry, delay, reason = visual_retry_policy(
            "Cloudflare Vision", 403, "Forbidden: missing Workers AI permission",
            attempt_number=1,
            max_attempts=3,
        )
        self.assertFalse(retry)
        self.assertEqual(delay, 0.0)
        self.assertIn("non-retryable", reason)

    def test_provider_retry_budget_is_bounded(self):
        retry, delay, reason = visual_retry_policy(
            "Groq", 429, "try again in 90s",
            attempt_number=3,
            max_attempts=3,
        )
        self.assertFalse(retry)
        self.assertEqual(delay, 0.0)
        self.assertIn("exhausted", reason)

    def test_error_redaction_removes_known_secret(self):
        cleaned = redact_provider_error(
            "Request failed with Bearer secret-token-value", ["secret-token-value"]
        )
        self.assertNotIn("secret-token-value", cleaned)
        self.assertIn("[REDACTED]", cleaned)


if __name__ == "__main__":
    unittest.main()
