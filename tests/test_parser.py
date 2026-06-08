"""Parser tests against representative guest-endpoint card markup (offline)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.parser import parse_card, parse_search_html

SAMPLE = """
<li>
  <div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:3811234567">
    <a class="base-card__full-link"
       href="https://www.linkedin.com/jobs/view/python-dev-3811234567?trk=track"></a>
    <h3 class="base-search-card__title">Python Developer</h3>
    <h4 class="base-search-card__subtitle"><a>Acme GmbH</a></h4>
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
