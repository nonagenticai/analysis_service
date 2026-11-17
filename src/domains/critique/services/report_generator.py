"""Report generation for critique results.

Includes scoring, deduplication, AI summary generation, and Markdown report output.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from urllib.parse import urlparse

import httpx

from src.config import get_settings
from src.domains.critique.schemas import LLMConfigOverride
from src.llm_client import get_wm_client
from src.llm_credentials import model_override_from

logger = logging.getLogger(__name__)


def _get_severity_weights() -> dict[str, int]:
    """Get severity weights from config."""
    try:
        s = get_settings().scoring
        return {
            "critical": s.critical_weight,
            "high": s.high_weight,
            "medium": s.medium_weight,
            "low": s.low_weight,
            "info": s.info_weight,
        }
    except ValueError, TypeError, KeyError, OSError, RuntimeError:
        return {"critical": 30, "high": 15, "medium": 7, "low": 3, "info": 0}


def _get_decay_k() -> int:
    """Get decay constant from config."""
    try:
        return get_settings().scoring.decay_k
    except ValueError, TypeError, KeyError, OSError, RuntimeError:
        return 25


# Category weights for overall score
CATEGORY_WEIGHTS = {
    "accessibility": 0.25,
    "security": 0.25,
    "performance": 0.20,
    "seo": 0.15,
    "ux": 0.15,
    "cro": 0.15,
}


# Canonical score→letter-grade mapping. Ordered high→low; first threshold the
# score meets or exceeds wins. Single source of truth for grades across the
# service (report_generator, html_report_generator, exports, cro).
GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (95.0, "A+"),
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (60.0, "D"),
    (0.0, "F"),
)


def score_to_grade(score: float) -> str:
    """Map a numeric 0-100 score to a letter grade (A+ through F)."""
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


# Cross-scanner equivalence map: when two scanners flag the same underlying issue
# with different check_ids, treat them as duplicates.
EQUIVALENT_CHECKS: dict[str, str] = {
    "seo-images-missing-alt": "a11y-img-missing-alt",
    "seo-viewport-missing": "perf-missing-viewport",
    "seo-heading-hierarchy-skip": "a11y-heading-skip",
}

_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}


def _normalize_check_id(check_id: str) -> str:
    """Map equivalent check_ids to a canonical form."""
    return EQUIVALENT_CHECKS.get(check_id, check_id)


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Deduplicate findings using check_id (primary) or (category, title, url) fallback.

    When merging duplicates:
    - Keeps the higher-severity finding.
    - Annotates with ``scanner_count`` and ``confirmed_by`` for cross-scanner visibility.

    Then findings sharing an explicit topic key (:data:`TOPIC_BY_CHECK_ID`,
    :data:`TOPIC_BY_CLAIM_TYPE`) merge. Nothing merges on title wording, and
    distinct findings in one category stay distinct.
    """
    # Maps dedup key -> (best finding so far, set of source scanners)
    index: dict[str, tuple[dict, set[str]]] = {}
    order: list[str] = []  # insertion order of keys

    for f in findings:
        check_id = f.get("check_id")
        if check_id:
            dedup_key = _normalize_check_id(check_id)
        else:
            dedup_key = f"{f.get('category', '')}|{f.get('title', '')}|{f.get('url', '')}"

        source = f.get("source_tool", "unknown")

        if dedup_key in index:
            existing, sources = index[dedup_key]
            sources.add(source)
            # Keep the higher-severity finding
            existing_rank = _SEVERITY_RANK.get(existing.get("severity", "info"), 4)
            new_rank = _SEVERITY_RANK.get(f.get("severity", "info"), 4)
            if new_rank < existing_rank:
                index[dedup_key] = (f, sources)
        else:
            order.append(dedup_key)
            index[dedup_key] = (f, {source})

    unique: list[dict] = []
    for key in order:
        finding, sources = index[key]
        finding["scanner_count"] = len(sources)
        finding["confirmed_by"] = sorted(sources)
        unique.append(finding)

    return _merge_equivalent_topics(unique)


#: A topic key names ONE defect: (subject, defect, polarity). Two findings merge
#: only when both map to the SAME key -- never on shared words in a title, which
#: once merged "empty alt text" into "missing alt text" and "hero image is
#: lazy-loaded" into its opposite, "images without lazy loading".
TopicKey = tuple[str, str, str]

_NO_CONTACT_METHOD: TopicKey = ("contact", "no_contact_method", "absent")

#: Deterministic checks, by check_id, that state a topic-keyed defect.
TOPIC_BY_CHECK_ID: dict[str, TopicKey] = {
    "mkt-brand-no-contact-info": _NO_CONTACT_METHOD,
    "seo-eeat-no-contact": _NO_CONTACT_METHOD,
}

#: LLM UX findings, by their structured ``claim_type``. Only a PAGE-scoped claim
#: can equal a page-level check; "no contact details above the fold" cannot.
TOPIC_BY_CLAIM_TYPE: dict[str, TopicKey] = {
    "missing_contact_method": _NO_CONTACT_METHOD,
}


def finding_topic(finding: dict) -> TopicKey | None:
    """The explicit topic key for ``finding``, or ``None`` (never merged by topic)."""
    check_id = finding.get("check_id")
    if check_id:
        return TOPIC_BY_CHECK_ID.get(check_id)
    if finding.get("scope") == "page":
        return TOPIC_BY_CLAIM_TYPE.get(str(finding.get("claim_type")))
    return None


def _merge_equivalent_topics(findings: list[dict]) -> list[dict]:
    groups: dict[tuple[TopicKey, str], list[int]] = defaultdict(list)
    for i, f in enumerate(findings):
        topic = finding_topic(f)
        if topic is not None:
            groups[(topic, str(f.get("url", "")))].append(i)

    dropped: set[int] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        # Keep the most severe; on a tie the deterministic (check_id) finding.
        keep = min(
            members,
            key=lambda i: (
                _SEVERITY_RANK.get(findings[i].get("severity", "info"), 4),
                0 if findings[i].get("check_id") else 1,
                i,
            ),
        )
        sources = set(findings[keep].get("confirmed_by") or [])
        for i in members:
            sources.update(findings[i].get("confirmed_by") or [])
            if i != keep:
                dropped.add(i)
        findings[keep]["confirmed_by"] = sorted(sources)
        findings[keep]["scanner_count"] = len(sources)
    return [f for i, f in enumerate(findings) if i not in dropped]


def _get_score_floor() -> float:
    """Get the configurable score floor from settings."""
    try:
        return get_settings().scoring.score_floor
    except ValueError, TypeError, KeyError, OSError, RuntimeError:
        return 50.0


def compute_category_score(findings: list[dict], category: str) -> float:
    """Compute score for a category using exponential decay.

    Score = 100 * e^(-penalty/k), floored at configurable minimum.

    A finding marked ``verified: False`` (an LLM claim with no matching evidence
    or probe, see ``ux_claims``) carries ZERO penalty: it is listed, never
    scored. The self-hosted model is nondeterministic under concurrent
    batching, so two scans of one unchanged page return different unverified
    findings -- they must not return different scores.
    """
    severity_weights = _get_severity_weights()
    decay_k = _get_decay_k()
    score_floor = _get_score_floor()

    category_findings = [f for f in findings if f.get("category") == category]
    if not category_findings:
        return 100.0

    penalty = sum(
        severity_weights.get(f.get("severity", "info"), 0)
        for f in category_findings
        if f.get("verified") is not False
    )
    score = score_floor + (100 - score_floor) * math.exp(-penalty / decay_k)
    return max(round(score, 1), score_floor)


def _is_scoring_evidence(finding: dict) -> bool:
    """A finding that may move a score: every finding not explicitly ``verified: False``."""
    return finding.get("verified") is not False


def compute_scores(findings: list[dict]) -> dict[str, float]:
    """Compute scores for the categories that have a verified evidence basis.

    A category is scored only when at least one of its findings is scoring
    evidence (a deterministic check, or an LLM claim confirmed on the page). A
    category whose findings are ALL unverified is left out entirely: scoring it
    would print 100.0/A+ for "the reviewer raised N issues and none could be
    checked" -- the absence of evidence, not quality. It is reported instead as
    not scored (:func:`unscored_categories`), and because
    :func:`compute_overall_score` weights only the categories present, the
    overall score is renormalised over the scored ones.
    """
    categories = {f.get("category", "") for f in findings if _is_scoring_evidence(f)}
    scores: dict[str, float] = {}
    for cat in categories:
        if cat:
            scores[cat] = compute_category_score(findings, cat)
    return scores


def unscored_categories(findings: list[dict]) -> dict[str, int]:
    """Categories with findings but no scoring evidence -> their unverified count.

    Exactly the categories :func:`compute_scores` omits although findings exist.
    Suppressed findings are ignored, as they are by scoring.
    """
    counts: dict[str, int] = defaultdict(int)
    scored: set[str] = set()
    for f in findings:
        category = f.get("category") or ""
        if not category or f.get("is_suppressed"):
            continue
        if _is_scoring_evidence(f):
            scored.add(category)
        else:
            counts[category] += 1
    return {c: n for c, n in sorted(counts.items()) if c not in scored}


#: Printed in place of the overall number AND grade when no category is scored.
OVERALL_NOT_SCORED = "Not scored"


def overall_is_unscored(scores: dict[str, float], unscored: dict[str, int]) -> bool:
    """Findings exist, but not one category has a verified evidence basis.

    Then there is no overall score -- not a stored 100.0/A+ from an "empty" scan,
    and not 0.0/F. A clean scan (no findings at all) is not this case.
    """
    return bool(unscored) and not set(scores) - set(unscored)


def score_coverage_note(scores: dict[str, float], unscored: dict[str, int]) -> str:
    """ "Overall score based on N of M categories", or "" when every category is scored."""
    if not unscored:
        return ""
    if overall_is_unscored(scores, unscored):
        return f"{OVERALL_NOT_SCORED} — none of {len(unscored)} categories has verified evidence"
    scored = len(set(scores) - set(unscored))
    return f"Overall score based on {scored} of {scored + len(unscored)} categories"


def unscored_label(count: int) -> str:
    """The printed text that replaces a score (and its grade) for an unscored category."""
    noun = "finding" if count == 1 else "findings"
    return f"Not scored — {count} unverified {noun}"


def compute_overall_score(scores: dict[str, float]) -> float:
    """Compute weighted overall score from category scores.

    Uses category weights: a11y(25%), security(25%), performance(20%), seo(15%), ux(15%).
    Only includes categories that have scores.
    """
    if not scores:
        return 0.0

    try:
        weights = get_settings().scoring
        category_weights = {
            "accessibility": weights.a11y_weight,
            "security": weights.security_weight,
            "performance": weights.performance_weight,
            "seo": weights.seo_weight,
            "ux": weights.ux_weight,
            "cro": weights.cro_weight,
        }
    except ValueError, TypeError, KeyError, OSError, RuntimeError:
        category_weights = CATEGORY_WEIGHTS

    total_weight = 0.0
    weighted_sum = 0.0

    for cat, score in scores.items():
        weight = category_weights.get(cat, 0.1)
        weighted_sum += score * weight
        total_weight += weight

    if total_weight == 0:
        return 0.0

    return round(weighted_sum / total_weight, 1)


def overall_score_for_complete_scan(scores: dict[str, float]) -> float:
    """Weighted overall score for a scan KNOWN to have measured everything.

    ``compute_scores`` derives its category set FROM the findings, so a clean
    scan yields ``{}`` — and :func:`compute_overall_score` maps ``{}`` to
    ``0.0``, the worst possible score. That default is the correct answer only
    where "no findings" is AMBIGUOUS, i.e. where a scanner may have crashed and
    measured nothing at all.

    Callers that have *established* the scan ran to completion must use this
    function instead, so a flawless page is not delivered to the customer as
    ``0.0``/grade F. It is the single definition of that rule: the async
    critique pipeline and the synchronous artifact scorer both route through
    here, so the two scorers cannot drift apart again.
    """
    return compute_overall_score(scores) if scores else 100.0


def compute_confidence_scores(
    findings: list[dict], pages_scanned: int = 1, max_pages: int = 1
) -> dict[str, float]:
    """Compute confidence scores per category.

    Confidence is based on:
    - Pages coverage (pages_scanned / max_pages)
    - Number of checks that ran (approximated by findings count)
    - Whether AI-generated findings are involved (lower confidence)
    """
    categories = set(f.get("category", "") for f in findings)
    coverage = min(pages_scanned / max(max_pages, 1), 1.0)

    confidence: dict[str, float] = {}
    for cat in categories:
        if not cat:
            continue
        cat_findings = [f for f in findings if f.get("category") == cat]
        # Base confidence from coverage
        base = coverage * 0.7
        # Bonus for having findings (scanner actually ran)
        check_bonus = 0.3 if cat_findings else 0.1
        # Reduce confidence for AI-generated findings
        ai_penalty = 0.0
        if any(f.get("false_positive_risk") == "medium" for f in cat_findings):
            ai_penalty = 0.1
        if any(f.get("false_positive_risk") == "high" for f in cat_findings):
            ai_penalty = 0.2

        confidence[cat] = round(min(base + check_bonus - ai_penalty, 1.0), 2)

    return confidence


def compare_findings(
    findings_1: list[dict], findings_2: list[dict]
) -> tuple[list[dict], list[dict], int]:
    """Compare findings between two jobs.

    Returns (new_findings, resolved_findings, persistent_count).
    Uses check_id for matching, falls back to (category, title).
    """

    def _key(f: dict) -> tuple[str, str]:
        check_id = f.get("check_id")
        if check_id:
            return (f.get("category", ""), check_id)
        return (f.get("category", ""), f.get("title", ""))

    keys_1 = {_key(f) for f in findings_1}
    keys_2 = {_key(f) for f in findings_2}

    new_keys = keys_2 - keys_1
    resolved_keys = keys_1 - keys_2
    persistent_count = len(keys_1 & keys_2)

    new_findings = [f for f in findings_2 if _key(f) in new_keys]
    resolved_findings = [f for f in findings_1 if _key(f) in resolved_keys]

    return new_findings, resolved_findings, persistent_count


def compute_score_deltas(
    before_scores: dict[str, float], after_scores: dict[str, float]
) -> dict[str, float]:
    """Per-category ``after - before`` score deltas, rounded to 1 decimal.

    Pure. Missing categories on either side count as ``0.0`` (a category that
    only appears after → its full ``after`` value; only before → its negative).
    Used by both ``/api/critique/compare`` and ``/api/brand/score-delta``.
    """
    all_cats = set(before_scores) | set(after_scores)
    return {
        cat: round(float(after_scores.get(cat, 0.0)) - float(before_scores.get(cat, 0.0)), 1)
        for cat in all_cats
    }


_CRITIQUE_SUMMARY_WORKER = "critique-summary"

_CRITIQUE_SUMMARY_SYSTEM_PROMPT = (
    "You are a website critique editor. Summarize a critique report in 2-3 "
    "concise paragraphs focused on actionable, high-priority issues. Reply "
    "with plain text — no JSON, no markdown headers, no bullet lists."
)


async def generate_summary(
    findings: list[dict],
    url: str,
    scores: dict[str, float],
    llm_config: LLMConfigOverride | None = None,
) -> tuple[str, dict | None]:
    """Generate AI summary of findings via the shared LLM client. Falls back to basic summary.

    Returns:
        Tuple of (summary text, token_usage dict or None).
    """
    try:
        import time as _time

        wm = get_wm_client()
        model_override = model_override_from(llm_config)
        # Backend-neutral: ``WMClient`` routes to worker_management OR straight
        # to vLLM (``LLM_BACKEND=vllm``); "via WM" lied on the vllm path.
        logger.info(
            "Starting AI summary generation: url=%s llm_backend=%s",
            url,
            getattr(wm, "llm_backend", "unknown"),
        )
        t0 = _time.monotonic()
        findings_text = _format_findings_for_summary(findings)
        ranking_text = category_ranking(scores)

        prompt = f"""Summarize this website critique in 2-3 concise paragraphs.

URL: {url}
Categories, weakest to strongest:
{ranking_text}
Total findings: {len(findings)}

Key findings:
{findings_text}

Write a professional summary highlighting the most important issues and
recommendations. Focus on actionable items. Do NOT state any score, grade
number or points value for a category or the site; the scores are shown
separately. Refer to categories by rank or grade word only."""

        response = await wm.message(
            worker_name=_CRITIQUE_SUMMARY_WORKER,
            message=prompt,
            system_prompt=_CRITIQUE_SUMMARY_SYSTEM_PROMPT,
            model_override=model_override,
        )
        usage = response.usage_metrics or {}
        token_usage = (
            {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            }
            if usage
            else None
        )
        content = response.output if isinstance(response.output, str) else None
        elapsed = round(_time.monotonic() - t0, 2)
        if content:
            logger.info("AI summary generated: url=%s elapsed=%.2fs", url, elapsed)
            checked = drop_swapped_score_sentences(content, scores)
            return f"{score_sentence(scores)}\n\n{checked}".strip(), token_usage
        logger.warning("AI summary returned empty content, using basic fallback")
        return _basic_summary(findings, url, scores), None

    except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
        logger.warning("AI summary generation failed, using basic fallback: %s", e)
        return _basic_summary(findings, url, scores), None


_CATEGORY_NAMES: dict[str, str] = {
    "accessibility": "accessibility",
    "a11y": "accessibility",
    "performance": "performance",
    "security": "security",
    "seo": "seo",
    "search engine optimi[sz]ation": "seo",
    "ux": "ux",
    "user experience": "ux",
    "usability": "ux",
    "cro": "cro",
    "conversion": "cro",
    # "overall" alone is an adverb ("Overall, UX scored 52.4"); only
    # "overall score/rating/grade" names the overall score.
    r"overall(?=\s+(?:score|rating|grade))": "overall",
}
_CATEGORY_PATTERNS = [
    (re.compile(rf"\b(?:{alias})\b", re.IGNORECASE), category)
    for alias, category in _CATEGORY_NAMES.items()
]
_NUMBER_RE = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d+)?(?![\w]|\.\d)")
#: The scale in "52.4 out of 100" / "52.4/100" is not a quoted score.
_SCALE_RE = re.compile(r"(?:\bout\s+of|/)\s*100\b", re.IGNORECASE)
#: Sentence ends and line breaks, captured so the text can be rebuilt.
_SENTENCE_SPLIT_RE = re.compile(r"((?<=[.!?])[ \t]+|\n+)")
_SCORE_TOLERANCE = 0.05


def display_category(category: str) -> str:
    """Human label for a category key: acronyms upper-cased ("ux" -> "UX"), others titled."""
    key = category.lower()
    return {"seo": "SEO", "ux": "UX", "cro": "CRO"}.get(key, category.replace("_", " ").title())


def score_sentence(scores: dict[str, float]) -> str:
    """The exec summary's score line, rendered from the computed numbers."""
    if not scores:
        return ""
    parts = ", ".join(f"{display_category(c)} {scores[c]:.1f}" for c in sorted(scores))
    return f"Overall score: {compute_overall_score(scores):.1f}/100 ({parts})."


_GRADE_WORDS: dict[str, str] = {
    "A+": "excellent",
    "A": "excellent",
    "B": "good",
    "C": "fair",
    "D": "weak",
    "F": "poor",
}


def category_ranking(scores: dict[str, float]) -> str:
    """Categories weakest -> strongest with grade words, and NO numbers.

    The summary model is given this instead of the scores: it cannot misquote
    a number it never saw.
    """
    ranked = sorted(scores, key=lambda c: (scores[c], c))
    lines = []
    for rank, category in enumerate(ranked, start=1):
        grade = score_to_grade(scores[category])
        lines.append(f"{rank}. {display_category(category)}: grade {grade} ({_GRADE_WORDS[grade]})")
    return "\n".join(lines)


def _category_mentions(sentence: str) -> list[tuple[int, str]]:
    """(position, category) for every category named in the sentence, in order."""
    return sorted(
        (m.start(), category)
        for pattern, category in _CATEGORY_PATTERNS
        for m in pattern.finditer(sentence)
    )


def _is_swapped_score_sentence(sentence: str, known: dict[str, float]) -> bool:
    """A number equal to one category's score is bound to a DIFFERENT category.

    Each number that matches a computed score is bound to the category it is
    written against: in a list form ("security and performance scores are A and
    B") the i-th category to the i-th such number; otherwise the nearest category
    named before it (or, failing that, after it). A correctly attributed
    multi-category sentence ("Security (52.7) trails performance (59.7)") stays.
    """
    mentions = _category_mentions(sentence)
    if not mentions:
        return False
    masked = _SCALE_RE.sub(lambda m: " " * len(m.group(0)), sentence)
    quoted = []
    for m in _NUMBER_RE.finditer(masked):
        value = float(m.group(0))
        owners = {c for c, v in known.items() if abs(value - v) <= _SCORE_TOLERANCE}
        if owners:
            quoted.append((m.start(), owners))
    if not quoted:
        return False
    ordered: list[str] = []
    for _, category in mentions:
        if category not in ordered:
            ordered.append(category)
    if len(quoted) >= 2 and len(ordered) == len(quoted) and mentions[-1][0] < quoted[0][0]:
        bound = ordered
    else:
        before = [[c for p, c in mentions if p < pos] for pos, _ in quoted]
        bound = [
            names[-1] if names else next(c for p, c in mentions if p > pos)
            for names, (pos, _) in zip(before, quoted, strict=True)
        ]
    return any(category not in owners for category, (_, owners) in zip(bound, quoted, strict=True))


def drop_swapped_score_sentences(text: str, scores: dict[str, float]) -> str:
    """Drop a sentence that quotes one category's score next to ANOTHER category.

    The summary model once wrote "security and performance scores are 59.7 and
    52.7 respectively" with the two numbers swapped. The model no longer sees
    numeric scores (see :func:`category_ranking`) and the score line is rendered
    from the computed values, so this is a backstop, and it never edits prose:

    * a sentence containing a number within 0.05 of a computed category (or
      overall) score is dropped whole when that number is written against a
      category other than the one the score belongs to;
    * every other sentence -- including counts ("3 render-blocking scripts",
      "2 points short") and a correctly attributed score -- is left untouched.
    """
    if not scores:
        return text
    known = {**scores, "overall": compute_overall_score(scores)}
    parts = _SENTENCE_SPLIT_RE.split(text)
    kept: list[str] = []
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        separator = parts[i + 1] if i + 1 < len(parts) else ""
        if sentence.strip() and _is_swapped_score_sentence(sentence, known):
            logger.warning("Dropping summary sentence with a misattributed score: %r", sentence)
            if "\n" in separator and kept and "\n" not in kept[-1]:
                kept[-1] = separator  # keep the paragraph break the dropped sentence ended
            continue
        kept.extend([sentence, separator])
    return "".join(kept).strip()


def _basic_summary(findings: list[dict], url: str, scores: dict[str, float]) -> str:
    """Generate a basic summary without AI."""
    severity_counts: dict[str, int] = defaultdict(int)
    for f in findings:
        severity_counts[f.get("severity", "info")] += 1

    parts = [f"Website critique for {url} found {len(findings)} issues."]

    if severity_counts.get("critical"):
        parts.append(f"{severity_counts['critical']} critical issues require immediate attention.")
    if severity_counts.get("high"):
        parts.append(f"{severity_counts['high']} high-severity issues should be prioritized.")

    lowest_cat = min(scores, key=lambda k: scores[k]) if scores else None
    if lowest_cat:
        parts.append(f"The weakest area is {lowest_cat} (score: {scores[lowest_cat]}).")

    return " ".join(parts)


def _format_findings_for_summary(findings: list[dict]) -> str:
    """Format findings for the AI summary prompt."""
    lines: list[str] = []
    critical_high = [f for f in findings if f.get("severity") in ("critical", "high")]
    for f in critical_high[:15]:
        lines.append(
            f"- [{f.get('severity', '').upper()}] {f.get('title', '')}: {f.get('description', '')[:100]}"
        )
    if len(critical_high) > 15:
        lines.append(f"  ... and {len(critical_high) - 15} more critical/high findings")
    return "\n".join(lines)


def _md_scores_section(
    scores: dict[str, float], unscored: dict[str, int] | None = None
) -> list[str]:
    unscored = unscored or {}
    if not scores and not unscored:
        return []
    lines = ["## Category Scores", "", "| Category | Score |", "|----------|-------|"]
    rows = {c: f"{_score_emoji(s)} {s}" for c, s in scores.items() if c not in unscored}
    rows.update({c: unscored_label(n) for c, n in unscored.items()})
    for cat, cell in sorted(rows.items()):
        lines.append(f"| {display_category(cat)} | {cell} |")
    if overall_is_unscored(scores, unscored):
        coverage = score_coverage_note(scores, unscored)
        return [*lines, "", f"**Overall Score: {OVERALL_NOT_SCORED}**", "", f"*{coverage}.*", ""]
    if not scores:
        return [*lines, ""]
    # The API and the PDF both report ``compute_overall_score`` (category-
    # WEIGHTED). An unweighted mean here made the Markdown report a second,
    # disagreeing authority for the same job -- {a11y: 60, seo: 90} rendered
    # 75.0 next to the API's 71.2. There is exactly one scorer.
    overall = compute_overall_score(scores)
    lines.extend(["", f"**Overall Score: {overall:.1f}/100**", ""])
    coverage = score_coverage_note(scores, unscored)
    if coverage:
        lines.extend([f"*{coverage}; unverified findings are listed but not scored.*", ""])
    return lines


def _md_finding_block(f: dict) -> list[str]:
    sev = f.get("severity", "info").upper()
    block: list[str] = [
        f"#### [{sev}] {f.get('title', '')}",
        "",
        f.get("description", ""),
    ]
    if f.get("recommendation"):
        block.extend(["", f"**Recommendation:** {f['recommendation']}"])
    if f.get("url"):
        block.extend(["", f"*URL: {f['url']}*"])
    block.append("")
    return block


def _md_findings_section(findings: list[dict]) -> list[str]:
    if not findings:
        return []
    lines: list[str] = ["## Findings", ""]
    by_category: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_category[f.get("category", "other")].append(f)
    for cat, cat_findings in sorted(by_category.items()):
        lines.append(f"### {display_category(cat)} ({len(cat_findings)} findings)")
        lines.append("")
        for f in sorted(cat_findings, key=lambda x: _severity_order(x.get("severity", "info"))):
            lines.extend(_md_finding_block(f))
    return lines


def generate_markdown_report(
    url: str,
    findings: list[dict],
    scores: dict[str, float],
    summary: str,
    scan_duration: float | None = None,
    github_repo: str | None = None,
) -> str:
    """Generate a full Markdown critique report."""
    lines: list[str] = [f"# Website Critique Report: {url}", ""]
    if scan_duration:
        lines.extend([f"*Scan completed in {scan_duration:.1f}s*", ""])

    lines.extend(["## Summary", "", summary, ""])
    lines.extend(_md_scores_section(scores, unscored_categories(findings)))
    lines.extend(_md_findings_section(findings))

    if github_repo:
        lines.extend(["## Code Quality", "", f"Repository: {github_repo}", ""])

    lines.extend(["---", "*Generated by Critique Service*"])
    return "\n".join(lines)


def _score_emoji(score: float) -> str:
    """Deprecated alias for :func:`score_to_grade` (kept for internal callers)."""
    return score_to_grade(score)


def _severity_order(severity: str) -> int:
    """Return sort order for a severity level (lower is more severe)."""
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    return order.get(severity, 5)


def _extract_domain(url: str) -> str:
    """Extract a clean domain name from a URL.

    Examples:
        https://www.example.com/page -> example.com
        https://sub.example.co.uk/path -> sub.example.co.uk
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        # Strip leading www.
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except ValueError, TypeError, KeyError, OSError, RuntimeError:
        return url
