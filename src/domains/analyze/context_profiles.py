"""CRUD endpoints for UserContextProfile management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from w2v_common.auth import LICENSE_GATE
from w2v_common.database.rls import bind_session_tenant

from src.domains.analyze.schemas import ContextProfileCreate, ContextProfileResponse
from src.domains.analyze.services.repositories.context_profile_repository import (
    UserContextProfileRepository,
)
from src.shared.tenant import require_request_tenant, tenant_uuid

logger = logging.getLogger(__name__)

profiles_router = APIRouter(
    prefix="/api/context/profiles",
    tags=["context-profiles"],
    dependencies=list(LICENSE_GATE),
)


@profiles_router.post("/", status_code=201, operation_id="create_context_profile")
async def create_profile(
    body: ContextProfileCreate,
    request: Request,
) -> ContextProfileResponse:
    """Create a new context profile.

    Args:
        body: Profile creation payload with name and context data.
        request: The incoming FastAPI request.

    Returns:
        The created context profile.

    Raises:
        HTTPException: 503 if database is not available.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Creating context profile: tenant=%s name=%s", tenant_id, body.name)
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if not session_factory:
        raise HTTPException(status_code=503, detail="Database not available")

    async with session_factory() as session:
        await bind_session_tenant(session, tenant_uuid(tenant_id))
        profile = await UserContextProfileRepository.create(
            session=session,
            tenant_id=tenant_id,
            name=body.name,
            context_data=body.context_data,
            is_default=body.is_default,
        )
        await session.commit()
        return ContextProfileResponse(
            id=profile.id,
            tenant_id=profile.tenant_id,
            profile_name=profile.profile_name,
            context_data=profile.context_data,
            is_default=profile.is_default,
            created_at=str(profile.created_at) if profile.created_at else None,
            updated_at=str(profile.updated_at) if profile.updated_at else None,
        )


@profiles_router.get("/", operation_id="list_context_profiles")
async def list_profiles(request: Request) -> list[ContextProfileResponse]:
    """List all context profiles for the current tenant.

    Args:
        request: The incoming FastAPI request.

    Returns:
        List of context profile responses.

    Raises:
        HTTPException: 503 if database is not available.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Listing context profiles: tenant=%s", tenant_id)
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if not session_factory:
        raise HTTPException(status_code=503, detail="Database not available")

    async with session_factory() as session:
        await bind_session_tenant(session, tenant_uuid(tenant_id))
        profiles = await UserContextProfileRepository.list_by_tenant(session, tenant_id)
        return [
            ContextProfileResponse(
                id=p.id,
                tenant_id=p.tenant_id,
                profile_name=p.profile_name,
                context_data=p.context_data,
                is_default=p.is_default,
                created_at=str(p.created_at) if p.created_at else None,
                updated_at=str(p.updated_at) if p.updated_at else None,
            )
            for p in profiles
        ]


@profiles_router.get("/{profile_id}", operation_id="get_context_profile")
async def get_profile(profile_id: int, request: Request) -> ContextProfileResponse:
    """Get a specific context profile by ID.

    Args:
        profile_id: The profile identifier.
        request: The incoming FastAPI request.

    Returns:
        The matching context profile.

    Raises:
        HTTPException: 404 if not found, 503 if database unavailable.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Fetching context profile: profile_id=%d tenant=%s", profile_id, tenant_id)
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if not session_factory:
        raise HTTPException(status_code=503, detail="Database not available")

    async with session_factory() as session:
        # Bind the GUC so RLS scopes the by-id fetch to the caller's tenant, AND
        # pass the tenant into the query: RLS is a runtime setting, the WHERE is
        # not, and this read must be scoped either way.
        await bind_session_tenant(session, tenant_uuid(tenant_id))
        profile = await UserContextProfileRepository.get(session, profile_id, tenant_id=tenant_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        return ContextProfileResponse(
            id=profile.id,
            tenant_id=profile.tenant_id,
            profile_name=profile.profile_name,
            context_data=profile.context_data,
            is_default=profile.is_default,
            created_at=str(profile.created_at) if profile.created_at else None,
            updated_at=str(profile.updated_at) if profile.updated_at else None,
        )


@profiles_router.delete("/{profile_id}", status_code=204, operation_id="delete_context_profile")
async def delete_profile(profile_id: int, request: Request) -> None:
    """Delete a context profile by ID.

    Args:
        profile_id: The profile identifier.
        request: The incoming FastAPI request.

    Raises:
        HTTPException: 404 if not found, 503 if database unavailable.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Deleting context profile: profile_id=%d tenant=%s", profile_id, tenant_id)
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if not session_factory:
        raise HTTPException(status_code=503, detail="Database not available")

    async with session_factory() as session:
        # Mirror the GET sibling: bind the GUC so RLS scopes this session to the
        # caller's tenant, and additionally pass tenant_id into the query so the
        # scope is explicit in the SQL even where RLS is not enforcing.
        await bind_session_tenant(session, tenant_uuid(tenant_id))
        profile = await UserContextProfileRepository.get(session, profile_id, tenant_id=tenant_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        await UserContextProfileRepository.delete(session, profile_id, tenant_id=tenant_id)
        await session.commit()
