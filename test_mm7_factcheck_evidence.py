"""Tests for claim-targeted evidence retrieval.

Every topic in the production suite stopped at fact-checking with
"could not support N claim(s) after 2 repair attempts" for claims that are
plainly documented facts (ant larvae and pupae, for example). The verifier was
only ever shown one fixed summary of the topic; rewriting the script was the
only follow-up. These tests pin the added step: find the passage that discusses
the rejected claim, re-verify against it, and never invent evidence when
nothing is found.
"""

import unittest
from unittest.mock import patch

import main
import research


ARTICLE = {
    "language": "en",
    "title": "Ant",
    "url": "https://en.wikipedia.org/wiki/Ant",
    "extract": (
        "Ants are eusocial insects of the family Formicidae.\n\n"
        "The life cycle begins when the queen lays eggs. Eggs hatch into larvae, "
        "which are fed by workers until they pupate.\n\n"
        "Colonies can be very large."
    ),
}


class ClaimEvidenceRetrievalTests(unittest.TestCase):
    def test_claim_terms_drop_stopwords_and_keep_the_subject(self):
        terms = research.claim_search_terms(
            "Eggs hatch into larvae, which are fed by sterile workers until they spin a cocoon."
        )
        self.assertIn("larvae", terms)
        self.assertIn("cocoon", terms)
        for stopword in ("which", "into", "until", "they"):
            self.assertNotIn(stopword, terms)

    def test_retrieval_returns_the_passage_that_discusses_the_claim(self):
        with patch.object(research, "_search_titles", return_value=[{"title": "Ant", "snippet": "insect"}]), \
                patch.object(research, "rank_search_hits", return_value=[(0.9, 1, {"title": "Ant", "snippet": ""})]), \
                patch.object(research, "_fetch_article", return_value=dict(ARTICLE)), \
                patch.object(research, "_sleep"):
            result = research.gather_claim_evidence(["Eggs hatch into larvae which pupate."])
        self.assertIn("larvae", result["brief"])
        self.assertIn("https://en.wikipedia.org/wiki/Ant", result["brief"])
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["queries"][0], "hatch larvae pupate")

    def test_retrieval_reports_honestly_when_nothing_is_found(self):
        with patch.object(research, "_search_titles", return_value=[]), patch.object(research, "_sleep"):
            result = research.gather_claim_evidence(["Ants build towers on Mars."])
        self.assertEqual(result["brief"], "")
        self.assertTrue(result["warnings"])
        self.assertIn("no passage found", result["warnings"][0])

    def test_query_is_anchored_to_the_topic(self):
        variants = research.claim_query_variants(["hatch", "larvae", "pupate"], topic_terms=["ant", "colony"])
        self.assertEqual(variants[0], "ant colony hatch larvae pupate")

    def test_off_topic_article_is_not_used_as_evidence(self):
        maggot = {
            "language": "en",
            "title": "Maggot",
            "url": "https://en.wikipedia.org/wiki/Maggot",
            "extract": "A maggot is the larva of a fly; the larva will pupate after feeding.",
        }
        with patch.object(research, "_search_titles", return_value=[{"title": "Maggot", "snippet": "larva"}]), \
                patch.object(research, "rank_search_hits", return_value=[(0.9, 1, {"title": "Maggot", "snippet": "larva"})]), \
                patch.object(research, "_fetch_article", return_value=maggot), \
                patch.object(research, "_sleep"):
            result = research.gather_claim_evidence(
                ["Larvae pupate after feeding."], topic_terms=["ant", "colony"]
            )
        self.assertEqual(result["brief"], "")
        self.assertIn("ignored off-topic source", " ".join(result["warnings"]))

    def test_passages_without_term_overlap_are_not_used_as_evidence(self):
        article = dict(ARTICLE, extract="The weather was mild.\n\nThe river ran north.")
        with patch.object(research, "_search_titles", return_value=[{"title": "Ant", "snippet": ""}]), \
                patch.object(research, "rank_search_hits", return_value=[(0.9, 1, {"title": "Ant", "snippet": ""})]), \
                patch.object(research, "_fetch_article", return_value=article), \
                patch.object(research, "_sleep"):
            result = research.gather_claim_evidence(["Eggs hatch into larvae which pupate."])
        self.assertEqual(result["brief"], "")


def _analysis(claims):
    return {
        "claims": list(claims),
        "non_factual": [],
        "source": "semantic:mock",
        "provider": "mock",
        "coverage": {"script_sentences": len(claims), "coverage": 1.0, "uncovered_sentences": []},
        "skipped_sentences": [],
        "error": None,
    }


UNSUPPORTED = {
    "claims": [
        {"claim": "Ants live in soil.", "verdict": "supported", "evidence": "quote from source"},
        {"claim": "Eggs hatch into larvae.", "verdict": "unsupported", "evidence": "not in brief"},
        {"claim": "Workers feed the brood.", "verdict": "supported", "evidence": "quote from source"},
    ],
    "supported": 2, "unsupported": 1, "contradicted": 0, "overall": "revise", "sourced_supports": 2,
}
SUPPORTED = {
    "claims": [
        {"claim": "Ants live in soil.", "verdict": "supported", "evidence": "quote from source"},
        {"claim": "Eggs hatch into larvae.", "verdict": "supported", "evidence": "Eggs hatch into larvae"},
        {"claim": "Workers feed the brood.", "verdict": "supported", "evidence": "quote from source"},
    ],
    "supported": 3, "unsupported": 0, "contradicted": 0, "overall": "pass", "sourced_supports": 3,
}


class FactCheckRetrievalWiringTests(unittest.TestCase):
    def test_rejected_claim_is_reverified_against_retrieved_evidence(self):
        research_data = {"brief": "Ants are insects.", "topic_language": "en", "articles": []}
        verification = {}
        claims = [item["claim"] for item in UNSUPPORTED["claims"]]
        with patch.object(main, "analyse_script_claims", return_value=_analysis(claims)), \
                patch.object(main, "_verify_claims", side_effect=[dict(UNSUPPORTED), dict(SUPPORTED)]), \
                patch.object(main.research_module, "gather_claim_evidence",
                             return_value={"brief": "[en] Ant (url)\nEggs hatch into larvae.",
                                           "articles": [ARTICLE], "queries": ["eggs hatch larvae"],
                                           "warnings": [], "characters": 40}):
            result = main.fact_check_plan({"script": "x", "scenes": []}, research_data, max_repairs=0)
        self.assertEqual(result["overall"], "pass")
        self.assertIn("Eggs hatch into larvae", research_data["brief"])
        self.assertEqual(len(research_data["articles"]), 1)
        self.assertIn("evidence_retrieval", result)

    def test_no_retrieved_evidence_keeps_the_run_blocked(self):
        research_data = {"brief": "Ants are insects.", "topic_language": "en", "articles": []}
        with patch.object(main, "analyse_script_claims",
                          return_value=_analysis([c["claim"] for c in UNSUPPORTED["claims"]])), \
                patch.object(main, "_verify_claims", return_value=dict(UNSUPPORTED)), \
                patch.object(main.research_module, "gather_claim_evidence",
                             return_value={"brief": "", "articles": [], "queries": [], "warnings": []}), \
                patch.object(main.research_module, "write_research_files"):
            with self.assertRaisesRegex(RuntimeError, "not safe to narrate"):
                main.fact_check_plan({"script": "x", "scenes": []}, research_data, max_repairs=0)

    def test_failed_report_keeps_the_rejected_claim(self):
        captured = {}
        with patch.object(main.research_module, "write_research_files",
                          side_effect=lambda research_data, payload: captured.update(payload)):
            with self.assertRaises(RuntimeError):
                main._fail_fact_check(
                    {}, "unsupported", "boom",
                    {"source": "semantic:mock", "coverage": {}}, dict(UNSUPPORTED),
                )
        self.assertEqual(captured["unsupported"], 1)
        self.assertEqual(captured["rejected_claims"][0]["claim"], "Eggs hatch into larvae.")
        self.assertEqual(len(captured["claims"]), 3)


if __name__ == "__main__":
    unittest.main()
