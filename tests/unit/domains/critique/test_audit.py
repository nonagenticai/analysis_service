"""Unit tests for audit logging."""

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.domains.critique.services.audit import log_event


class TestLogEventCreatesAuditLog:
    """Verify log_event creates proper AuditLog objects with correct field values."""

    @pytest.mark.asyncio
    async def test_creates_audit_log_with_all_fields(self, mock_db_session: AsyncMock) -> None:
        """log_event should create an AuditLog with event_type, tenant_id, job_id, and details."""
        await log_event(
            mock_db_session,
            event_type="job_created",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_id="j1",
            details={"url": "https://example.com"},
        )
        mock_db_session.add.assert_called_once()
        audit_obj = mock_db_session.add.call_args[0][0]
        assert audit_obj.event_type == "job_created"
        assert audit_obj.tenant_id == "7eed672d-a7ff-5f16-87d7-b6a493fee523"
        assert audit_obj.job_id == "j1"
        assert audit_obj.details == {"url": "https://example.com"}

    @pytest.mark.asyncio
    async def test_audit_log_has_utc_timestamp(self, mock_db_session: AsyncMock) -> None:
        """The created_at field should be a UTC datetime."""
        await log_event(
            mock_db_session,
            event_type="job_started",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_id="j1",
        )
        audit_obj = mock_db_session.add.call_args[0][0]
        assert audit_obj.created_at.tzinfo == UTC

    @pytest.mark.asyncio
    async def test_job_id_defaults_to_none(self, mock_db_session: AsyncMock) -> None:
        """When job_id is not provided, it should be None on the audit object."""
        await log_event(
            mock_db_session,
            event_type="rate_limited",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
        )
        audit_obj = mock_db_session.add.call_args[0][0]
        assert audit_obj.job_id is None

    @pytest.mark.asyncio
    async def test_details_defaults_to_none(self, mock_db_session: AsyncMock) -> None:
        """When details is not provided, it should be None on the audit object."""
        await log_event(
            mock_db_session,
            event_type="job_completed",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_id="j1",
        )
        audit_obj = mock_db_session.add.call_args[0][0]
        assert audit_obj.details is None

    @pytest.mark.asyncio
    async def test_tenant_id_defaults_to_none(self, mock_db_session: AsyncMock) -> None:
        """When tenant_id is not provided, it should be None on the audit object."""
        await log_event(
            mock_db_session,
            event_type="job_created",
        )
        audit_obj = mock_db_session.add.call_args[0][0]
        assert audit_obj.tenant_id is None


class TestLogEventStructuredLogging:
    """Verify log_event emits structured log messages."""

    @pytest.mark.asyncio
    async def test_log_message_contains_audit_prefix(self, mock_db_session: AsyncMock) -> None:
        with patch("src.domains.critique.services.audit.logger") as mock_logger:
            await log_event(
                mock_db_session,
                event_type="job_completed",
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
                job_id="j1",
            )
            mock_logger.info.assert_called_once()
            log_msg = mock_logger.info.call_args[0][0]
            assert "AUDIT" in log_msg

    @pytest.mark.asyncio
    async def test_log_message_contains_event_type(self, mock_db_session: AsyncMock) -> None:
        with patch("src.domains.critique.services.audit.logger") as mock_logger:
            await log_event(
                mock_db_session,
                event_type="job_failed",
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
                job_id="j1",
            )
            # The event type should appear in the log args
            log_args = mock_logger.info.call_args[0]
            # Format string is: "AUDIT: %s tenant=%s job=%s %s" with event_type as first arg
            assert log_args[1] == "job_failed"

    @pytest.mark.asyncio
    async def test_log_uses_dash_for_missing_tenant(self, mock_db_session: AsyncMock) -> None:
        with patch("src.domains.critique.services.audit.logger") as mock_logger:
            await log_event(
                mock_db_session,
                event_type="job_created",
                tenant_id=None,
                job_id=None,
            )
            log_args = mock_logger.info.call_args[0]
            # tenant_id and job_id should be "-" when None
            assert log_args[2] == "-"  # tenant_id
            assert log_args[3] == "-"  # job_id


class TestLogEventAllEventTypes:
    """Verify log_event works for all documented event types."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_type",
        ["job_created", "job_started", "job_completed", "job_failed", "job_cached"],
    )
    async def test_accepted_event_types(self, mock_db_session: AsyncMock, event_type: str) -> None:
        await log_event(
            mock_db_session, event_type=event_type, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        audit_obj = mock_db_session.add.call_args[0][0]
        assert audit_obj.event_type == event_type
        mock_db_session.reset_mock()


class TestLogEventErrorHandling:
    """Verify log_event handles errors gracefully."""

    @pytest.mark.asyncio
    async def test_db_add_failure_does_not_raise(self, mock_db_session: AsyncMock) -> None:
        """log_event should not raise if db.add fails."""
        mock_db_session.add = MagicMock(side_effect=SQLAlchemyError("DB error"))
        # Should not raise
        await log_event(
            mock_db_session,
            event_type="job_created",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
        )

    @pytest.mark.asyncio
    async def test_db_add_failure_logs_error(self, mock_db_session: AsyncMock) -> None:
        """When db.add fails, the error should be logged."""
        mock_db_session.add = MagicMock(side_effect=SQLAlchemyError("DB connection lost"))
        with patch("src.domains.critique.services.audit.logger") as mock_logger:
            await log_event(
                mock_db_session,
                event_type="job_created",
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            )
            mock_logger.error.assert_called_once()
            error_msg = mock_logger.error.call_args[0][0]
            assert "Failed to log audit event" in error_msg

    @pytest.mark.asyncio
    async def test_does_not_commit_transaction(self, mock_db_session: AsyncMock) -> None:
        """log_event should NOT commit -- the caller manages the transaction."""
        await log_event(
            mock_db_session,
            event_type="job_created",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_id="j1",
        )
        mock_db_session.commit.assert_not_awaited()
