# DataForSEO SEO Monitoring

This maintainer-only tool turns the documentation site's tracked keyword list into a sanitized JSON report containing:

- Google Ads monthly search volume;
- organic keyword difficulty from DataForSEO Labs;
- the target domain's Google organic rank and matched landing page;
- optional Google AI Overview detection and citations of the target domain.

It is not browser code and is never bundled into VitePress. DataForSEO credentials must stay in a local environment or GitHub Actions secrets.

## Safety and evidence contract

DataForSEO bills by request, so GitHub Actions never starts a paid run on a timer. A skipped run and a real zero are different facts:

- pull requests run tests and three `dry-run` plans only; they receive no billing credentials;
- manual workflow dispatch defaults to `dry-run` and sends no paid request;
- the workflow has no `schedule` trigger; routine free observation runs locally, outside GitHub Actions;
- a paid baseline must be started manually and always uses SERP depth 100 with AI Overview loading enabled;
- there is no `ENABLE_PAID_DATAFORSEO_SCHEDULE` variable; an old variable with that name has no effect and should be removed from repository settings;
- SERP depth 100 may bill up to ten result pages per query;
- each SERP request sets `max_crawl_pages` from that depth, making the displayed page count a hard crawl limit;
- asynchronous AI Overview loading can add a charge to every SERP request and is intentionally included in a manually dispatched paid baseline;
- one paid run tracks 19 `.online` English queries, 8 `.cn` Chinese queries, and 3 `.online` Chinese documentation queries;
- explicit transient SERP API failures that report zero cost retry only the failed keyword, at most three attempts with backoff;
- ambiguous network, response-body, or JSON failures are never retried automatically because the completed request may already have been billed;
- a failed response reporting any nonzero cost is never retried automatically, preventing an accidental duplicate charge;
- recoverable keyword failures do not discard successful results; the artifact records `partial` or `failed` status and per-keyword diagnostics;
- account-wide fatal failures stop the run immediately and do not produce an artifact; the sanitized fatal diagnostic includes attempts and any cost reported for the current keyword;
- generated reports live under `docs/.seo-reports/`, are ignored by Git, and are retained as workflow artifacts for 30 days;
- every segment writes an execution-status manifest even when collection fails; a missing expected report makes the workflow fail instead of silently producing a green empty run.

The request plan always states the request count, maximum SERP pages, and number of AIO-enabled calls before execution. A completed paid report records the costs returned by DataForSEO.

::: danger Keep credentials server-side
Never add credentials to `docs/public`, Markdown, tracked JSON, browser code, or a `VITE_*` variable. Vite exposes `VITE_*` values to the client bundle. Use the separate `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD` values from the DataForSEO API Access page; the account password is not the API password.
:::

## Tracked keywords

The baseline uses three independent configs:

| Config | Target | Query count | Paid mode | KD |
| --- | --- | ---: | --- | --- |
| `docs/seo/dataforseo.config.json` | `.online`, US English | 19 | metrics + SERP + AIO | supported |
| `docs/seo/dataforseo.cn.config.json` | `.cn`, China zh-CN | 8 | Volume + SERP + AIO | unsupported for China |
| `docs/seo/dataforseo.online-zh.config.json` | `.online`, China zh-CN | 3 | Volume + SERP + AIO | unsupported for China |

The English config is derived from existing documentation pages and targets `project-neko.online` in US English (`locationCode` 2840).

```json
{
  "targetDomain": "project-neko.online",
  "locationCode": 2840,
  "languageCode": "en",
  "device": "desktop",
  "serpDepth": 100,
  "keywords": [
    {
      "keyword": "live2d ai assistant",
      "landingPage": "/frontend/live2d",
      "intent": "MOFU feature"
    }
  ]
}
```

Keep each keyword unique and mapped to one primary landing page. Missing Volume or KD remains `null`; the tool does not invent a replacement value.

The committed US/English baseline contains 19 phrases. Twelve are strict AI desktop-pet or desktop-companion category terms; the remaining seven measure supporting capabilities such as memory, plugins, and self-hosting. The three Chinese documentation queries are kept in their own segment and point to concrete `.online` pages. The same phrases may also appear in the `.cn` segment because the two domains are measured independently.

DataForSEO Labs does not list China location `2156` for organic keyword difficulty. China KD is therefore `UNSUPPORTED`, never `0` and never borrowed from another market. Google Ads Search Volume uses Google geographical targets, so the China segments still collect Volume; SERP rank, matched URL and AIO also remain available.

Because the default `all` and `keywords` modes call Google Ads Search Volume, each tracked phrase is validated against that endpoint's limit of 80 characters and 10 words before any paid request is sent.

Google Ads `competition` and `competition_index` describe paid-ad competition. They are preserved as `adsCompetition*` fields but are not treated as organic KD. Organic `keywordDifficulty` comes from the separate DataForSEO Labs endpoint.

## Validate without spending

From `docs/`:

```bash
npm ci
npm run test:dataforseo
npm run seo:dataforseo -- --dry-run
```

The last command validates the config and writes a request plan to `.seo-reports/dataforseo-report.json`. It does not require credentials.

## Run locally

Set credentials only in the current shell, then select the smallest required mode:

```bash
export DATAFORSEO_LOGIN='api-login-from-dataforseo'
export DATAFORSEO_PASSWORD='api-password-from-dataforseo'

# Two paid requests: one Volume request and one bulk KD request.
npm run seo:dataforseo -- --mode keywords

# One paid Live SERP request per tracked keyword, depth 100.
npm run seo:dataforseo -- --mode serp --depth 100 --include-ai-overview

# Volume + KD + SERP.
npm run seo:dataforseo -- --mode all
```

Use an alternate segment config explicitly when running outside Actions:

```bash
npm run seo:dataforseo -- --config seo/dataforseo.cn.config.json --mode all --skip-keyword-difficulty --depth 100 --include-ai-overview
npm run seo:dataforseo -- --config seo/dataforseo.online-zh.config.json --mode all --skip-keyword-difficulty --depth 100 --include-ai-overview
```

Use `--output <path>` for a different report path and `--config <path>` for an alternate untracked keyword set.

## Run in GitHub Actions

1. In the target repository, open **Settings → Secrets and variables → Actions**.
2. Add `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD` as secrets. Do not combine them into one public variable.
3. Remove the obsolete `ENABLE_PAID_DATAFORSEO_SCHEDULE` variable if it still exists; the current workflow does not read it.
4. Open **Actions → SEO GEO Daily Report → Run workflow**.
5. Run `dry-run` first and inspect all three request plans.
6. Run `paid`; paid dispatches always force depth 100 and AIO on, even if the dry-run-only inputs were changed.
7. Download the fixed-name `seo-geo-daily-report` diagnostic artifact. It always contains raw reports, all execution manifests, the unified JSON and the unified Markdown, including evidence from a failed gate.
8. A successful paid run on `main` also uploads `seo-geo-daily-paid-baseline`. Only this gate-verified artifact is eligible for next-run rank/AIO comparisons; dry-runs, failed paid runs, and feature-branch runs cannot replace it.
9. There is no automatic paid schedule. Start a paid baseline manually after reviewing the dry-run plan and budget; missing credentials, a missing core report, or a failed technical/content invariant makes it fail after the diagnostic artifact has been uploaded.

Pull requests run the unit tests and committed-config dry-run only. They never receive DataForSEO secrets and never execute a paid request.

The workflow also writes a unified GSC/GA4 Markdown and JSON summary. See [SEO/GEO daily monitoring](./seo-geo-daily-monitoring) for its read-only Google setup and `N/A` behavior.

## Report fields

| Field | Meaning |
| --- | --- |
| `keywordMetrics[].searchVolume` | Approximate average monthly Google Ads search volume |
| `keywordMetrics[].keywordDifficulty` | Organic top-10 difficulty from DataForSEO Labs, 0-100 or `null` |
| `serp[].organicRank` | Rank among organic results (`rank_group`) |
| `serp[].absoluteRank` | Absolute position among all SERP elements (`rank_absolute`) |
| `serp[].landingPageMatched` | Whether Google ranked the configured primary page |
| `serp[].aiOverviewTriggered` | Whether an AIO item appeared |
| `serp[].matchedUrl` | The real URL that ranked for the configured target domain |
| `serp[].aiOverviewCitedTarget` | Whether AIO referenced the segment's target domain or a subdomain |
| `status` | `planned`, `complete`, `partial`, or `failed` |
| `errors[]` | Sanitized per-keyword error, attempts, incurred cost, and cost-guard decisions, including uncertain billing |
| `costs.totalUsd` | Sum of costs returned by the API responses |

SERP crawling stops only when the target is found in an `organic` result. Appearances in other result types do not stop the crawl before the natural ranking can be recorded.

## Official API references

- [Authentication](https://docs.dataforseo.com/v3/auth/)
- [Google Ads Search Volume Live](https://docs.dataforseo.com/v3/keywords_data-google_ads-search_volume-live/)
- [Google Bulk Keyword Difficulty Live](https://docs.dataforseo.com/v3/dataforseo_labs-google-bulk_keyword_difficulty-live/)
- [Google Organic SERP Live Advanced](https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/)
