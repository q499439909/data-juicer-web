from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path


class PlanFlowLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        self.old_home = os.environ.get("DJ_PLAN_FLOW_HOME")
        os.environ["DJ_PLAN_FLOW_HOME"] = str(Path(self.temp_dir.name) / "locators")
        self.module = importlib.import_module("data_juicer.tools.DJ_mcp_plan_flow")

    def tearDown(self) -> None:
        if self.old_home is None:
            os.environ.pop("DJ_PLAN_FLOW_HOME", None)
        else:
            os.environ["DJ_PLAN_FLOW_HOME"] = self.old_home
        self.temp_dir.cleanup()

    def test_plan_is_frozen_only_after_validation_and_approval(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"text": "hello world"}\n', encoding="utf-8")

        job = self.module.create_job(str(self.workspace), "Clean test data", "clean-test-data")
        draft = self.module.prepare_plan(
            job["job_id"],
            {
                "dataset_path": str(input_path),
                "process": [{"text_length_filter": {"min_len": 1}}],
                "goal": "Keep non-empty text",
            },
        )

        validation = self.module.validate_plan(job["job_id"], draft["draft_id"])
        self.assertTrue(validation["ok"], validation["errors"])

        approved = self.module.approve_plan(job["job_id"], draft["draft_id"], "approved in test")
        self.assertEqual(draft["plan_hash"], approved["plan_hash"])

        saved = self.module.get_plan(job["job_id"], approved["plan_version"])
        self.assertEqual("approved", saved["plan"]["status"])
        self.assertEqual(approved["recipe_hash"], saved["plan"]["recipe_sha256"])
        self.assertTrue(Path(approved["recipe_path"]).is_file())

    def test_scan_media_is_scoped_to_job_drafts(self) -> None:
        media_dir = self.workspace / "media"
        media_dir.mkdir()
        (media_dir / "example.jpg").write_bytes(b"not-a-real-image")

        job = self.module.create_job(str(self.workspace), "Media intake", "media-intake")
        scanned = self.module.scan_media_folder(job["job_id"], str(media_dir))

        self.assertTrue(scanned["ok"])
        self.assertEqual(1, scanned["record_count"])
        self.assertTrue(Path(scanned["dataset_path"]).is_file())
        self.assertIn(".dj", Path(scanned["dataset_path"]).parts)

    def test_run_rejects_unapproved_plan(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"text": "hello world"}\n', encoding="utf-8")
        job = self.module.create_job(str(self.workspace), "Unapproved", "unapproved")
        draft = self.module.prepare_plan(
            job["job_id"],
            {
                "dataset_path": str(input_path),
                "process": [{"text_length_filter": {"min_len": 1}}],
            },
        )
        with self.assertRaises(self.module.PlanFlowError) as caught:
            self.module.run_recipe(job["job_id"], "v001")
        self.assertEqual("NOT_FOUND", caught.exception.code)
        self.assertTrue(draft["draft_id"].startswith("draft_"))

    def test_run_ids_are_short_and_unique(self) -> None:
        first = self.module._next_run_id(self.workspace / "one")
        second = self.module._next_run_id(self.workspace / "two")
        self.assertRegex(first, r"^r_[0-9a-f]{8}$")
        self.assertRegex(second, r"^r_[0-9a-f]{8}$")
        self.assertNotEqual(first, second)

    def test_validation_rejects_provider_base_url_as_api_endpoint(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"images": ["example.jpg"]}\n', encoding="utf-8")
        job = self.module.create_job(str(self.workspace), "VLM endpoint", "vlm-endpoint")
        draft = self.module.prepare_plan(
            job["job_id"],
            {
                "dataset_path": str(input_path),
                "process": [
                    {
                        "image_tagging_vlm_mapper": {
                            "is_api_model": True,
                            "api_or_hf_model": "qwen3.7-plus",
                            "api_endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "tag_field_name": "image_tags",
                        }
                    }
                ],
            },
        )

        validation = self.module.validate_plan(job["job_id"], draft["draft_id"])
        error_codes = {error["code"] for error in validation["errors"]}
        self.assertIn("INVALID_VLM_ENDPOINT", error_codes)

    def test_validation_warns_when_tag_metadata_will_not_be_exported(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"images": ["example.jpg"]}\n', encoding="utf-8")
        job = self.module.create_job(str(self.workspace), "Tag metadata", "tag-metadata")
        draft = self.module.prepare_plan(
            job["job_id"],
            {
                "dataset_path": str(input_path),
                "process": [{"image_tagging_vlm_mapper": {"is_api_model": False}}],
            },
        )
        validation = self.module.validate_plan(job["job_id"], draft["draft_id"])
        warning_codes = {warning["code"] for warning in validation["warnings"]}
        self.assertIn("TAG_METADATA_NOT_EXPORTED", warning_codes)

    def test_validation_accepts_environment_backed_qwen_endpoint(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"images": ["example.jpg"]}\n', encoding="utf-8")
        previous = {name: os.environ.get(name) for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL")}
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["OPENAI_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        try:
            job = self.module.create_job(str(self.workspace), "VLM environment", "vlm-environment")
            draft = self.module.prepare_plan(
                job["job_id"],
                {
                    "dataset_path": str(input_path),
                    "process": [
                        {
                            "image_tagging_vlm_mapper": {
                                "is_api_model": True,
                                "api_or_hf_model": "qwen3.7-plus",
                                "tag_field_name": "image_tags",
                            }
                        }
                    ],
                },
            )
            validation = self.module.validate_plan(job["job_id"], draft["draft_id"])
            self.assertTrue(validation["ok"], validation["errors"])
            warning_codes = {warning["code"] for warning in validation["warnings"]}
            self.assertIn("VLM_DEFAULT_CHAT_ENDPOINT", warning_codes)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_recipe_environment_reference_is_resolved_before_hashing(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"images": ["example.jpg"]}\n', encoding="utf-8")
        previous = os.environ.get("DJ_VLM_MODEL")
        os.environ["DJ_VLM_MODEL"] = "configured-vision-model"
        try:
            job = self.module.create_job(str(self.workspace), "Default model", "default-model")
            draft = self.module.prepare_plan(
                job["job_id"],
                {
                    "dataset_path": str(input_path),
                    "process": [
                        {
                            "image_tagging_vlm_mapper": {
                                "is_api_model": True,
                                "api_or_hf_model": "${DJ_VLM_MODEL}",
                            }
                        }
                    ],
                },
            )
            settings = draft["compiled_recipe"]["process"][0]["image_tagging_vlm_mapper"]
            self.assertEqual("configured-vision-model", settings["api_or_hf_model"])
            self.assertTrue(settings["is_api_model"])
        finally:
            if previous is None:
                os.environ.pop("DJ_VLM_MODEL", None)
            else:
                os.environ["DJ_VLM_MODEL"] = previous

    def test_recipe_preserves_an_explicit_model(self) -> None:
        input_path = self.workspace / "input.jsonl"
        input_path.write_text('{"images": ["example.jpg"]}\n', encoding="utf-8")
        previous = os.environ.get("DJ_VLM_MODEL")
        os.environ["DJ_VLM_MODEL"] = "configured-vision-model"
        try:
            job = self.module.create_job(str(self.workspace), "Explicit model", "explicit-model")
            draft = self.module.prepare_plan(
                job["job_id"],
                {
                    "dataset_path": str(input_path),
                    "process": [
                        {
                            "image_tagging_vlm_mapper": {
                                "is_api_model": True,
                                "api_or_hf_model": "user-selected-model",
                            }
                        }
                    ],
                },
            )
            settings = draft["compiled_recipe"]["process"][0]["image_tagging_vlm_mapper"]
            self.assertEqual("user-selected-model", settings["api_or_hf_model"])
        finally:
            if previous is None:
                os.environ.pop("DJ_VLM_MODEL", None)
            else:
                os.environ["DJ_VLM_MODEL"] = previous


if __name__ == "__main__":
    unittest.main()
