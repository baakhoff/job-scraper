"""One-off capability audit of LinkedIn's public guest job endpoints.

Probes, with real HTTP requests and conservative delays, what data is exposed
beyond what the current pipeline extracts. NOT part of the package; throwaway
investigation tooling. Run with:  python scripts/audit_endpoints.py
"""

from __future__ import annotations

import re
import sys
import time

import httpx
from bs4 import BeautifulSoup

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
DETAIL_URL = "https://www.linkedin.com/jobs/view/{job_id}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DELAY = 4.0  # be polite


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def get(client: httpx.Client, url: str, params: dict[str, str] | None = None) -> httpx.Response:
    time.sleep(DELAY)
    r = client.get(url, params=params, headers=HEADERS)
    print(f"GET {r.url}\n  -> status={r.status_code} bytes={len(r.text)}")
    return r


def count_cards(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    return len(soup.select("li"))


def probe_pagination(client: httpx.Client) -> None:
    banner("1. PAGINATION DEPTH (offset 0, 100, 200, 500, 1000)")
    base = {"keywords": "python", "location": "United States"}
    for start in (0, 100, 200, 500, 1000):
        r = get(client, GUEST_SEARCH_URL, {**base, "start": str(start)})
        n = count_cards(r.text) if r.status_code == 200 else 0
        print(f"  start={start}: cards={n}")


def probe_company_links(client: httpx.Client) -> None:
    banner("2. COMPANY PROFILE LINKS in search HTML")
    r = get(client, GUEST_SEARCH_URL, {"keywords": "python", "location": "United States", "start": "0"})
    soup = BeautifulSoup(r.text, "lxml")
    card = soup.select_one("li")
    if card is None:
        print("  no cards")
        return
    print("  --- first card raw (truncated) ---")
    print(card.prettify()[:2500])
    company_links = re.findall(r'href="([^"]*?/company/[^"]*)"', r.text)
    print(f"\n  /company/ links found: {len(company_links)}")
    for cl in company_links[:5]:
        print(f"    {cl}")
    # benefits / salary insights chips
    for sel in (
        "span.job-search-card__salary-info",
        "div.job-search-card__benefits",
        "span.job-posting-benefits__text",
        "time",
    ):
        els = soup.select(sel)
        print(f"  selector {sel!r}: {len(els)} match(es)"
              + (f" e.g. {els[0].get_text(strip=True)!r}" if els else ""))


def first_job_id(client: httpx.Client) -> str | None:
    r = get(client, GUEST_SEARCH_URL, {"keywords": "software engineer", "location": "United States", "start": "0"})
    m = re.search(r"/jobs/view/[^\"']*?(\d{8,})", r.text)
    if m:
        return m.group(1)
    soup = BeautifulSoup(r.text, "lxml")
    for li in soup.select("li"):
        urn = li.get("data-entity-urn") or ""
        m = re.search(r"(\d{8,})", str(urn))
        if m:
            return m.group(1)
    return None


def probe_detail(client: httpx.Client) -> None:
    banner("3. JOB DETAIL PAGE extra fields")
    job_id = first_job_id(client)
    print(f"  using job_id={job_id}")
    if not job_id:
        print("  could not resolve a job id")
        return
    # The guest jobs UI fetches detail via this API fragment:
    api = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    r = get(client, api)
    if r.status_code != 200 or not r.text.strip():
        print("  jobPosting API empty/failed; trying full view page")
        r = get(client, DETAIL_URL.format(job_id=job_id))
    soup = BeautifulSoup(r.text, "lxml")

    checks = {
        "description markup": "div.show-more-less-html__markup, div.description__text",
        "criteria items": "li.description__job-criteria-item",
        "criteria subheader": "h3.description__job-criteria-subheader",
        "criteria text": "span.description__job-criteria-text",
        "salary (main)": "div.salary, span.compensation__salary, div.compensation__salary-range",
        "applicants (num)": "span.num-applicants__caption, figure.num-applicants__figure",
        "company link": "a.topcard__org-name-link, a[href*='/company/']",
        "company logo": "img.artdeco-entity-image",
        "easy apply": "button.sign-up-modal__outlet, code#applyUrl, .apply-button",
        "posted time": "span.posted-time-ago__text, time",
    }
    for label, sel in checks.items():
        els = soup.select(sel)
        sample = els[0].get_text(" ", strip=True)[:80] if els else ""
        print(f"  {label:22s}: {len(els):2d}  {sample!r}")

    print("\n  --- job criteria (seniority/function/employment type/industry) ---")
    for item in soup.select("li.description__job-criteria-item"):
        h = item.select_one("h3.description__job-criteria-subheader")
        v = item.select_one("span.description__job-criteria-text")
        if h and v:
            print(f"    {h.get_text(strip=True)!r}: {v.get_text(strip=True)!r}")

    # applicant count often hides in a <figcaption> / script
    m = re.search(r"([\d,]+)\s+applicants", r.text)
    print(f"\n  regex 'N applicants': {m.group(0) if m else 'not found'}")
    # company link via regex
    cl = re.search(r'href="(https://www\.linkedin\.com/company/[^"]+)"', r.text)
    print(f"  regex company link: {cl.group(1) if cl else 'not found'}")


def probe_tpr(client: httpx.Client) -> None:
    banner("4. f_TPR DATE FILTER (r86400 / r604800 / r2592000)")
    base = {"keywords": "python", "location": "United States", "start": "0"}
    for label, val in (("none", None), ("24h r86400", "r86400"), ("week r604800", "r604800"), ("month r2592000", "r2592000")):
        params = dict(base)
        if val:
            params["f_TPR"] = val
        r = get(client, GUEST_SEARCH_URL, params)
        soup = BeautifulSoup(r.text, "lxml")
        times = [t.get("datetime") for t in soup.select("time") if t.get("datetime")]
        print(f"  {label:16s}: cards={count_cards(r.text)} sample_dates={times[:3]}")


def probe_geoid(client: httpx.Client) -> None:
    banner("5. geoId PARAM (90000084 = NYC area, 103644278 = US)")
    for label, geo in (("NYC 90000084", "90000084"), ("US 103644278", "103644278")):
        r = get(client, GUEST_SEARCH_URL, {"keywords": "python", "geoId": geo, "start": "0"})
        soup = BeautifulSoup(r.text, "lxml")
        locs = [e.get_text(strip=True) for e in soup.select("span.job-search-card__location")]
        print(f"  {label:16s}: cards={count_cards(r.text)} sample_locs={locs[:4]}")


def main() -> None:
    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        probe_pagination(client)
        probe_company_links(client)
        probe_detail(client)
        probe_tpr(client)
        probe_geoid(client)
    print("\nDONE")


if __name__ == "__main__":
    sys.exit(main())
