"""Health and runtime configuration routes."""

import os

from dotenv import load_dotenv
from fastapi import APIRouter

from app.core.config import GEMINI_MODELS, settings
from app.core.llm_context import llm_meta
from app.domain.rules.custom_business_rules import custom_rules_enabled

router = APIRouter()


@router.get("/health")
async def health():
    load_dotenv()
    meta = llm_meta()
    return {
        "status": "ok",
        "version": "4.0.0",
        **meta,
        "provider": "gemini",
        "gemini_configured": bool(settings.gemini_api_key),
        "default_gemini_model": settings.gemini_model,
        "gemini_models": list(GEMINI_MODELS),
        "dataverse_configured": bool(
            os.environ.get("AZURE_APP_API_URL")
            and os.environ.get("TENANT_ID")
            and os.environ.get("CLIENT_ID")
            and os.environ.get("CLIENT_SECRET")
        ),
    }


@router.get("/business-rules", tags=["Extraction"])
async def business_rules_status():
    """Describe toggleable CRM business rules (env + per-request override)."""
    return {
        "enabled_by_default": custom_rules_enabled(),
        "env_var": "CUSTOM_BUSINESS_RULES_ENABLED",
        "request_param": "apply_custom_rules",
        "rules": [
            "Prepaid → Booking Term Freehand, Freight Payable At Origin",
            "Collect → Booking Term Nomination, Freight Payable At Destination",
            "Load type FCL vs LCL from document meaning (consolidation/CFS/manifest)",
            "LCL house operations: Total TEUs = 0",
            "Multi-house master: totals = sum of house rows",
        ],
    }


@router.get("/llm/models", tags=["Extraction"])
async def list_llm_models():
    """Gemini model ids available for per-request selection."""
    return {
        "providers": ["gemini"],
        "default_provider": "gemini",
        "default_gemini_model": settings.gemini_model,
        "gemini_models": list(GEMINI_MODELS),
    }
