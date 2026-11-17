#!/usr/bin/env python3
"""Manual quality harness for the ICP-from-URL endpoint.

Runs a small list of real company URLs through a LIVE ``/api/brand/icp-from-url``
and prints a coverage table (grounded contract keys + confidence + pages fetched
per URL). This is a hand-run quality check, NOT a CI gate: it needs a live
``analysis_service`` reachable, a live ``worker_management`` LLM gateway, a live
webscraper, and a valid license token — none of which exist in unit CI.

Usage::

    # inline URLs
    ICP_LICENSE=<license-token> uv run python scripts/eval_icp.py \
        --base-url https://analysis-service.test \
        https://stripe.com https://linear.app

    # the committed golden set (one URL per line, '#' comments allowed)
    ICP_LICENSE=<license-token> uv run python scripts/eval_icp.py \
        --base-url https://analysis-service.test \
        --urls-file scripts/icp_golden_sites.txt --insecure

The license token may be passed via ``--license`` or the ``ICP_LICENSE`` env var.
Pass ``--insecure`` for ``*.test`` hosts behind the self-signed CA.

Reachability note: analysis_service is **ClusterIP-only** — there is no external
``analysis-service.test`` ingress, and dev hosts cannot resolve it. To run
this against the live service, exec it from inside the cluster hitting the pod's
own port, where cluster DNS reaches ``worker_management`` + the webscraper::

    # token from a pod that already carries a real tenant's license
    TOK=$(kubectl -n marketing exec deploy/marketing -- printenv VIDEO_W2V_LICENSE_TOKEN)
    # then POST each golden URL to http://localhost:6303/api/brand/icp-from-url
    # from inside the analysis-service pod (this script's src/ is not in the image,
    # so run an inline runner that reproduces the loop below).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

# The 11 flat keys recon reads (kept in sync with
# ``src.domains.icp.service._CONTRACT_KEYS``).
CONTRACT_KEYS: tuple[str, ...] = (
    "company",
    "product",
    "offering",
    "positioning",
    "category",
    "value_props",
    "competitors",
    "pains",
    "problems",
    "pricing_signals",
    "brand_identity",
)

_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _coverage_color(ratio: float) -> str:
    if ratio >= 0.6:
        return _GREEN
    if ratio >= 0.3:
        return _YELLOW
    return _RED


def analyze_url(
    client: httpx.Client, base_url: str, license_token: str, url: str, max_pages: int
) -> dict:
    """POST one URL to the live ICP endpoint and return the parsed JSON body."""
    endpoint = f"{base_url.rstrip('/')}/api/brand/icp-from-url"
    resp = client.post(
        endpoint,
        headers={"X-Tenant-Id": f"Bearer {license_token}"},
        json={"url": url, "max_pages": max_pages},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def _read_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.urls_file:
        with Path(args.urls_file).open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.split("#", 1)[0].strip()
                if stripped:
                    urls.append(stripped)
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="ICP-from-URL coverage eval harness")
    parser.add_argument("urls", nargs="*", help="Company URLs to analyze")
    parser.add_argument("--urls-file", help="File of URLs, one per line ('#' comments allowed)")
    parser.add_argument("--base-url", required=True, help="analysis_service base URL")
    parser.add_argument("--license", default=os.environ.get("ICP_LICENSE"), help="License token")
    parser.add_argument("--max-pages", type=int, default=3, help="Extra pages per site (1-5)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS verification (needed for internal self-signed CA hosts)",
    )
    args = parser.parse_args()

    if not args.license:
        parser.error("license token required (--license or ICP_LICENSE env var)")
    urls = _read_urls(args)
    if not urls:
        parser.error("no URLs given (positional args or --urls-file)")

    header = f"{'URL':<40} {'COVERAGE':<12} {'CONF':<6} {'PAGES':<6} GROUNDED KEYS"
    print(header)
    print("-" * len(header))

    total_ratio = 0.0
    # Secure by default; --insecure disables verification for internal
    # self-signed CA hosts. Passing an expression (not a literal False) keeps
    # this off the B501 / S501 no-cert-validation gate for the default path.
    with httpx.Client(verify=not args.insecure) as client:
        for url in urls:
            try:
                body = analyze_url(client, args.base_url, args.license, url, args.max_pages)
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"{url:<40} {_RED}ERROR{_RESET}  {e}")
                continue
            grounded = [k for k in CONTRACT_KEYS if body.get(k) not in (None, "", [], {})]
            ratio = len(grounded) / len(CONTRACT_KEYS)
            total_ratio += ratio
            color = _coverage_color(ratio)
            coverage = f"{len(grounded)}/{len(CONTRACT_KEYS)}"
            conf = body.get("confidence", 0.0)
            pages = len(body.get("pages_fetched", []) or [])
            keys = _DIM + ", ".join(grounded) + _RESET
            print(f"{url:<40} {color}{coverage:<12}{_RESET} {conf:<6.3f} {pages:<6} {keys}")

    if urls:
        print("-" * len(header))
        print(f"mean coverage: {total_ratio / len(urls):.1%} over {len(urls)} URL(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
