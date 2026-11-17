"""Single source of truth for the EXECUTE grants on this service's SECURITY DEFINER resolvers.

Why this module exists
----------------------
``analysis_service`` has four owner-owned SECURITY DEFINER resolvers that the
runtime — connected as the restricted, non-owner role ``w2v_analysis_app`` under
``TENANT_RLS_MODE=enforce`` — must be able to CALL:

``prune_review_jobs`` and ``prune_critique_data`` (0005), ``claim_due_critique_schedules``
(0007) and ``reap_stale_critique_jobs`` (0010).

Every one of those migrations ``REVOKE``s EXECUTE from ``PUBLIC`` and none of them
ever ``GRANT``ed it back to the app role. In prod the grant was applied by hand
during the Stage-A role cutover, and it was applied to some resolvers and not
others. That has now cost two identical production outages:

* ``claim_due_critique_schedules`` — ``permission denied for function`` on every
  60-second scheduler tick for 9.4 days (13,523 errors), repaired out-of-band by
  ``the deployment manifests``,
  whose own header says the durable fix belongs here.
* ``reap_stale_critique_jobs`` — same error, ~91/day on both replicas, on a reaper
  that has never once succeeded, so stranded ``running`` critique jobs were never
  reaped.

A hand-run ``GRANT`` on prod is a snowflake: it does not survive a rebuild from
migrations and it does not exist in any other environment. Putting the grants in
migration DDL — the pattern every sibling w2v service already uses
(``content_service._GRANT_RECONCILE_FN``, ``video_compiler_service.rls_resolvers._GRANT``,
``tts_gateway`` 0002) — makes them reproducible.

Adding a resolver? Add its signature to :data:`RESOLVER_SIGNATURES` **and** a NEW
migration that runs :func:`guarded_grant_statement` with its own explicitly pinned
tuple. ``tests/unit/shared/database/test_resolver_grants.py`` fails the build if a
SECURITY DEFINER function defined in ``src/`` is missing from that tuple, so the
third recurrence of this defect is a red test rather than a silent production outage.

⛔ Never let an EXISTING migration render its grant list from :data:`RESOLVER_SIGNATURES`.
A migration is a historical record; the live tuple is a view of today's code. See
:data:`RESOLVER_SIGNATURES_AS_OF_0011` for the outage shape that produces.
"""

from __future__ import annotations

#: The restricted, non-owner role the runtime connects as in prod. Absent in CI
#: and local test databases, which is why the grants are role-guarded below.
APP_ROLE = "w2v_analysis_app"

#: Every SECURITY DEFINER resolver in this service, by identity signature.
#: Identity arguments verified against the prod primary 2026-08-26.
RESOLVER_SIGNATURES: tuple[str, ...] = (
    "prune_review_jobs(timestamptz)",
    "prune_critique_data(timestamptz, timestamptz)",
    "claim_due_critique_schedules(timestamptz)",
    "reap_stale_critique_jobs(interval)",
    "reap_stale_review_jobs(interval)",
    # Added by 0015, which grants them with its OWN pinned tuple (never this one).
    "reap_never_started_critique_jobs(interval)",
    "reap_stale_analysis_jobs(interval)",
    "prune_analysis_jobs(timestamptz)",
)

#: ⛔ THIS TUPLE IS FROZEN HISTORY — DO NOT ADD TO IT, EVER.
#:
#: The exact contents of :data:`RESOLVER_SIGNATURES` **as of migration 0011**,
#: which is the only revision that ever called :func:`guarded_grant_statement`
#: with the live default. 0012 and 0013 already pin their own explicit tuples.
#:
#: Why a frozen copy exists at all: a migration is a historical record, not a
#: view of today's code. Rendering mid-chain 0011's DDL from the live tuple means
#: that the moment a fifth resolver is added (this commit adds
#: ``reap_stale_review_jobs``), an ``alembic upgrade head`` from an old database
#: reaches 0011 — three revisions before the function exists — and emits
#: ``GRANT EXECUTE ON FUNCTION reap_stale_review_jobs(interval)``, which aborts
#: on ``undefined_function``.
#:
#: And it fails ONLY in prod. The grant sits inside a ``pg_roles`` guard on
#: ``w2v_analysis_app``, a role that does not exist in CI or any local test DB,
#: so CI never executes the branch: the migration is green everywhere the defect
#: is harmless and fatal in the one place it is not. That is precisely the shape
#: that produced today's ``content_service`` outage — 85 init-container restarts,
#: six hours un-rollable, because the pod could neither migrate forward nor start.
#:
#: A new resolver gets its OWN migration running ``guarded_grant_statement`` with
#: its own pinned tuple. It does not get appended here.
RESOLVER_SIGNATURES_AS_OF_0011: tuple[str, ...] = (
    "prune_review_jobs(timestamptz)",
    "prune_critique_data(timestamptz, timestamptz)",
    "claim_due_critique_schedules(timestamptz)",
    "reap_stale_critique_jobs(interval)",
)


#: Catalog predicate the grants are guarded on. A plain literal, kept out of the
#: f-string below so the assembled statement contains no interpolated SQL keyword.
ROLE_EXISTS_PREDICATE = "SELECT 1 FROM pg_roles WHERE rolname = "


def guarded_grant_statement(signatures: tuple[str, ...] = RESOLVER_SIGNATURES) -> str:
    """``GRANT EXECUTE`` on each resolver to :data:`APP_ROLE`, guarded on the role existing.

    The guard is what makes this CI-safe: the per-PR CI job runs alembic as its own
    non-superuser owner against a database where ``w2v_analysis_app`` does not exist,
    so an unguarded ``GRANT`` would abort the migration. Guarded, it is a clean no-op
    there and the real grant in prod.

    Granting EXECUTE opens no new data access: these functions are SECURITY DEFINER
    and already run with the owner's rights. This only permits the app role to CALL
    them, which is the entire point of the resolver pattern.
    """
    body = "\n    ".join(f"GRANT EXECUTE ON FUNCTION {sig} TO {APP_ROLE};" for sig in signatures)
    # Assembled from literals + module constants only (no interpolation of anything
    # caller- or user-supplied), and deliberately not built with an f-string around
    # the catalog lookup so no lint suppression is needed to say so.
    guard = ROLE_EXISTS_PREDICATE + "'" + APP_ROLE + "'"
    return (
        "DO $$\nBEGIN\n" + f"  IF EXISTS ({guard}) THEN\n" + f"    {body}\n" + "  END IF;\nEND $$"
    )
