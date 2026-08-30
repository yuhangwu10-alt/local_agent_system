import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services import narrative_service, page_pool_service


class LLMFailurePropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_narrative_llm_failure_raises(self):
        with patch.object(
            narrative_service.llm_service,
            "chat",
            new=AsyncMock(side_effect=RuntimeError("LLM unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "LLM unavailable"):
                await narrative_service._extract_one_batch(
                    pages=[],
                    theme_name="水利",
                    description="",
                    schema_fields=["叙事单元标题"],
                    llm_config=None,
                    sem=asyncio.Semaphore(1),
                )

    async def test_narrative_invalid_json_raises(self):
        with patch.object(
            narrative_service.llm_service,
            "chat",
            new=AsyncMock(return_value="not-json"),
        ):
            with self.assertRaises(ValueError):
                await narrative_service._extract_one_batch(
                    pages=[],
                    theme_name="水利",
                    description="",
                    schema_fields=["叙事单元标题"],
                    llm_config=None,
                    sem=asyncio.Semaphore(1),
                )

    async def test_narrative_legitimate_empty_result_is_allowed(self):
        with patch.object(
            narrative_service.llm_service,
            "chat",
            new=AsyncMock(return_value='{"叙事单元": []}'),
        ):
            units = await narrative_service._extract_one_batch(
                pages=[],
                theme_name="水利",
                description="",
                schema_fields=["叙事单元标题"],
                llm_config=None,
                sem=asyncio.Semaphore(1),
            )

        self.assertEqual(units, [])

    async def test_single_theme_page_pool_llm_failure_raises(self):
        with patch.object(
            page_pool_service.llm_service,
            "chat",
            new=AsyncMock(side_effect=RuntimeError("LLM unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "LLM unavailable"):
                await page_pool_service._score_one_batch(
                    pages=[],
                    theme_name="水利",
                    description="",
                    core_keywords=["水利"],
                    extended_keywords=[],
                    page_pool_objects=[],
                    extractable_units=[],
                    research_questions=[],
                    llm_config=None,
                    sem=asyncio.Semaphore(1),
                )

    async def test_multi_theme_page_pool_llm_failure_raises(self):
        theme = {
            "name": "水利",
            "description": "",
            "core_keywords": ["水利"],
            "extended_keywords": [],
            "page_pool_objects": [],
            "extractable_units": [],
            "research_questions": [],
        }
        with patch.object(
            page_pool_service.llm_service,
            "chat",
            new=AsyncMock(side_effect=RuntimeError("LLM unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "LLM unavailable"):
                await page_pool_service._score_multi_theme_batch(
                    pages=[],
                    themes=[theme],
                    llm_config=None,
                    sem=asyncio.Semaphore(1),
                )


if __name__ == "__main__":
    unittest.main()
