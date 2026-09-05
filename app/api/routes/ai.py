"""AI endpoints.

All of these sit in the ``ai`` rate-limit bucket, which is much tighter than
``read``: each call costs money, takes seconds, and is trivially abusable. Every
response carries ``ai_generated`` so the client can label a deterministic
fallback as a fallback instead of passing it off as analysis.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.api.deps import ai_limit
from app.models.schemas import (
    AiChatRequest,
    AiChatResponse,
    AiHealthRequest,
    AiHealthResponse,
    AiImpactRequest,
    AiImpactResponse,
    DiseaseRiskResponse,
    Latitude,
    Longitude,
)
from app.services import ai as ai_service

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(ai_limit)])


@router.post(
    "/impact",
    response_model=AiImpactResponse,
    summary="Precautions and impact analysis for a hazard event",
)
async def impact(payload: AiImpactRequest) -> Dict[str, Any]:
    return await ai_service.impact_analysis(
        category=payload.category,
        title=payload.title,
        description=payload.description,
        country=payload.country,
        severity=payload.severity,
    )


@router.post(
    "/health-advisor",
    response_model=AiHealthResponse,
    summary="General wellbeing guidance for current conditions",
)
async def health_advisor(payload: AiHealthRequest) -> Dict[str, Any]:
    return await ai_service.health_advice(
        temperature=payload.temperature,
        humidity=payload.humidity,
        uv_index=payload.uv_index,
        aqi=payload.aqi,
        age=payload.age,
        pre_existing_conditions=payload.pre_existing_conditions,
    )


@router.post("/chat", response_model=AiChatResponse, summary="Ask the safety assistant")
async def chat(payload: AiChatRequest) -> Dict[str, Any]:
    return await ai_service.chat(
        message=payload.message,
        location_context=payload.location_context,
    )


@router.get(
    "/disease-risk",
    response_model=DiseaseRiskResponse,
    summary="Environment-linked disease risk for a location",
)
async def disease_risk(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
) -> Dict[str, Any]:
    """Read-only and parameterised entirely by the URL, so it is a GET.

    The original was a POST that took its arguments from the query string, which
    made it uncacheable and inconsistent with every other read in the API. POST
    is kept below as an alias so existing app builds keep working.
    """
    return await ai_service.disease_risk(lat, lon)


@router.post(
    "/disease-risk",
    response_model=DiseaseRiskResponse,
    summary="Disease risk (legacy POST alias)",
    deprecated=True,
    include_in_schema=False,
)
async def disease_risk_post(
    lat: Latitude = Query(...),
    lon: Longitude = Query(...),
) -> Dict[str, Any]:
    return await ai_service.disease_risk(lat, lon)
