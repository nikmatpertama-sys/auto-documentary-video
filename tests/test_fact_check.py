import unittest

from fact_check import (
    FactCheckError,
    extract_page_text,
    is_trusted_source_url,
    select_relevant_excerpt,
    source_organization_domain,
    validate_fact_claims,
    validate_planner_source_urls,
    verify_documentary_facts,
)
from sentence_utils import split_sentences


class FactCheckTests(unittest.TestCase):
    def _plan(self, first_claim, first_urls=None):
        sentences = [
            "The Amazon basin contains extensive tropical rainforest habitat.",
            "Seasonal changes influence rainfall across the region.",
            "Researchers study moisture movement to understand regional climate.",
        ]
        claims = [
            {
                "scene": 1,
                "sentence": first_claim,
                "source_urls": first_urls or ["https://www.nasa.gov/example"],
            },
            {
                "scene": 2,
                "sentence": sentences[1],
                "source_urls": ["https://www.noaa.gov/example"],
            },
            {
                "scene": 3,
                "sentence": sentences[2],
                "source_urls": ["https://www.usgs.gov/example"],
            },
        ]
        return {
            "script": " ".join(sentences),
            "scenes": [{"narration": sentence} for sentence in sentences],
            "fact_claims": claims,
        }

    def test_trusted_https_institution_domains_are_allowed(self):
        self.assertTrue(is_trusted_source_url("https://earthobservatory.nasa.gov/features/amazon"))
        self.assertTrue(is_trusted_source_url("https://www.noaa.gov/climate"))


    def test_planner_preflight_accepts_direct_https_trusted_sources(self):
        urls = validate_planner_source_urls(
            1,
            ["https://www.nasa.gov/example", "https://www.noaa.gov/example"],
        )
        self.assertEqual(len(urls), 2)

    def test_planner_preflight_rejects_untrusted_url_before_fact_fetch(self):
        with self.assertRaisesRegex(
            ValueError,
            r"outside the trusted HTTPS allowlist.*http://untrusted\.example",
        ):
            validate_planner_source_urls(1, ["http://untrusted.example/fact"])

    def test_planner_preflight_rejects_non_https_known_host(self):
        with self.assertRaisesRegex(ValueError, r"http://www\.nasa\.gov"):
            validate_planner_source_urls(1, ["http://www.nasa.gov/fact"])

    def test_subdomains_of_one_institution_are_not_independent_sources(self):
        self.assertEqual(
            source_organization_domain("https://earthobservatory.nasa.gov/example"),
            source_organization_domain("https://www.nasa.gov/example"),
        )

    def test_untrusted_or_unsafe_urls_are_rejected(self):
        self.assertFalse(is_trusted_source_url("http://www.nasa.gov/page"))
        self.assertFalse(is_trusted_source_url("https://random-blog.example/article"))
        self.assertFalse(is_trusted_source_url("https://nasa.gov.evil.example/article"))
        self.assertFalse(is_trusted_source_url("https://user:pass@nasa.gov/page"))

    def test_html_extraction_excludes_scripts_and_returns_title(self):
        html = b"""<html><head><title>Amazon Research</title><script>fake claim</script></head>
        <body><main><h1>Study results</h1><p>Amazon basin researchers measured rainfall variation across the region. This paragraph contains enough readable material for a factual evidence page and provides a meaningful description of the study. The research explains seasonal circulation, regional moisture movement, and how observations were collected and compared over a defined period. The page also describes the limits of the measurements and the geographic area covered by the study.</p></main></body></html>"""
        title, text = extract_page_text(html, "text/html; charset=utf-8")
        self.assertEqual(title, "Amazon Research")
        self.assertIn("measured rainfall variation", text)
        self.assertNotIn("fake claim", text)

    def test_relevant_excerpt_uses_words_from_fetched_page(self):
        page = (
            "A general introduction to climate research. "
            "Researchers measured rainfall variation across the Amazon basin during the study period. "
            "The paper describes moisture transport and regional atmospheric circulation."
        )
        excerpt = select_relevant_excerpt(page, "Researchers measured rainfall variation across the Amazon basin")
        self.assertIn("rainfall variation", excerpt)

    def test_one_lexical_anchor_is_enough_to_send_excerpt_to_semantic_auditor(self):
        page = (
            "A long institutional research overview about climate systems and ecology. "
            "Researchers describe Amazon moisture cycling and atmospheric transport across the basin. "
            "The report also explains its methods, observations, limitations, and regional context."
        )
        excerpt = select_relevant_excerpt(
            page,
            "Scientists describe how forest moisture returns to the atmosphere.",
        )
        self.assertIn("moisture", excerpt)

    def test_exact_complete_claim_sentences_are_required(self):
        plan = self._plan("The Amazon basin contains extensive tropical rainforest habitat.")
        claims = validate_fact_claims(plan)
        self.assertEqual(len(claims), 3)
        plan["fact_claims"][0]["sentence"] = "The Amazon basin contains extensive tropical rainforest"
        with self.assertRaises(FactCheckError):
            validate_fact_claims(plan)


    def test_evidence_fetch_and_independent_verdict_produce_auditable_pass_report(self):
        import fact_check
        from unittest.mock import patch

        plan = self._plan("The Amazon basin contains extensive tropical rainforest habitat.")
        source_pages = {
            "https://www.nasa.gov/example": {
                "requested_url": "https://www.nasa.gov/example",
                "url": "https://www.nasa.gov/example",
                "host": "www.nasa.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "NASA Amazon Study",
                "text": "The Amazon basin contains extensive tropical rainforest habitat and supports substantial biodiversity. " * 6,
            },
            "https://www.noaa.gov/example": {
                "requested_url": "https://www.noaa.gov/example",
                "url": "https://www.noaa.gov/example",
                "host": "www.noaa.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "NOAA Seasonal Climate",
                "text": "Seasonal changes influence rainfall across the region, and climate researchers study moisture movement. " * 6,
            },
            "https://www.usgs.gov/example": {
                "requested_url": "https://www.usgs.gov/example",
                "url": "https://www.usgs.gov/example",
                "host": "www.usgs.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "USGS Research",
                "text": "Researchers study moisture movement to understand regional climate and compare observations. " * 6,
            },
        }

        def fake_checker(_prompt, validator=None):
            payload = {
                "overall_status": "pass",
                "claim_verdicts": [
                    {"claim_id": f"C{index}", "status": "supported", "evidence_urls": [url], "reason": "The fetched page excerpt directly supports the sentence."}
                    for index, url in enumerate(source_pages, 1)
                ],
                "uncited_factual_sentences": [],
                "coverage_note": "Every factual statement was covered by the cited claims.",
            }
            return validator(payload), "deepseek", "deepseek-flash"

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: source_pages[url]):
            report = verify_documentary_facts(plan, fake_checker)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["claim_count"], 3)
        self.assertEqual(report["supported_claim_count"], 3)
        self.assertEqual(len(report["sources"]), 3)

    def test_irrelevant_first_source_does_not_prevent_trying_next_source(self):
        import fact_check
        from unittest.mock import patch

        plan = self._plan(
            "The Amazon basin contains extensive tropical rainforest habitat.",
            ["https://www.nasa.gov/irrelevant", "https://www.noaa.gov/relevant"],
        )
        source_pages = {
            "https://www.nasa.gov/irrelevant": {
                "requested_url": "https://www.nasa.gov/irrelevant",
                "url": "https://www.nasa.gov/irrelevant",
                "host": "www.nasa.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "NASA unrelated page",
                "text": "A page about spacecraft engineering and orbital observations. " * 12,
            },
            "https://www.noaa.gov/relevant": {
                "requested_url": "https://www.noaa.gov/relevant",
                "url": "https://www.noaa.gov/relevant",
                "host": "www.noaa.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "Amazon rainforest research",
                "text": (
                    "The Amazon basin contains extensive tropical rainforest habitat and supports biodiversity. "
                    "Seasonal changes influence rainfall across the region. "
                ) * 8,
            },
            "https://www.noaa.gov/example": {
                "requested_url": "https://www.noaa.gov/example",
                "url": "https://www.noaa.gov/example",
                "host": "www.noaa.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "NOAA Amazon rainforest research",
                "text": (
                    "Seasonal changes influence rainfall across the region and researchers study climate. "
                ) * 12,
            },
            "https://www.usgs.gov/example": {
                "requested_url": "https://www.usgs.gov/example",
                "url": "https://www.usgs.gov/example",
                "host": "www.usgs.gov",
                "status_code": 200,
                "content_type": "text/html",
                "title": "USGS moisture research",
                "text": "Researchers study moisture movement to understand regional climate. " * 12,
            },
        }

        def fake_checker(_prompt, validator=None):
            payload = {
                "overall_status": "pass",
                "claim_verdicts": [
                    {"claim_id": "C1", "status": "supported", "evidence_urls": ["https://www.noaa.gov/relevant"], "reason": "Relevant NOAA passage supports the claim."},
                    {"claim_id": "C2", "status": "supported", "evidence_urls": ["https://www.noaa.gov/example"], "reason": "Relevant NOAA passage supports the claim."},
                    {"claim_id": "C3", "status": "supported", "evidence_urls": ["https://www.usgs.gov/example"], "reason": "Relevant USGS passage supports the claim."},
                ],
                "uncited_factual_sentences": [],
                "coverage_note": "All claims are supported by relevant fetched excerpts.",
            }
            return validator(payload), "deepseek", "deepseek-flash"

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: source_pages[url]):
            report = verify_documentary_facts(plan, fake_checker)

        self.assertEqual(report["status"], "passed")
        failed_source = next(
            item for item in report["sources"]
            if item.get("requested_url") == "https://www.nasa.gov/irrelevant"
        )
        self.assertEqual(failed_source["status"], "not_usable")
        self.assertIn("https://www.noaa.gov/relevant", report["claims"][0]["source_urls"])

    def test_quantitative_claim_recovers_a_third_independent_source_before_blocking(self):
        import fact_check
        from unittest.mock import patch

        quantitative_sentence = "The measured area is 20 percent of the regional estimate."
        plan = self._plan(
            quantitative_sentence,
            ["https://www.noaa.gov/quantitative"],
        )
        plan["scenes"][0]["narration"] = quantitative_sentence
        plan["script"] = " ".join(scene["narration"] for scene in plan["scenes"])
        source_pages = {
            "https://www.nasa.gov/irrelevant": {
                "requested_url": "https://www.nasa.gov/irrelevant",
                "url": "https://www.nasa.gov/irrelevant",
                "host": "www.nasa.gov", "status_code": 200,
                "content_type": "text/html", "title": "Unrelated NASA page",
                "text": "A page about spacecraft and orbital engineering. " * 12,
            },
            "https://www.noaa.gov/quantitative": {
                "requested_url": "https://www.noaa.gov/quantitative",
                "url": "https://www.noaa.gov/quantitative",
                "host": "www.noaa.gov", "status_code": 200,
                "content_type": "text/html", "title": "NOAA regional estimate",
                "text": ("The measured area is 20 percent of the regional estimate. "
                         "The report describes the survey methods and geographic scope. ") * 8,
            },
            "https://www.fao.org/recovered": {
                "requested_url": "https://www.fao.org/recovered",
                "url": "https://www.fao.org/recovered",
                "host": "www.fao.org", "status_code": 200,
                "content_type": "text/html", "title": "FAO regional measurement",
                "text": ("The measured area is 20 percent of the regional estimate. "
                         "The assessment explains its methods and regional coverage. ") * 8,
            },
            "https://www.noaa.gov/example": {
                "requested_url": "https://www.noaa.gov/example",
                "url": "https://www.noaa.gov/example",
                "host": "www.noaa.gov", "status_code": 200,
                "content_type": "text/html", "title": "NOAA seasonal climate",
                "text": "Seasonal changes influence rainfall across the region. " * 12,
            },
            "https://www.usgs.gov/example": {
                "requested_url": "https://www.usgs.gov/example",
                "url": "https://www.usgs.gov/example",
                "host": "www.usgs.gov", "status_code": 200,
                "content_type": "text/html", "title": "USGS moisture research",
                "text": "Researchers study moisture movement to understand regional climate. " * 12,
            },
        }
        calls = []

        def fake_checker(prompt, validator=None):
            calls.append(prompt)
            if "Find up to" in prompt and "ADDITIONAL direct source-page URLs" in prompt:
                value = validator({"source_urls": ["https://www.fao.org/recovered"]})
                return value, "gemini", "gemini-test"
            payload = {
                "overall_status": "pass",
                "claim_verdicts": [
                    {"claim_id": "C1", "status": "supported",
                     "evidence_urls": ["https://www.noaa.gov/quantitative", "https://www.fao.org/recovered"],
                     "reason": "Both independent fetched excerpts directly support the numeric claim."},
                    {"claim_id": "C2", "status": "supported",
                     "evidence_urls": ["https://www.noaa.gov/example"],
                     "reason": "The excerpt directly supports the sentence."},
                    {"claim_id": "C3", "status": "supported",
                     "evidence_urls": ["https://www.usgs.gov/example"],
                     "reason": "The excerpt directly supports the sentence."},
                ],
                "uncited_factual_sentences": [],
                "coverage_note": "All sentences are supported by fetched evidence.",
            }
            return validator(payload), "deepseek", "deepseek-test"

        with patch.object(fact_check, "fetch_source_page", side_effect=lambda url: source_pages[url]):
            report = verify_documentary_facts(plan, fake_checker)

        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(calls), 2)
        recovered = next(
            item for item in report["sources"]
            if item.get("requested_url") == "https://www.fao.org/recovered"
        )
        self.assertEqual(recovered["source_kind"], "recovered")
        self.assertEqual(
            {fact_check.source_organization_domain(url) for url in report["claims"][0]["verified_evidence_urls"]},
            {"noaa.gov", "fao.org"},
        )

    def test_recovered_source_must_be_from_a_new_independent_organization(self):
        from fact_check import _recover_independent_source_urls

        def fake_checker(_prompt, validator=None):
            return validator({
                "source_urls": ["https://earthobservatory.nasa.gov/new-report"]
            }), "deepseek", "deepseek-test"

        with self.assertRaisesRegex(ValueError, "already tried"):
            _recover_independent_source_urls(
                {
                    "id": "C1",
                    "sentence": "The measured area is 20 percent of the regional estimate.",
                },
                ["https://www.nasa.gov/old-report", "https://www.noaa.gov/old-report"],
                fake_checker,
            )

    def test_fact_coverage_backstop_blocks_uncited_numeric_sentence(self):
        plan = self._plan("The Amazon basin contains extensive tropical rainforest habitat.")
        plan["scenes"][0]["narration"] = (
            "The Amazon basin contains extensive tropical rainforest habitat. "
            "The Amazon basin covers 6 million square kilometers."
        )
        plan["script"] = " ".join(scene["narration"] for scene in plan["scenes"])
        with self.assertRaisesRegex(FactCheckError, "uncited numeric statement"):
            validate_fact_claims(plan)

    def test_quantitative_claim_with_one_candidate_source_can_reach_recovery(self):
        plan = self._plan(
            "The measured area is 20 percent of the regional estimate.",
            ["https://www.nasa.gov/example"],
        )
        plan["scenes"][0]["narration"] = "The measured area is 20 percent of the regional estimate."
        plan["script"] = " ".join(scene["narration"] for scene in plan["scenes"])
        claims = validate_fact_claims(plan)
        self.assertEqual(claims[0]["source_urls"], ["https://www.nasa.gov/example"])

    def test_sentence_splitter_keeps_decimal_measurements_and_common_abbreviations(self):
        result = split_sentences("The figure reached 20.5 percent. Dr. Silva reviewed the U.S. data. It changed.")
        self.assertEqual(
            result,
            [
                "The figure reached 20.5 percent.",
                "Dr. Silva reviewed the U.S. data.",
                "It changed.",
            ],
        )


    def test_every_narration_sentence_requires_a_source_backed_claim(self):
        plan = self._plan("The Amazon basin contains extensive tropical rainforest habitat.")
        plan["scenes"][0]["narration"] += " This rainforest stores carbon and supports regional moisture cycling."
        plan["script"] = " ".join(scene["narration"] for scene in plan["scenes"])
        with self.assertRaisesRegex(FactCheckError, "uncited narration sentence"):
            validate_fact_claims(plan)

    def test_numeric_claim_with_two_subdomains_reaches_recovery_not_final_approval(self):
        plan = self._plan(
            "The measured area is 20 percent of the regional estimate.",
            ["https://www.nasa.gov/example", "https://earthobservatory.nasa.gov/example"],
        )
        plan["scenes"][0]["narration"] = "The measured area is 20 percent of the regional estimate."
        plan["script"] = " ".join(scene["narration"] for scene in plan["scenes"])
        claims = validate_fact_claims(plan)
        self.assertEqual(len(claims[0]["source_urls"]), 2)
        self.assertEqual(
            {fact_check_domain for fact_check_domain in map(source_organization_domain, claims[0]["source_urls"])},
            {"nasa.gov"},
        )

    def test_untrusted_redirect_is_rejected_before_following_it(self):
        import fact_check
        from unittest.mock import patch

        class FakeResponse:
            status_code = 302
            headers = {"Location": "https://untrusted.example/collect"}
            url = "https://www.nasa.gov/start"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch.object(fact_check.requests, "get", return_value=FakeResponse()) as get:
            with self.assertRaisesRegex(FactCheckError, "redirected outside"):
                fact_check.fetch_source_page("https://www.nasa.gov/start")
        get.assert_called_once()

    def test_failed_source_fetch_returns_a_diagnostic_report(self):
        import fact_check
        from unittest.mock import patch

        plan = self._plan("The Amazon basin contains extensive tropical rainforest habitat.")
        with patch.object(
            fact_check,
            "fetch_source_page",
            side_effect=FactCheckError("source unavailable"),
        ):
            with self.assertRaises(FactCheckError) as context:
                verify_documentary_facts(plan, lambda *_args, **_kwargs: self.fail("AI must not run"))
        self.assertEqual(context.exception.report["status"], "blocked")
        self.assertEqual(context.exception.report["blocked_at_claim"], "C1")



if __name__ == "__main__":
    unittest.main(verbosity=2)
