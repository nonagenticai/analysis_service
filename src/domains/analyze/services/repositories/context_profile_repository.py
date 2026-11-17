"""Repository for UserContextProfile CRUD operations."""

from __future__ import annotations

import logging
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.models import UserContextProfile

logger = logging.getLogger(__name__)


class UserContextProfileRepository:
    """CRUD operations for user context profiles."""

    @staticmethod
    async def create(
        session: AsyncSession,
        tenant_id: str,
        name: str,
        context_data: dict[str, Any],
        is_default: bool = False,
    ) -> UserContextProfile:
        """Create a new context profile.

        Args:
            session: Async database session.
            tenant_id: Tenant identifier.
            name: Profile display name.
            context_data: Context configuration dict.
            is_default: Whether this is the default profile.

        Returns:
            The created UserContextProfile instance.
        """
        logger.debug("DB creating context profile: tenant=%s name=%s", tenant_id, name)
        profile = UserContextProfile(
            tenant_id=tenant_id,
            profile_name=name,
            context_data=context_data,
            is_default=is_default,
        )
        session.add(profile)
        await session.flush()
        return profile

    @staticmethod
    async def get(
        session: AsyncSession,
        profile_id: int,
        *,
        tenant_id: str,
    ) -> UserContextProfile | None:
        """Get a profile by ID within ``tenant_id``.

        ``tenant_id`` is keyword-only and REQUIRED, same as :meth:`delete`. It
        used to default to ``None``, which made the tenant predicate optional
        and left ``GET /{profile_id}`` reading by primary key alone — a
        cross-tenant read that only RLS was closing. Isolation must not rest on
        a ConfigMap key (``TENANT_RLS_MODE``), so the filter is now in the SQL
        unconditionally and the type checker names every caller that forgets it.

        Args:
            session: Async database session.
            profile_id: The profile primary key.
            tenant_id: Owning tenant — a profile belonging to any other tenant
                reads as missing, so the caller answers 404 (never 403, which
                would confirm the id exists to a tenant that must not learn it).

        Returns:
            The UserContextProfile or None if not found in this tenant.
        """
        logger.debug("DB fetching context profile: id=%d tenant=%s", profile_id, tenant_id)
        stmt = select(UserContextProfile).where(
            UserContextProfile.id == profile_id,
            UserContextProfile.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_tenant(
        session: AsyncSession,
        tenant_id: str,
    ) -> list[UserContextProfile]:
        """List all profiles for a tenant.

        Args:
            session: Async database session.
            tenant_id: Tenant identifier.

        Returns:
            List of UserContextProfile instances, newest first.
        """
        logger.debug("DB listing context profiles: tenant=%s", tenant_id)
        result = await session.execute(
            select(UserContextProfile)
            .where(UserContextProfile.tenant_id == tenant_id)
            .order_by(UserContextProfile.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete(session: AsyncSession, profile_id: int, *, tenant_id: str) -> int:
        """Delete a profile by ID within a tenant.

        ``tenant_id`` is keyword-only and REQUIRED: an unscoped
        ``DELETE ... WHERE id = :id`` would remove another tenant's row whenever
        RLS is not enforcing (local dev, superuser role, an unbound GUC). RLS is
        the primary control; this predicate is the defence-in-depth layer.

        Args:
            session: Async database session.
            profile_id: The profile primary key.
            tenant_id: Owning tenant — rows belonging to any other tenant are
                left untouched.

        Returns:
            Number of rows deleted (0 when the profile does not exist or is
            owned by a different tenant).
        """
        logger.debug("DB deleting context profile: id=%d tenant=%s", profile_id, tenant_id)
        # session.execute() is typed as returning Result; a DML statement always
        # yields a CursorResult, which is where rowcount lives.
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(UserContextProfile).where(
                    UserContextProfile.id == profile_id,
                    UserContextProfile.tenant_id == tenant_id,
                )
            ),
        )
        await session.flush()
        return result.rowcount or 0
