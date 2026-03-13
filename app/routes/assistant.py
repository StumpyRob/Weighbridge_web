from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Tenant, User
from ..security import has_unsafe_markup
from ..services.ai_assistant import (
    AIAssistantError,
    answer_question_with_results,
)
from ..tenancy import request_platform_mode, request_tenant_id

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


class AssistantQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class AssistantResultLink(BaseModel):
    record_type: str
    record_id: int
    label: str
    href: str


class AssistantResultItem(BaseModel):
    record_type: str
    record_id: int
    title: str
    href: str
    meta: str = ""
    links: list[AssistantResultLink] = Field(default_factory=list)


class AssistantQueryResponse(BaseModel):
    answer: str
    items: list[AssistantResultItem] = Field(default_factory=list)


@router.post("/query", response_model=AssistantQueryResponse)
def assistant_query(
    payload: AssistantQueryRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> AssistantQueryResponse:
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    if not isinstance(current_user, User) or not bool(getattr(current_user, "is_active", False)):
        raise HTTPException(status_code=401, detail="Authentication required.")
    if request_platform_mode(request):
        raise HTTPException(status_code=403, detail="AI assistant is only available in tenant workspaces.")
    if has_unsafe_markup(payload.question):
        raise HTTPException(status_code=400, detail="Question must not contain HTML.")

    tenant_id = request_tenant_id(request)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    if not bool(getattr(tenant, "ai_enabled", False)):
        raise HTTPException(status_code=403, detail="AI assistant is disabled for this tenant.")
    try:
        result = answer_question_with_results(
            db,
            tenant_id,
            payload.question,
            user_id=int(current_user.id),
            model=getattr(tenant, "ai_model", None),
        )
        db.commit()
    except AIAssistantError as exc:
        db.commit()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        db.commit()
        raise HTTPException(status_code=502, detail="AI assistant is temporarily unavailable.") from exc
    return AssistantQueryResponse(**result)
