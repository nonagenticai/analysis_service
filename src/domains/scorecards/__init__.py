"""Shared building blocks for the synchronous scorecard domains.

``cro``, ``seo_audit`` and ``marketing`` are the same contract shape — fetch a
page (URL XOR raw HTML), scan it, decay-score weighted buckets, split findings
into quick-win / strategic action buckets, and assemble a
``{overall_score, grade, <breakdown>, quick_wins, strategic, source,
llm_enriched, scored_at}`` response. Before this package each domain hand-copied
the fetch, the finding-bucketing, and the request model. They now share:

* :mod:`.fetch`    — capped httpx fetch + single-parse soup.
* :mod:`.findings` — action-bucket split + per-bucket summary rollup.
* :mod:`.schemas`  — the ``url``/``html``/``include_llm`` request base.

The per-bucket decay + weighted-mean maths live one layer down in
``critique.services.scanners.composite``; each domain's ``service.py`` still owns
its own scan/score orchestration (they genuinely differ — marketing is a
composite over the CRO+SEO engines, SEO has the opt-in competitor/live path).
"""
