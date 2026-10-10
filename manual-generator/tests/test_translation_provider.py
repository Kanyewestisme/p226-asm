"""Mock HTTP translation protocol, credentials/privacy guards and failure safety.

Every HTTP opener is replaced by a local fake. These tests never contact the
Internet, import a STEP, render an illustration or export a PDF.
"""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manual_languages import (
    PROVIDER_ENV, PROVIDER_RESPONSE_BYTES, PROVIDER_TIMEOUT, _NoRedirect, draft_translations, provider_info,
    read_document, save_translations,
)


KEY = "fake-key-not-a-real-credential"
CONFIG = {PROVIDER_ENV[0]: "https://translation.invalid/v1", PROVIDER_ENV[1]: "mock-text-model", PROVIDER_ENV[2]: KEY}
SOURCE = "用D螺丝(M6X16)固定3个组件。"
TRANSLATIONS = {
    "en": {"未知型号安装手册": "Assembly manual for the new model", "配置新组件": "Fit the new component",
           SOURCE: "Secure 3 components with screws D (M6X16)."},
    "de": {"未知型号安装手册": "Montageanleitung für das neue Modell", "配置新组件": "Die neue Komponente montieren",
           SOURCE: "3 Komponenten mit Schrauben D (M6X16) befestigen."},
    "fr": {"未知型号安装手册": "Manuel d'assemblage du nouveau modèle", "配置新组件": "Installer le nouveau composant",
           SOURCE: "Fixer 3 composants avec les vis D (M6X16)."},
    "es": {"未知型号安装手册": "Manual de montaje del nuevo modelo", "配置新组件": "Montar el nuevo componente",
           SOURCE: "Fijar 3 componentes con tornillos D (M6X16)."},
}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, maximum):
        return self.payload[:maximum]


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.requests = []
        self.response = response
        self.error = error
    def open(self, request, timeout):
        body = json.loads(request.data)
        source = json.loads(body["messages"][1]["content"])
        self.requests.append({"url": request.full_url, "method": request.get_method(), "headers": request.headers,
                              "body": body, "source": source, "timeout": timeout})
        if self.error:
            raise self.error
        if self.response:
            return FakeResponse(self.response(source))
        translations = [{"key": item["key"], "text": TRANSLATIONS[source["target_locale"]].get(item["text"], "Translated instruction")}
                        for item in source["items"]]
        return FakeResponse(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"translations": translations}, ensure_ascii=False)}}]}, ensure_ascii=False).encode("utf-8"))


class TranslationProviderTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(os.environ, {name: "" for name in PROVIDER_ENV})
        self.environ.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.plan = {"source": {"sha256": "private-model-digest-never-transmitted"}, "product": {"title": "未知型号安装手册"},
                     "groups": [{"id": "group", "label": "不得发送的工厂分组"}],
                     "steps": [{"id": "action", "title": "配置新组件", "instruction": SOURCE,
                                "warnings": ["不得发送的工程假设"], "reviewed": False},
                               {"id": "overview", "title": "不得发送的工程总览", "instruction": "不得发送的模型核验", "include_in_manual": False, "warnings": []}],
                     "warnings": ["不得发送的原始分析说明"]}
        self.write_plan()
        self.network_guard = patch("manual_languages.urlrequest.build_opener", side_effect=AssertionError("Tests may not perform real network requests"))
        self.network_guard.start()

    def tearDown(self):
        self.network_guard.stop()
        self.temporary.cleanup()
        self.environ.stop()

    def write_plan(self):
        (self.project / "steps.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")

    def body(self, **extra):
        doc = read_document(self.project)
        return {"source_sha256": doc["source_sha256"], "base_sha256": doc["base_sha256"], "locales": ["en"], **extra}

    def seed(self, *, status="proposed"):
        doc = read_document(self.project)
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        return save_translations(self.project, self.body() | {"translations": [{"key": row["key"], "locale": "en",
              "text": "Fit 3 components using screws D (M6X16).", "status": status, "source_sha256": row["source_sha256"]}]})

    def test_missing_configuration_uses_memory_and_never_borrows_codex_or_generic_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-to-be-used", "CODEX_API_KEY": "also-not-to-be-used"}):
            info = provider_info()
            self.assertFalse(info["configured"])
            self.assertNotIn("not-to-be-used", json.dumps(info))
            doc = draft_translations(self.project, self.body())
        self.assertEqual(doc["draft_report"]["provider"], "offline-memory")
        self.assertEqual(doc["draft_report"]["request_count"], 0)
        self.assertIn("step.action.instruction", doc["draft_report"]["missing_keys"]["en"])

    def test_provider_info_is_read_only_and_contains_no_key_url_or_model_credential(self):
        with patch.dict(os.environ, CONFIG):
            info = provider_info()
            doc = read_document(self.project)
        self.assertTrue(info["configured"])
        serialized = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn(KEY, serialized)
        self.assertNotIn(CONFIG[PROVIDER_ENV[0]], serialized)
        self.assertFalse((self.project / "languages.json").exists())
        self.assertFalse(doc["capabilities"]["external_text_sent"])

    def test_configured_default_sends_only_anonymous_consumer_text_and_proposes_translations(self):
        opener = FakeOpener()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body())
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request["url"], CONFIG[PROVIDER_ENV[0]] + "/chat/completions")
        self.assertEqual(request["timeout"], PROVIDER_TIMEOUT)
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(request["method"], "POST")
        sent = json.dumps(request["body"], ensure_ascii=False)
        self.assertNotIn("不得发送", sent)
        self.assertNotIn(self.plan["source"]["sha256"], sent)
        self.assertNotIn(str(self.project), sent)
        self.assertNotIn("source_sha256", sent)
        self.assertNotIn("step.action", sent)
        self.assertEqual({item["key"] for item in request["source"]["items"]}, {"t0", "t1", "t2"})
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        self.assertEqual(row["translations"]["en"]["text"], TRANSLATIONS["en"][SOURCE])
        self.assertEqual(row["translations"]["en"]["status"], "proposed")
        self.assertEqual(row["translations"]["en"]["provenance"], "configured-text-provider")
        self.assertTrue(doc["capabilities"]["external_text_sent"])
        self.assertFalse(doc["last_translation_run"]["geometry_sent"])
        self.assertNotIn(KEY, (self.project / "languages.json").read_text(encoding="utf-8"))

    def test_other_languages_use_the_configured_provider_in_separate_batches(self):
        opener = FakeOpener()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body(locales=["de", "fr", "es"]))
        self.assertEqual([item["source"]["target_locale"] for item in opener.requests], ["de", "fr", "es"])
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        for locale in ("de", "fr", "es"):
            self.assertEqual(row["translations"][locale]["text"], TRANSLATIONS[locale][SOURCE])

    def test_explicit_offline_mode_does_not_contact_an_already_configured_provider(self):
        self.plan["steps"][0]["instruction"] = "安装头枕"
        self.write_plan()
        with patch.dict(os.environ, CONFIG):
            doc = draft_translations(self.project, self.body(provider="offline-memory"))
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        self.assertEqual(row["translations"]["en"]["text"], "Install the headrest")
        self.assertEqual(doc["draft_report"]["request_count"], 0)

    def test_incomplete_or_malformed_configuration_is_safe_and_cannot_request_ai_explicitly(self):
        urls = ["https://[malformed", "https://user:secret@translation.invalid/v1", "http://remote.invalid/v1", "https://translation.invalid/v1?key=secret"]
        for base in urls:
            with self.subTest(base=base), patch.dict(os.environ, CONFIG | {PROVIDER_ENV[0]: base}):
                info = provider_info()
                self.assertFalse(info["configured"])
                self.assertNotIn("secret", json.dumps(info))
                with self.assertRaises(ValueError):
                    draft_translations(self.project, self.body(provider="configured"))
        self.assertFalse((self.project / "languages.json").exists())

    def test_local_provider_and_explicit_chat_endpoint_are_supported_without_redirect(self):
        opener = FakeOpener()
        config = CONFIG | {PROVIDER_ENV[0]: "http://127.0.0.1:1234/v1/chat/completions"}
        with patch.dict(os.environ, config), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            draft_translations(self.project, self.body())
        self.assertEqual(opener.requests[0]["url"], config[PROVIDER_ENV[0]])
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid"))

    def test_http_failure_is_redacted_fail_fast_and_preserves_existing_proposed_copy(self):
        self.seed()
        error = HTTPError("https://translation.invalid/" + KEY, 401, "upstream echoed " + KEY, {}, io.BytesIO(KEY.encode()))
        opener = FakeOpener(error=error)
        with patch.dict(os.environ, CONFIG), patch("manual_languages.PROVIDER_BATCH_ITEMS", 1), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body(overwrite=True))
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn(KEY, json.dumps(doc, ensure_ascii=False))
        self.assertIn("HTTP 401", doc["draft_report"]["failures"][0]["reason"])
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        self.assertEqual(row["translations"]["en"]["text"], "Fit 3 components using screws D (M6X16).")

    def test_timeout_and_invalid_json_do_not_expose_provider_details_or_overwrite_copy(self):
        self.seed()
        for opener in [FakeOpener(error=TimeoutError(KEY)), FakeOpener(response=lambda source: KEY.encode())]:
            with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
                doc = draft_translations(self.project, self.body(overwrite=True))
            self.assertTrue(doc["draft_report"]["failures"])
            self.assertNotIn(KEY, json.dumps(doc, ensure_ascii=False))
            row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
            self.assertEqual(row["translations"]["en"]["text"], "Fit 3 components using screws D (M6X16).")

    def test_numerical_or_marker_mismatch_is_not_published_but_other_valid_results_can_be(self):
        for invalid in ["Secure 4 components with screws D (M6X16).", "Secure 3 components with screws D (6X16).", "Secure 3 components with screws (M6X16)."]:
            with self.subTest(invalid=invalid):
                self.seed()
                def response(source):
                    translated = [{"key": item["key"], "text": invalid if item["text"] == SOURCE else TRANSLATIONS["en"][item["text"]]} for item in source["items"]]
                    return json.dumps({"choices": [{"message": {"content": json.dumps({"translations": translated})}}]}).encode()
                opener = FakeOpener(response=response)
                with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
                    doc = draft_translations(self.project, self.body(overwrite=True))
                self.assertIn("step.action.instruction", doc["draft_report"]["missing_keys"]["en"])
                self.assertIn("product.title", doc["draft_report"]["drafted_keys"]["en"])
                row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
                self.assertEqual(row["translations"]["en"]["text"], "Fit 3 components using screws D (M6X16).")

    def test_reviewed_text_is_never_sent_or_overwritten_even_with_overwrite_true(self):
        self.seed(status="reviewed")
        opener = FakeOpener()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body(overwrite=True))
        self.assertNotIn(SOURCE, json.dumps(opener.requests[0]["body"], ensure_ascii=False))
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        self.assertEqual(row["translations"]["en"]["status"], "reviewed")
        self.assertEqual(row["translations"]["en"]["provenance"], "human_edit")

    def test_absolute_paths_in_consumer_text_are_not_sent(self):
        private_path = "C:/Users/Private/Secret/model.stp"
        self.plan["document_copy"] = {"entries": [{"key": "document.private", "text": "文件在 " + private_path, "scope": "consumer"}]}
        self.write_plan()
        opener = FakeOpener()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body())
        self.assertNotIn(private_path, json.dumps(opener.requests[0]["body"]))
        self.assertIn("document.private", doc["draft_report"]["missing_keys"]["en"])

    def test_batches_respect_item_limit_and_complete_all_valid_consumer_entries(self):
        opener = FakeOpener()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.PROVIDER_BATCH_ITEMS", 1), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body())
        self.assertEqual(len(opener.requests), 3)
        self.assertTrue(all(len(request["source"]["items"]) == 1 for request in opener.requests))
        self.assertEqual(doc["draft_report"]["sent_entry_count"], 3)

    def test_character_budget_batches_and_oversize_source_are_enforced(self):
        self.plan["document_copy"] = {"entries": [{"key": "document.too-long", "text": "过长内容" * 20, "scope": "consumer"}]}
        self.write_plan()
        opener = FakeOpener()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.PROVIDER_BATCH_CHARACTERS", 25), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body())
        self.assertTrue(all(sum(len(item["text"]) for item in request["source"]["items"]) <= 25 for request in opener.requests))
        self.assertNotIn("过长内容", json.dumps([request["body"] for request in opener.requests], ensure_ascii=False))
        self.assertIn("document.too-long", doc["draft_report"]["missing_keys"]["en"])

    def test_oversized_upstream_response_is_rejected_without_publishing_partial_text(self):
        self.seed()
        opener = FakeOpener(response=lambda source: b"x" * (PROVIDER_RESPONSE_BYTES + 1))
        with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
            doc = draft_translations(self.project, self.body(overwrite=True))
        self.assertIn("响应过大", doc["draft_report"]["failures"][0]["reason"])
        row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
        self.assertEqual(row["translations"]["en"]["text"], "Fit 3 components using screws D (M6X16).")

    def test_unknown_or_duplicate_response_keys_reject_the_batch(self):
        self.seed()
        for items in [[{"key": "unrequested", "text": "Do something"}], [{"key": "t0", "text": "Manual"}, {"key": "t0", "text": "Manual"}]]:
            opener = FakeOpener(response=lambda source: json.dumps({"choices": [{"message": {"content": json.dumps({"translations": items})}}]}).encode())
            with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=opener):
                doc = draft_translations(self.project, self.body(overwrite=True))
            self.assertTrue(doc["draft_report"]["failures"])
            row = next(row for row in doc["entries"] if row["key"] == "step.action.instruction")
            self.assertEqual(row["translations"]["en"]["text"], "Fit 3 components using screws D (M6X16).")

    def test_a_source_change_during_http_does_not_publish_old_text_into_the_new_version(self):
        self.seed()
        before = (self.project / "languages.json").read_bytes()
        def response(source):
            self.plan["source"]["sha256"] = "new-source"
            self.write_plan()
            translated = [{"key": item["key"], "text": TRANSLATIONS["en"][item["text"]]} for item in source["items"]]
            return json.dumps({"choices": [{"message": {"content": json.dumps({"translations": translated})}}]}).encode()
        with patch.dict(os.environ, CONFIG), patch("manual_languages.urlrequest.build_opener", return_value=FakeOpener(response=response)):
            with self.assertRaisesRegex(ValueError, "保存期间"):
                draft_translations(self.project, self.body(overwrite=True))
        self.assertEqual((self.project / "languages.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
