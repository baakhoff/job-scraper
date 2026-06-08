"""Parser tests against representative guest-endpoint card markup (offline)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.parser import parse_card, parse_detail_html, parse_search_html

SAMPLE = """
<li>
  <div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:3811234567">
    <a class="base-card__full-link"
       href="https://www.linkedin.com/jobs/view/python-dev-3811234567?trk=track"></a>
    <h3 class="base-search-card__title">Python Developer</h3>
    <h4 class="base-search-card__subtitle">
      <a class="hidden-nested-link"
         href="https://www.linkedin.com/company/acme?trk=public_jobs">Acme GmbH</a>
    </h4>
    <span class="job-search-card__location">Berlin, Germany (Remote)</span>
    <time datetime="2026-06-01">1 week ago</time>
  </div>
</li>
<li>
  <div class="base-card" data-entity-urn="urn:li:jobPosting:3899999999">
    <a class="base-card__full-link"
       href="https://www.linkedin.com/jobs/view/backend-3899999999"></a>
    <h3 class="base-search-card__title">Backend Engineer</h3>
    <h4 class="base-search-card__subtitle">Globex</h4>
    <span class="job-search-card__location">Munich, Germany (Hybrid)</span>
  </div>
</li>
<li><div>structural noise, no job id</div></li>
"""


def test_parse_search_html_skips_noise_cards() -> None:
    raw = parse_search_html(SAMPLE)
    assert len(raw) == 2
    assert {r["job_id"] for r in raw} == {"3811234567", "3899999999"}


def test_parse_card_strips_tracking_query_from_url() -> None:
    raw = parse_search_html(SAMPLE)
    first = next(r for r in raw if r["job_id"] == "3811234567")
    assert first["url"] == "https://www.linkedin.com/jobs/view/python-dev-3811234567"
    assert first["title"] == "Python Developer"
    assert first["company"] == "Acme GmbH"
    assert first["posted_at"] == "2026-06-01"


def test_parse_card_captures_company_profile_url() -> None:
    raw = parse_search_html(SAMPLE)
    first = next(r for r in raw if r["job_id"] == "3811234567")
    # Company profile link is pulled from the subtitle anchor, tracking stripped.
    assert first["company_url"] == "https://www.linkedin.com/company/acme"


DETAIL_SAMPLE = """
<html><body>
  <a class="topcard__org-name-link"
     href="https://www.linkedin.com/company/notionhq?trk=public_jobs_topcard">Notion</a>
  <div class="show-more-less-html__markup">We are hiring a Python engineer. Build things.</div>
  <span class="num-applicants__caption">Over 200 applicants</span>
  <ul>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Seniority level</h3>
      <span class="description__job-criteria-text">Mid-Senior level</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text">Full-time</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Job function</h3>
      <span class="description__job-criteria-text">Engineering and Information Technology</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Industries</h3>
      <span class="description__job-criteria-text">Software Development</span>
    </li>
  </ul>
</body></html>
"""


def test_parse_detail_html_extracts_criteria_and_applicants() -> None:
    detail = parse_detail_html(DETAIL_SAMPLE)
    assert detail["seniority"] == "Mid-Senior level"
    assert detail["employment_type"] == "Full-time"
    assert detail["job_function"] == "Engineering and Information Technology"
    assert detail["industries"] == "Software Development"
    assert detail["applicant_count"] == 200
    assert detail["company_url"] == "https://www.linkedin.com/company/notionhq"
    assert "Python engineer" in str(detail["description"])


def test_parse_detail_html_tolerates_missing_fields() -> None:
    detail = parse_detail_html("<html><body>nothing useful</body></html>")
    assert detail["seniority"] is None
    assert detail["applicant_count"] is None
    assert detail["company_url"] is None


def test_job_id_falls_back_to_link_when_no_urn() -> None:
    html = """
    <li><div class="base-card">
      <a class="base-card__full-link"
         href="https://www.linkedin.com/jobs/view/some-role-4012345678"></a>
      <h3 class="base-search-card__title">Role</h3>
      <h4 class="base-search-card__subtitle">Co</h4>
    </div></li>
    """
    card = BeautifulSoup(html, "lxml").select_one("li")
    assert card is not None
    raw = parse_card(card)
    assert raw is not None
    assert raw["job_id"] == "4012345678"
