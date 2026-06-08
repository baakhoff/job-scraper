# LinkedIn public guest-endpoint capability audit

Date: 2026-06-08. Method: real HTTP requests with a 4 s delay between calls,
logged-out (no cookies/auth), via `scripts/audit_endpoints.py`. Keyword
`python` / `software engineer`, location `United States`. Raw output is
reproduced inline below each finding.

> The guest endpoints are undocumented and change without notice. Treat the
> specifics here as a snapshot, not a contract.

## TL;DR — what changed in the pipeline

| Finding | Status | Action taken |
| --- | --- | --- |
| Company profile URL is in the search HTML | ✅ available, free | Now captured (`company_url`) end-to-end |
| Page size is ~10, not 25 | ⚠️ silent under-fetch | Scraper now advances `start` by the **actual** card count |
| Deep pagination works to ~start=975, then `400` | ✅ confirmed | Already handled (400 ⇒ end-of-results) |
| Detail page exposes employment type / job function / industries / applicants | ✅ available, +1 req/job | Parsed; opt-in `search --details` enriches listings |
| `f_TPR` date filter | ✅ works | Already supported (`posted_within_seconds`) |
| `geoId` targeting | ✅ accepted | Already supported (`geo_id`) |
| Salary range | ❌ absent on sampled guest cards/details | Field kept; populated only when LinkedIn includes it |
| Easy-apply flag | ❌ not reliably distinguishable on guest pages | Deliberately not modelled (would be guesswork) |

## 1. Pagination depth — how far back can we go?

`start` is honoured well past the first page; LinkedIn caps the guest result
set at ~1000 and returns **HTTP 400** beyond it (already treated as
end-of-results in `scraper.fetch_page`).

```
start=0:    status=200  cards=10
start=100:  status=200  cards=9
start=200:  status=200  cards=10
start=500:  status=200  cards=10
start=1000: status=400  cards=0   <- past the end of the available set
```

**Practical limit:** roughly `start≈975` (the 400 marks the wall). The
existing scraper walks until the first empty/400 page, so it already reaches
the deepest available listings — *provided the offset step is right* (see next).

**Bug found & fixed — page size is ~10, not 25.** The endpoint returned 9–10
cards per call, but the scraper stepped `start` by a hard-coded `PAGE_SIZE = 25`.
Stepping by 25 while only 10 come back **skips ~60% of listings** (jobs at
offsets 10–24 were never fetched). The scraper now advances `start` by the
number of cards actually returned on the previous page, which is robust to
LinkedIn changing the page size again. `PAGE_SIZE` is retained only as a
fallback/initial hint.

## 2. Company profile links in search HTML — yes, free

The company name in `<h4 class="base-search-card__subtitle">` wraps an anchor
to the company's public profile. No extra request needed.

```html
<h4 class="base-search-card__subtitle">
  <a class="hidden-nested-link"
     href="https://www.linkedin.com/company/bostonceltics?trk=public_jobs_jserp-result_job-search-card-subtitle">
    Boston Celtics
  </a>
</h4>
```

All 10 cards on the page carried a `/company/...` link. Note some are on
country subdomains (`ca.linkedin.com`, `uk.linkedin.com`). We strip the `?trk`
tracking query and store the clean URL as `company_url`. The card also carries
a company logo (`img.artdeco-entity-image[data-delayed-url]`) and an "Actively
Hiring" badge (`span.job-posting-benefits__text`) — low value, not stored.

## 3. Job detail page — substantial extra fields

The logged-out detail fragment used by the guest UI:
`https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}` returns a
rich HTML document. For `job_id=4406118990` (a Notion role):

```
description markup : present (full HTML description)
applicants         : "Over 200 applicants"        (regex: "200 applicants")
company link       : https://www.linkedin.com/company/notionhq
job criteria:
  'Seniority level': 'Not Applicable'
  'Employment type': 'Full-time'
  'Job function'   : 'Engineering and Information Technology'
  'Industries'     : 'Software Development'
salary             : absent on this posting (no compensation block)
easy apply         : an "Apply" button is present, but guest markup does not
                     reliably distinguish Easy-Apply from off-site apply
```

Newly captured from the detail page: **employment type, job function,
industries, applicant count** (plus full description & seniority that the model
already had). Salary is parsed when LinkedIn includes a "Base salary" criterion
but is usually absent on public postings. Easy-apply is intentionally **not**
modelled — it can't be told apart reliably without auth, so a flag would be
misleading.

Enrichment is opt-in via `python main.py search --details` because it costs one
extra (rate-limited) request per job.

## 4. `f_TPR` date filter — works

Confirmed the time-posted-range filter narrows results as documented. With
`r86400` every returned card was dated today; without it, older dates appear.

```
none            : sample_dates=['2026-06-05', '2026-06-05', '2026-05-29']
24h  r86400     : sample_dates=['2026-06-08', '2026-06-08', '2026-06-08']
week r604800    : sample_dates=['2026-06-05', '2026-06-05', '2026-06-04']
month r2592000  : sample_dates=['2026-06-05', '2026-06-05', '2026-05-29']
```

Already supported through `SearchParams.posted_within_seconds` → `f_TPR=r{N}`.

## 5. `geoId` targeting — accepted

`geoId` is accepted without a `location` string and returns results. (LinkedIn
resolves its own internal geo ids; the human-readable `location` string is the
more predictable knob for ad-hoc use, but `geoId` works for precise targeting
when you know the id.)

```
geoId=103644278 (United States): cards=10  sample_locs=['Boston, MA', 'United States', 'Indianapolis, IN', ...]
geoId=90000084                 : cards=10  sample_locs=['San Francisco, CA', ...]
```

Already supported through `SearchParams.geo_id` → `geoId`.

## Reproducing

```bash
python scripts/audit_endpoints.py   # ~25 requests, 4s apart (be polite)
```
