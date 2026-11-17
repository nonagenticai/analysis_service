"""LLM-powered GTM/ICP extraction routed through worker_management.

Mirrors ``domains/brand/services/llm_brand_analyzer.py``: w2v services never
call providers directly — every LLM call goes through
``worker_management.POST /workers/{name}/message`` via the shared
:class:`WMClient` (see memory ``wm-as-sole-llm-gateway``).

The output is the flat key set recon reads (``_BRAND_FIELD_MAP``). An
**anti-fabrication post-filter** keeps only keys whose value is grounded (i.e.
non-empty); ungrounded keys are dropped so the affected facet stays ``unknown``
rather than being guessed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from w2v_common.clients import WMClient

logger = logging.getLogger(__name__)

WORKER_NAME = "icp-analysis"


# The keys the grammar MUST force the model to emit. This is the structured-output
# contract worker_management/vLLM actually enforces, and getting it wrong is what
# let the 2026-08-26 regression happen: with `"required": []` the only legal-but-
# useless answer `{}` was *grammatically valid*, so xgrammar let the model stop
# after three tokens (`{`, `}`, `<eos>`) and every layer below read that as
# success.
#
# Why these three and not others. `required` here is not a quality bar, it is a
# decoding constraint: naming a key forces the model to emit it, so naming a key
# a page cannot ground converts an honest omission into a fabrication — exactly
# what `filter_grounded` exists to prevent. So the set is limited to the facets
# that any landing page a marketing site serves states outright:
#
#   company     — who is behind the site. Read off the page, never inferred.
#   product     — what they sell. The reason the page exists.
#   value_props — why it matters. A landing page IS a list of these.
#
# Deliberately NOT required, and each for the same reason: `pricing_signals` and
# `competitors` are routinely absent from a site altogether; `positioning` and
# `category` are analyst judgements, not page content; `pains` / `problems` are
# inferred; `offering` is a near-duplicate of `product`; and `brand_identity` is
# an object whose empty form `{}` is the very vacuum this guards against.
# Forcing any of them would trade a silent vacuum for a confident invention.
#
# Presence is all the platform can enforce for us — see the fail-closed guard in
# ``routes.extract_icp_from_url`` for why that is only half the answer.
REQUIRED_ICP_KEYS: tuple[str, ...] = ("company", "product", "value_props")

ICP_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "product": {"type": "string"},
        "offering": {"type": "string"},
        "value_props": {"type": "array", "items": {"type": "string"}},
        "pricing_signals": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "brand_identity": {"type": "object"},
        "positioning": {"type": "string"},
        "category": {"type": "string"},
        "competitors": {"type": "array", "items": {"type": "string"}},
        "pains": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    # Populated, and it has to be. See REQUIRED_ICP_KEYS above for the choice of
    # keys; this is the note about the two constraints that are NOT available.
    #
    # `required` is the ONLY keyword on this schema that survives the trip to the
    # decoder. worker_management strips minLength/minItems/maxLength/pattern and
    # friends in `src/utils/xgrammar_schema.py` (`_XGRAMMAR_UNSUPPORTED_KEYS`)
    # because xgrammar cannot compile them and silently disables structured
    # decoding for the whole request when it meets one — and it strips them from
    # the *validator* too, so a non-vacuity constraint written here would be
    # inert rather than merely unenforced. `required` is carried through verbatim
    # (it lives in that module's `_DATA_KEYS`), so it reaches both the grammar and
    # the validator.
    #
    # Consequence, stated plainly: this forces the three keys to be PRESENT, and
    # nothing here can force them to be non-empty. `{"company": "", "product": "",
    # "value_props": []}` is still schema-valid. That residue is caught one layer
    # up, where `filter_grounded` drops empty values and the route fails closed on
    # an empty result.
    "required": list(REQUIRED_ICP_KEYS),
}

# Canonical response keys (snake_case) + camelCase aliases recon also accepts.
_STRING_KEYS = ("company", "product", "offering", "positioning", "category")
_LIST_KEYS = ("value_props", "competitors", "pains", "problems")
_ALIASES = {"valueProps": "value_props", "brandIdentity": "brand_identity"}

# The full flat contract recon reads (``_BRAND_FIELD_MAP``): the 5 string keys,
# the 4 list keys, plus ``pricing_signals`` and ``brand_identity``. Used to score
# grounded-key coverage (confidence).
_CONTRACT_KEYS: tuple[str, ...] = (*_STRING_KEYS, *_LIST_KEYS, "pricing_signals", "brand_identity")

# Per-source character budgets for the (possibly multi-page) prompt. The landing
# page gets the lion's share; each extra page is capped so one huge page can't
# starve the others, and the whole document is bounded.
_LANDING_CHAR_BUDGET = 16000
_EXTRA_PAGE_CHAR_BUDGET = 8000
_PROMPT_CHAR_BUDGET = 40000


def compute_confidence(grounded: dict[str, Any]) -> float:
    """Coverage confidence: grounded contract keys / total contract keys.

    ``grounded`` is the post-filter dict (only contract keys, all non-empty), so
    this is simply how many of the 11 recon facets we could ground. Debug metric —
    recon ignores it and stamps its own ``confidence=0.6``.
    """
    present = sum(1 for k in _CONTRACT_KEYS if k in grounded)
    return round(present / len(_CONTRACT_KEYS), 3)


def merge_page_sections(
    landing_text: str, extra_pages: list[tuple[str, str, str]] | None = None
) -> str:
    """Merge a landing page + labelled extra pages into one budgeted document.

    ``extra_pages`` is a list of ``(label, url, text)`` (e.g. ``("pricing",
    "https://x/pricing", "...")``). The landing text leads; each extra becomes a
    ``## {LABEL} PAGE: {url}`` section, per-page and total-length capped so the
    prompt stays bounded regardless of page sizes. With no extras this returns the
    (budgeted) landing text unchanged — the single-page path.
    """
    parts = [landing_text[:_LANDING_CHAR_BUDGET]]
    used = len(parts[0])
    for label, url, text in extra_pages or []:
        if used >= _PROMPT_CHAR_BUDGET:
            break
        remaining = _PROMPT_CHAR_BUDGET - used
        section_body = text[: min(_EXTRA_PAGE_CHAR_BUDGET, remaining)]
        block = f"\n\n## {label.upper()} PAGE: {url}\n{section_body}"
        parts.append(block)
        used += len(block)
    return "".join(parts)


# Asked for by name, never forced. `required` is a DECODING CONSTRAINT: naming a key there
# makes the grammar emit it whether or not the page grounds it, which is why
# REQUIRED_ICP_KEYS holds only the three facets a marketing landing page always states
# outright (see the fence above). But a key that is neither required NOR mentioned is, in
# practice, never emitted: the prompt used to send only Domain / Page title / Page content,
# so the model filled the three required keys and stopped.
#
# Measured 2026-08-31: clay.com returned a clean 200 with `product`, `offering`, six
# `value_props` and four pricing tiers — and `category`, `positioning`, `pains` and
# `problems` all absent, leaving the dossier's market and problem facets at confidence 0.0.
# merqi.nl's own homepage states all three in plain sentences ("Most 'AI for GTM' makes you
# choose: safe, or autonomous", "Merqi refuses the trade-off", "Keep a human in front of
# every action and the work doesn't scale") and would have grounded them.
#
# So this block closes the gap between "not compelled" and "not asked for", and it is
# written to keep honest omission the easy path: each key is conditional on the page
# actually stating it, and omitting is named as the correct answer rather than a failure.
# `filter_grounded` still drops anything empty downstream.
_OPTIONAL_KEY_GUIDANCE = """
Fill these additional fields ONLY when the page states them. Omitting a field is the
correct answer when the page does not support it — never infer, guess, or generalise
from the industry to fill one:
- category: the kind of product this is, if the page names it.
- positioning: how it differs from alternatives, if the page draws that contrast.
- pains / problems: difficulties the page asserts its buyers have, if it names any.
- offering, pricing_signals, competitors: only if stated outright.
"""


def build_prompt(page_text: str, title: str = "", domain: str = "") -> str:
    """Build the user prompt for ICP extraction (mirror brand ``build_prompt``).

    ``page_text`` may be a single page or a pre-merged multi-section document from
    :func:`merge_page_sections`; it is bounded to the total prompt budget here.

    Names the optional schema keys explicitly and conditionally — see
    :data:`_OPTIONAL_KEY_GUIDANCE` for why asking is not the same as requiring.
    """
    parts = []
    if domain:
        parts.append(f"Domain: {domain}")
    if title:
        parts.append(f"Page title: {title}")
    parts.append(_OPTIONAL_KEY_GUIDANCE)
    truncated_text = page_text[:_PROMPT_CHAR_BUDGET]
    parts.append(f"\nPage content:\n{truncated_text}")
    return "\n".join(parts)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from an LLM response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _coerce_to_dict(raw: Any) -> dict[str, Any]:
    """Normalize WM ``output`` (dict for structured-output, str for text) to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        cleaned = _strip_markdown_fences(raw)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"ICP-analysis JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _is_grounded(value: Any) -> bool:
    """A value counts as grounded only if it is non-empty (recon drops the rest)."""
    return value not in (None, "", [], {})


def _normalize_aliases(data: dict[str, Any]) -> dict[str, Any]:
    """Map camelCase aliases to snake_case; a grounded value wins over an empty one."""
    normalized: dict[str, Any] = {}
    for key, value in data.items():
        canonical = _ALIASES.get(key, key)
        if canonical not in normalized or _is_grounded(value):
            normalized.setdefault(canonical, value)
    return normalized


def _clean_str(value: Any) -> str | None:
    """Return the value if it is a grounded string, else ``None``."""
    return value if isinstance(value, str) and _is_grounded(value) else None


def _clean_list(value: Any) -> list[str] | None:
    """Return a list of grounded stringified items, else ``None``."""
    if isinstance(value, list):
        items = [str(v) for v in value if _is_grounded(v)]
        return items or None
    return None


def filter_grounded(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only recon's known keys with grounded (non-empty) values.

    Coerces value types to the contract (strings/lists), normalizes camelCase
    aliases to snake_case, and drops everything ungrounded. This is the
    anti-fabrication gate: an ungrounded key never reaches the wire.
    """
    normalized = _normalize_aliases(data)
    grounded: dict[str, Any] = {}
    for key in _STRING_KEYS:
        cleaned = _clean_str(normalized.get(key))
        if cleaned is not None:
            grounded[key] = cleaned
    for key in _LIST_KEYS:
        cleaned_list = _clean_list(normalized.get(key))
        if cleaned_list is not None:
            grounded[key] = cleaned_list
    # pricing_signals may be str or list.
    pricing = normalized.get("pricing_signals")
    pricing_clean = _clean_str(pricing) or _clean_list(pricing)
    if pricing_clean is not None:
        grounded["pricing_signals"] = pricing_clean
    # brand_identity is an object.
    identity = normalized.get("brand_identity")
    if isinstance(identity, dict) and _is_grounded(identity):
        grounded["brand_identity"] = identity
    return grounded


async def analyze_icp_with_llm(
    wm_client: WMClient,
    prompt: str,
    *,
    model_override: str | None = None,
) -> tuple[dict[str, Any], float, dict[str, Any] | None]:
    """Call WM and return ``(grounded_fields, confidence, token_usage)``.

    Raises whatever ``WMClient.message`` raises (transport / HTTP errors) plus
    :class:`ValueError` / :class:`json.JSONDecodeError` on shape errors — the
    route maps all of these to a fail-closed ``502``.
    """
    response = await wm_client.message(
        worker_name=WORKER_NAME,
        message=prompt,
        output_schema=ICP_OUTPUT_SCHEMA,
        model_override=model_override,
    )
    # The contract is only worth declaring if it was actually applied. WM echoes
    # back what it used: `output_schema_used` is the (post-strip) schema the
    # decoder was constrained by, and `output_type` is "object" only on the
    # structured path. When guided decoding did NOT engage the model answers
    # unconstrained — which is the state this whole module was silently in while
    # `required` was empty — so say so out loud instead of finding out from a
    # confidence of 0.0 three days later. Not fatal: an unconstrained answer that
    # still parses to a grounded dict is a usable result, and the empty case is
    # already fail-closed by the route.
    if not response.output_schema_used or response.output_type != "object":
        logger.warning(
            "ICP structured output was NOT applied: output_type=%r output_schema_used=%s "
            "- the model answered unconstrained and `required` was not enforced",
            response.output_type,
            bool(response.output_schema_used),
        )
    grounded = filter_grounded(_coerce_to_dict(response.output))
    confidence = compute_confidence(grounded)
    token_usage: dict[str, Any] | None = None
    if response.usage_metrics:
        usage = response.usage_metrics
        token_usage = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    logger.info(
        "ICP LLM analysis complete: grounded_keys=%s confidence=%.3f",
        sorted(grounded),
        confidence,
    )
    return grounded, confidence, token_usage
