import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

async function readWorkflow() {
  return readFile(
    new URL('../../../.github/workflows/dataforseo.yml', import.meta.url),
    'utf8',
  )
}

async function readDeployWorkflow() {
  return readFile(
    new URL('../../../.github/workflows/docs.yml', import.meta.url),
    'utf8',
  )
}

async function readMaintainerGuide() {
  return readFile(
    new URL('../../contributing/dataforseo-seo-monitoring.md', import.meta.url),
    'utf8',
  )
}

function topLevelMapping(source, key) {
  const lines = source.split(/\r?\n/u)
  const start = lines.findIndex((line) => line.startsWith(`${key}:`))
  assert.notEqual(start, -1, `missing top-level ${key} mapping`)

  const block = [lines[start]]
  for (const line of lines.slice(start + 1)) {
    if (line && !/^\s/u.test(line)) break
    block.push(line)
  }

  return block.join('\n').replace(/\s+#.*$/gmu, '')
}

test('paid baselines require manual dispatch and still force depth-100 AIO', async () => {
  const workflow = await readWorkflow()
  const triggers = topLevelMapping(workflow, 'on')

  assert.doesNotMatch(triggers, /\bschedule\b/u)
  assert.doesNotMatch(triggers, /\bcron\b/u)
  assert.match(triggers, /\bworkflow_dispatch\b/u)
  assert.match(workflow, /github\.event_name == 'workflow_dispatch'/)
  assert.match(workflow, /COLLECTION_KIND: \$\{\{ inputs\.run_mode \}\}/)
  assert.match(workflow, /inputs\.run_mode == 'paid' && '100'/)
  assert.match(workflow, /inputs\.run_mode == 'paid' && 'true'/)
  assert.doesNotMatch(workflow, /ENABLE_PAID_DATAFORSEO_SCHEDULE/)
})

test('forks cannot run validation or paid report jobs', async () => {
  const workflow = await readWorkflow()

  assert.match(
    workflow,
    /jobs:\r?\n  test:\r?\n    if: github\.repository == 'Project-N-E-K-O\/N\.E\.K\.O'/,
  )
  assert.match(
    workflow,
    /\r?\n  report:\r?\n    if: >-\r?\n      github\.repository == 'Project-N-E-K-O\/N\.E\.K\.O' &&/,
  )
})

test('maintainer documentation keeps paid collection manual', async () => {
  const guide = await readMaintainerGuide()

  assert.match(guide, /workflow has no `schedule` trigger/)
  assert.match(guide, /paid baseline must be started manually/)
  assert.match(guide, /fixed-name `seo-geo-daily-report` diagnostic artifact/)
  assert.match(guide, /`seo-geo-daily-paid-baseline`/)
  assert.match(guide, /obsolete `ENABLE_PAID_DATAFORSEO_SCHEDULE` variable/)
  assert.doesNotMatch(guide, /set `ENABLE_PAID_DATAFORSEO_SCHEDULE=true`/)
  assert.doesNotMatch(guide, /schedule always runs the paid baseline/)
})

test('one run collects independent CN, online-English, and online-Chinese segments', async () => {
  const workflow = await readWorkflow()

  assert.match(workflow, /dataforseo\.config\.json --mode all/)
  assert.match(workflow, /dataforseo\.cn\.config\.json --mode all --skip-keyword-difficulty/)
  assert.match(workflow, /dataforseo\.online-zh\.config\.json --mode all --skip-keyword-difficulty/)
  assert.match(workflow, /--segment online-en/)
  assert.match(workflow, /--segment cn/)
  assert.match(workflow, /--segment online-zh/)
  assert.match(workflow, /actions\/artifacts\?name=dataforseo-cn-report/)
  assert.match(workflow, /\.name == "dataforseo-cn-report"/)
  assert.match(workflow, /CN_EXTERNAL_REPORT/)
  assert.match(workflow, /mode.*all/)
  assert.match(workflow, /run_status.*complete/)
  assert.match(workflow, /ranking_status.*complete/)
  assert.match(workflow, /keyword_metrics_status.*complete/)
  assert.match(workflow, /ai_overview_status.*complete/)
  assert.match(workflow, /report_status.*complete/)
  assert.match(workflow, /serp_depth.*100/)
  assert.match(workflow, /include_ai_overview.*true/)
  assert.match(workflow, /artifact_date.*today/)
})

test('the unified artifact contains source statuses, raw reports, Markdown, and JSON', async () => {
  const workflow = await readWorkflow()

  assert.match(workflow, /secrets\.GOOGLE_SERVICE_ACCOUNT_JSON/)
  assert.match(workflow, /vars\.GA4_PROPERTY_ID/)
  assert.match(workflow, /vars\.GA4_CN_PROPERTY_ID/)
  assert.match(workflow, /vars\.GSC_SITE_URL/)
  assert.match(workflow, /vars\.GSC_CN_SITE_URL/)
  assert.match(workflow, /actions\/artifacts\?name=indexnow-online-submission/)
  assert.match(workflow, /--name "indexnow-online-submission"/)
  assert.match(workflow, /secrets\.SEO_REPORTS_TOKEN/)
  assert.match(workflow, /Project-N-E-K-O\/N\.E\.K\.O\.OfficialWebsite/)
  assert.match(workflow, /actions\/artifacts\?name=indexnow-cn-submission/)
  assert.match(workflow, /--name "indexnow-cn-submission"/)
  assert.equal((workflow.match(/sort_by\(\.created_at\) \| last/gu) ?? []).length, 4)
  assert.match(workflow, /--indexnow "cn=/)
  assert.match(workflow, /--indexnow "online=/)
  assert.match(workflow, /actions\/artifacts\?name=seo-geo-daily-paid-baseline/)
  assert.match(workflow, /\.name == "seo-geo-daily-paid-baseline"/)
  assert.match(workflow, /\.workflow_run\.head_branch == "main"/)
  assert.match(workflow, /--previous-report "\$PREVIOUS_REPORT"/)
  assert.match(workflow, /PREVIOUS_REPORT_EVIDENCE=https:\/\/github\.com/)
  assert.match(workflow, /npm run seo:report/)
  assert.match(workflow, /assert-report\.mjs --input "\$report" --level daily/)
  assert.match(workflow, /env\.COLLECTION_KIND == 'paid'/)
  assert.match(workflow, /name: Enforce the zero-cost planning contract/)
  assert.match(workflow, /env\.COLLECTION_KIND == 'dry-run'/)
  assert.match(workflow, /\.runStatus == "planned"/)
  assert.match(workflow, /include-hidden-files: true/)
  assert.match(workflow, /if-no-files-found: error/)
  assert.match(workflow, /docs\/\.seo-reports\/\*\*/)
  assert.match(workflow, /!docs\/\.seo-reports\/previous\/\*\*/)
  assert.match(workflow, /name: seo-geo-daily-report/)
  assert.match(workflow, /name: Publish verified paid comparison baseline/)
  assert.match(workflow, /name: seo-geo-daily-paid-baseline/)
  assert.match(workflow, /github\.ref == 'refs\/heads\/main'/)
})

test('documentation deployment retains a fixed-name .online IndexNow status artifact', async () => {
  const workflow = await readDeployWorkflow()

  assert.match(workflow, /--output docs\/\.seo-reports\/indexnow-online\.json/)
  assert.match(workflow, /name: indexnow-online-submission/)
  assert.match(workflow, /if: always\(\)/)
  assert.match(workflow, /if-no-files-found: error/)
  assert.match(workflow, /retention-days: 90/)
})
