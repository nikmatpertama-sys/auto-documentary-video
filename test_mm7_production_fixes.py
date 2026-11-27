"""MM7 tests: production-failure fixes for research, fact-check and discovery.

These tests cover the root causes found in a real run:

* Wikipedia search hits were accepted in result order, so "siklus kehidupan
  semut" was researched from the articles "Ekosistem" and "Rayap".
* Claim extraction only looked for digits, so a narration with no numbers
  produced zero claims and was reported as PASS.
* Footage discovery built long natural-language queries that every provider
  ANDs, so a scene returned no candidates at all and failed with "No candidate
  passed the metadata relevance filter".

Network access is mocked; the live behaviour is verified separately.
"""

import types
import unittest
from unittest.mock import patch

import research

try:  # main imports edge_tts for the rendering path only
    import edge_tts  # noqa: F401
except Exception:  # pragma: no cover - environment without the speech package
    import sys

    sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

import footage_queries
import main


class ResearchRelevanceTests(unittest.TestCase):
    def test_topic_terms_drop_treatment_words_and_camera_words(self):
        self.assertEqual(research.topic_terms("siklus kehidupan semut"), ["semut"])
        self.assertEqual(
            research.topic_terms("Amazon rainforest canopy aerial"),
            ["amazon", "rainforest", "canopy"],
        )

    def test_topic_language_detection(self):
        self.assertEqual(research.detect_topic_language("siklus kehidupan semut"), "id")
        self.assertEqual(research.detect_topic_language("life cycle of ants"), "en")

    def test_term_only_overlap_is_not_relevant(self):
        terms = research.topic_terms("siklus kehidupan semut")
        ecosystem = {"title": "Ekosistem", "snippet": "siklus materi seperti siklus karbon"}
        termite = {"title": "Rayap", "snippet": 'sering dijuluki "semut putih"'}
        ant = {"title": "Semut", "snippet": "serangga eusosial dari famili Formicidae"}
        self.assertEqual(research.score_search_hit(ecosystem, terms), 0.0)
        self.assertEqual(research.score_search_hit(termite, terms), 0.0)
        self.assertGreater(research.score_search_hit(ant, terms), research.TITLE_RELEVANCE_FLOOR)

    def test_ranked_hits_put_the_topic_article_first(self):
        terms = research.topic_terms("siklus kehidupan semut")
        hits = [
            {"title": "Ekosistem", "snippet": "siklus"},
            {"title": "Rayap", "snippet": "semut putih"},
            {"title": "Semut tentara", "snippet": "semut"},
            {"title": "Semut", "snippet": "semut"},
        ]
        ranked = research.rank_search_hits(hits, terms)
        self.assertEqual(ranked[0][2]["title"], "Semut")
        self.assertEqual([item[2]["title"] for item in ranked[2:]], ["Ekosistem", "Rayap"])

    def test_specific_event_titles_rank_below_the_topic_article(self):
        terms = research.topic_terms("Amazon rainforest")
        hits = [
            {"title": "2019 Amazon rainforest wildfires", "snippet": "amazon rainforest"},
            {"title": "Amazon rainforest", "snippet": "amazon rainforest"},
        ]
        ranked = research.rank_search_hits(hits, terms)
        self.assertEqual(ranked[0][2]["title"], "Amazon rainforest")

    def test_research_topic_rejects_irrelevant_hits_and_bridges_languages(self):
        search_results = {
            "id": [
                {"title": "Ekosistem", "snippet": "siklus"},
                {"title": "Rayap", "snippet": "semut putih"},
                {"title": "Semut", "snippet": "serangga eusosial"},
            ],
            "en": [],
        }

        def fake_search(lang, topic, limit=12):
            return search_results[lang]

        def fake_fetch(lang, title):
            return {
                "language": lang,
                "title": title,
                "url": f"https://{lang}.wikipedia.org/wiki/{title}",
                "extract": "Ants are eusocial insects of the family Formicidae. " * 20,
                "license": "CC BY-SA 4.0",
            }

        with patch.object(research, "_search_titles", side_effect=fake_search), \
                patch.object(research, "_fetch_article", side_effect=fake_fetch), \
                patch.object(research, "_langlinks", return_value={"en": "Ant"}):
            data = research.research_topic("siklus kehidupan semut", pause_seconds=0)

        titles = {(article["language"], article["title"]) for article in data["articles"]}
        self.assertIn(("id", "Semut"), titles)
        self.assertIn(("en", "Ant"), titles)
        self.assertNotIn("Ekosistem", {title for _lang, title in titles})
        self.assertNotIn("Rayap", {title for _lang, title in titles})
        self.assertTrue(data["brief"])

    def test_research_subject_fallback_query_when_topic_wording_fails(self):
        calls = []

        def fake_search(lang, topic, limit=12):
            calls.append((lang, topic))
            if topic == "siklus kehidupan semut":
                return [{"title": "Ekosistem", "snippet": "siklus kehidupan"}]
            return [{"title": "Semut", "snippet": "serangga"}]

        with patch.object(research, "_search_titles", side_effect=fake_search), \
                patch.object(research, "_fetch_article", return_value={
                    "language": "id",
                    "title": "Semut",
                    "url": "https://id.wikipedia.org/wiki/Semut",
                    "extract": "Semut adalah serangga eusosial. " * 20,
                    "license": "CC BY-SA 4.0",
                }), \
                patch.object(research, "_langlinks", return_value={}):
            data = research.research_topic("siklus kehidupan semut", pause_seconds=0)

        self.assertEqual(data["articles"][0]["title"], "Semut")
        self.assertTrue(any("semut" == query for _lang, query in calls))


class FactCheckTests(unittest.TestCase):
    def test_claims_are_not_limited_to_sentences_with_numbers(self):
        script = (
            "Ants live in colonies that can contain millions of workers. "
            "The queen lays eggs deep inside the nest. "
            "Workers feed the larvae and defend the tunnels. "
            "Is that surprising? "
            "Wow."
        )
        claims = research.extract_claims(script)
        self.assertIn("Ants live in colonies that can contain millions of workers.", claims)
        self.assertIn("The queen lays eggs deep inside the nest.", claims)
        self.assertIn("Workers feed the larvae and defend the tunnels.", claims)
        self.assertNotIn("Is that surprising?", claims)
        self.assertGreaterEqual(len(claims), 3)

    def test_indonesian_declarative_sentences_are_claims(self):
        claims = research.extract_claims(
            "Semut hidup dalam koloni besar. Ratu semut bertelur di dalam sarang."
        )
        self.assertEqual(len(claims), 2)

    def test_supported_without_evidence_is_not_a_pass(self):
        summary = research.summarise_verification(
            {"claims": [{"claim": "Ants live in colonies", "verdict": "supported", "evidence": ""}]},
            ["Ants live in colonies"],
        )
        self.assertEqual(summary["claims"][0]["verdict"], "unsupported")
        self.assertEqual(summary["overall"], "revise")

    def test_missing_verdicts_are_reported_per_claim(self):
        summary = research.summarise_verification(
            {"claims": []}, ["Ants live in colonies", "Queens lay eggs"]
        )
        self.assertEqual(summary["checked"], 2)
        self.assertTrue(all(item["evidence"] for item in summary["claims"]))
        self.assertEqual(summary["overall"], "revise")

    def test_no_checked_claim_is_a_failure(self):
        summary = research.summarise_verification({"claims": []}, [])
        self.assertEqual(summary["overall"], "fail")

    def test_parse_semantic_claims(self):
        payload = {"claims": ["Ants live in colonies.", {"claim": "Queens lay eggs."}, "short"]}
        self.assertEqual(
            research.parse_claims(payload),
            ["Ants live in colonies.", "Queens lay eggs."],
        )

    def test_fact_check_aborts_instead_of_passing_without_claims(self):
        analysis = {
            "claims": [],
            "non_factual": [],
            "source": "semantic:mock",
            "error": "extractor returned nothing",
            "coverage": {"script_sentences": 1, "coverage": 0.0},
            "skipped_sentences": [],
        }
        with patch.object(main, "analyse_script_claims", return_value=analysis), \
                patch.object(main.research_module, "write_research_files"):
            with self.assertRaisesRegex(RuntimeError, "No claim was verified"):
                main.fact_check_plan(
                    {"script": "Something happens.", "scenes": []},
                    {"brief": ""},
                )


class QueryPlanningTests(unittest.TestCase):
    def test_layered_queries_keep_the_subject_and_add_short_layers(self):
        layers = footage_queries.layered_queries(
            "ant colony underground tunnels macro",
            "Ants dig tunnels under the soil.",
        )
        self.assertEqual(layers[0], "ant colony underground tunnels macro")
        self.assertIn("ant colony", layers)
        self.assertTrue(any(layer.startswith("ant") for layer in layers[1:]))
        self.assertLess(len(" ".join(layers).split()), 40)

    def test_provider_variants_progressively_shorten_the_query(self):
        variants = footage_queries.provider_query_variants(
            "ant colony underground tunnels macro", 3
        )
        self.assertEqual(
            variants,
            ["ant colony underground tunnels macro", "ant colony underground", "ant colony"],
        )

    def test_visual_query_expansion_starts_with_short_queries(self):
        queries = main.expand_visual_queries(
            ["ant colony underground tunnels macro"],
            "Ants dig tunnels under the soil.",
        )
        self.assertLessEqual(len(queries), 8)
        self.assertTrue(any(len(query.split()) <= 3 for query in queries[:4]))


class FootageMetadataTests(unittest.TestCase):
    def test_candidate_search_text_includes_the_filename(self):
        hit = {
            "title": "File:3D-портрет муравья.webm",
            "url": "https://upload.wikimedia.org/wikipedia/commons/b/b4/Ant_trail_on_concrete_kerb.webm",
            "tags": "",
            "metadata": "",
        }
        text = main.candidate_search_text(hit)
        self.assertIn("ant", text.lower())
        self.assertIn("trail", text.lower())

    def test_secondary_subject_words_do_not_block_relevant_footage(self):
        # "leaves" belongs to the canopy group but is not the scene's subject.
        hit = {
            "title": "File:Matabele ants carrying dead termites.webm",
            "url": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Matabele_ants.webm",
            "tags": "ants insect",
            "metadata": "Ants carrying food",
            "page_url": "https://commons.wikimedia.org/wiki/File:Matabele_ants.webm",
        }
        self.assertTrue(
            main.candidate_matches_visual_anchors(
                hit,
                "ants carrying leaves back to nest",
                primary_query="ants carrying leaves back to nest",
            )
        )

    def test_partial_metadata_candidate_is_queued_for_vision(self):
        hit = {"title": "Ant colony timelapse", "metadata": "", "tags": "", "url": ""}
        self.assertTrue(main.candidate_partial_subject_match(hit, "ant colony underground"))
        city = {"title": "City street traffic", "metadata": "", "tags": "", "url": ""}
        self.assertFalse(main.candidate_partial_subject_match(city, "ant colony underground"))


class ProviderRelaxationTests(unittest.TestCase):
    def test_commons_retries_a_shorter_query_when_the_full_one_is_empty(self):
        hit = {"source": "Wikimedia Commons", "id": "1", "title": "Ant colony"}
        calls = []

        def fake_once(query):
            calls.append(query)
            return [hit] if len(query.split()) <= 2 else []

        with patch.object(main, "_commons_search_once", side_effect=fake_once):
            results = main.commons_search("ant colony underground tunnels macro")

        self.assertEqual(results, [hit])
        self.assertGreater(len(calls), 1)
        self.assertEqual(calls[-1], "ant colony")

    def test_internet_archive_retries_a_shorter_query(self):
        hit = {"source": "Internet Archive", "id": "1", "title": "Ants"}
        calls = []

        def fake_once(query):
            calls.append(query)
            return [hit] if len(query.split()) <= 2 else []

        with patch.object(main, "_internet_archive_search_once", side_effect=fake_once):
            results = main.internet_archive_search("ant colony underground tunnels macro")

        self.assertEqual(results, [hit])
        self.assertEqual(calls[-1], "ant colony")


class WebDiscoveryTests(unittest.TestCase):
    def test_page_video_urls_are_extracted(self):
        page = (
            '<html><head><meta property="og:video" content="/media/ant.mp4"></head>'
            '<body><video src="/clips/ants.webm"></video>'
            '<a href="https://example.org/other.mov">x</a></body></html>'
        )
        urls = main._page_video_urls("https://example.org/page", page)
        self.assertIn("https://example.org/media/ant.mp4", urls)
        self.assertIn("https://example.org/clips/ants.webm", urls)
        self.assertIn("https://example.org/other.mov", urls)

    def test_page_without_licence_is_not_accepted(self):
        self.assertEqual(
            main._page_license_note("https://random.example/page", "All rights reserved"),
            "",
        )
        self.assertTrue(
            main._page_license_note("https://random.example/page", "This clip is CC0 public domain")
        )
        self.assertTrue(main._page_license_note("https://images.nasa.gov/x", "NASA media"))

    def test_web_page_search_returns_embedded_video_with_licence(self):
        html_body = (
            '<html><head><title>Ant footage</title>'
            '<meta property="og:video" content="https://cdn.example.org/ant.mp4"></head>'
            "<body>Public domain footage of ants.</body></html>"
        )

        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            text = html_body

            def raise_for_status(self):
                return None

        with patch.object(main, "_web_result_pages", return_value=[("Google", "https://example.org/ants", "Ant footage")]), \
                patch.object(main, "_footage_source_get", return_value=FakeResponse()):
            results = main.web_page_video_search("ant colony footage")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "Web Search")
        self.assertEqual(results[0]["url"], "https://cdn.example.org/ant.mp4")
        self.assertIn("public domain", results[0]["license"].lower())


class DefaultLanguageTests(unittest.TestCase):
    def test_project_narration_defaults_to_english(self):
        self.assertEqual(main.SCRIPT_LANGUAGE, "en")
        self.assertEqual(main.VOICE, "en-US-GuyNeural")


if __name__ == "__main__":
    unittest.main()
