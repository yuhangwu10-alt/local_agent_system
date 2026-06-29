from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException

from app.providers.ocr.qwen_vl import OCR_PROMPT
from app.services.classification_service import CLASSIFICATION_PROMPT
from app.services.keyword_completion_service import BATCH_KEYWORD_PROMPT, CONSOLIDATION_KEYWORD_PROMPT
from app.services.narrative_service import NARRATIVE_EXTRACTION_PROMPT
from app.services.ocr_config_service import list_provider_models, public_providers
from app.services.page_pool_service import LLM_SCORING_PROMPT
from app.services.topic_extraction_service import BATCH_EXTRACTION_PROMPT, CONSOLIDATION_PROMPT

router = APIRouter(prefix="/api/ocr", tags=["ocr-config"])


class ModelListRequest(BaseModel):
    provider: str
    api_key: str = Field(min_length=1)


@router.get("/providers")
async def get_ocr_providers():
    return {"providers": public_providers()}


@router.get("/default-prompts")
async def get_default_prompts():
    llm_prompt = "\n\n".join(
        [
            "【页级分类】\n" + CLASSIFICATION_PROMPT,
            "【批量专题发现】\n" + BATCH_EXTRACTION_PROMPT,
            "【专题合并整理】\n" + CONSOLIDATION_PROMPT,
            "【关键词补全】\n" + BATCH_KEYWORD_PROMPT,
            "【关键词合并】\n" + CONSOLIDATION_KEYWORD_PROMPT,
            "【页面池评分】\n" + LLM_SCORING_PROMPT,
            "【叙事单元提取】\n" + NARRATIVE_EXTRACTION_PROMPT,
        ]
    )
    return {
        "ocr": OCR_PROMPT,
        "llm": llm_prompt,
    }


@router.post("/models")
async def get_ocr_models(payload: ModelListRequest):
    try:
        models = await list_provider_models(payload.provider, payload.api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"查询模型列表失败：{e}")
    return {"models": models}
