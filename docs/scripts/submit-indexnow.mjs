#!/usr/bin/env node

import { execFileSync } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { isNoindexRoute } from '../.vitepress/indexing-policy.mjs'
import { SOURCE_REWRITES } from '../route-aliases.mjs'

export const SITE_ORIGIN = 'https://project-neko.online'
export const INDEXNOW_ENDPOINT = 'https://api.indexnow.org/indexnow'
export const INDEXNOW_KEY_FILENAME = '8968441f0c1047fbb91a39f7099beaff.txt'

const SCRIPT_DIRECTORY = dirname(fileURLToPath(import.meta.url))
const KEY_FILE_PATH = resolve(
  SCRIPT_DIRECTORY,
  '..',
  'public',
  INDEXNOW_KEY_FILENAME,
)
const MAX_URLS_PER_REQUEST = 10_000
const DEFAULT_REQUEST_TIMEOUT_MILLISECONDS = 15_000
const RETRYABLE_STATUS_CODES = new Set([429, 500, 502, 503, 504])
const EXCLUDED_SOURCE_PATHS = new Set([
  'README_en.md',
  'README_ja.md',
  'README_ru.md',
  'zh-CN/guide/openclaw_guide.md',
  'zh-CN/guide/openclaw_guide.en.md',
  'zh-CN/guide/openclaw_guide.ja.md',
  'zh-CN/guide/openclaw_guide.ko.md',
  'zh-CN/guide/openclaw_guide.ru.md',
  'zh-CN/guide/openclaw_guide.zh-TW.md',
])

function normalizePath(path) {
  return path.replaceAll('\\', '/').replace(/^\.?\//, '')
}

export function documentationPathToUrl(path) {
  const normalizedPath = normalizePath(path)
  if (!normalizedPath.startsWith('docs/')) return null

  const sourcePath = normalizedPath.slice('docs/'.length)
  if (
    !sourcePath.endsWith('.md') ||
    sourcePath.startsWith('.vitepress/') ||
    sourcePath.startsWith('node_modules/') ||
    sourcePath.startsWith('public/') ||
    EXCLUDED_SOURCE_PATHS.has(sourcePath)
  ) {
    return null
  }

  const routeSourcePath = SOURCE_REWRITES[sourcePath] ?? sourcePath
  let route = `/${routeSourcePath}`
  if (route === '/index.md') {
    route = '/'
  } else if (route.endsWith('/index.md')) {
    route = route.slice(0, -'index.md'.length)
  } else {
    route = route.slice(0, -'.md'.length)
  }

  const pageUrl = new URL(route, `${SITE_ORIGIN}/`)
  if (isNoindexRoute(pageUrl.pathname)) return null
  return pageUrl.href
}

export function parseGitNameStatus(output) {
  if (!output) return []

  const fields = output.split('\0')
  const paths = []
  let index = 0

  while (index < fields.length) {
    const status = fields[index++]
    if (!status) continue

    const firstPath = fields[index++]
    if (!firstPath) {
      throw new Error(`Missing path for git diff status ${status}`)
    }
    paths.push(firstPath)

    if (status.startsWith('R') || status.startsWith('C')) {
      const secondPath = fields[index++]
      if (!secondPath) {
        throw new Error(`Missing destination path for git diff status ${status}`)
      }
      paths.push(secondPath)
    }
  }

  return paths
}

function isRevision(value) {
  return value === 'HEAD' || /^[0-9a-f]{7,40}$/i.test(value)
}

export function changedPathsFromGit(base, head = 'HEAD', cwd = process.cwd()) {
  if (!isRevision(base) || !isRevision(head)) {
    throw new Error('IndexNow git revisions must be HEAD or 7-40 hexadecimal characters')
  }
  if (/^0+$/.test(base)) {
    throw new Error('IndexNow cannot diff from an all-zero before SHA')
  }

  const output = execFileSync(
    'git',
    [
      'diff',
      '--name-status',
      '--find-renames',
      '-z',
      base,
      head,
      '--',
      ':(top)docs',
    ],
    { cwd, encoding: 'utf8' },
  )
  return parseGitNameStatus(output)
}

export function normalizeSiteUrl(value) {
  const url = new URL(value, `${SITE_ORIGIN}/`)
  if (url.origin !== SITE_ORIGIN) {
    throw new Error(`IndexNow URL must belong to ${SITE_ORIGIN}: ${value}`)
  }
  if (url.username || url.password) {
    throw new Error(`IndexNow URL must not contain credentials: ${value}`)
  }

  url.hash = ''
  return url.href
}

export function collectIndexNowUrls({
  changedPaths = [],
  explicitUrls = [],
  bootstrap = false,
} = {}) {
  const urls = new Set(explicitUrls.map(normalizeSiteUrl))

  for (const path of changedPaths) {
    const normalizedPath = normalizePath(path)
    const pageUrl = documentationPathToUrl(normalizedPath)
    if (pageUrl) urls.add(pageUrl)

    if (normalizedPath === `docs/public/${INDEXNOW_KEY_FILENAME}`) {
      urls.add(`${SITE_ORIGIN}/`)
    }
  }

  if (bootstrap) urls.add(`${SITE_ORIGIN}/`)
  return [...urls].sort()
}

export async function readIndexNowKey(path = KEY_FILE_PATH) {
  const key = (await readFile(path, 'utf8')).trim()
  if (!/^[A-Za-z0-9-]{8,128}$/.test(key)) {
    throw new Error('IndexNow key must contain 8-128 letters, numbers, or hyphens')
  }
  if (`${key}.txt` !== INDEXNOW_KEY_FILENAME) {
    throw new Error('IndexNow key file name and contents must match')
  }
  return key
}

function retryDelayMilliseconds(response, attempt) {
  const retryAfter = response?.headers?.get?.('retry-after')
  if (retryAfter) {
    const seconds = Number(retryAfter)
    if (Number.isFinite(seconds)) {
      return Math.min(Math.max(seconds * 1000, 0), 10_000)
    }

    const retryDate = Date.parse(retryAfter)
    if (Number.isFinite(retryDate)) {
      return Math.min(Math.max(retryDate - Date.now(), 0), 10_000)
    }
  }
  return Math.min(1000 * 2 ** (attempt - 1), 10_000)
}

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))
}

async function fetchIndexNowResponse(
  fetchImpl,
  endpoint,
  payload,
  timeoutMilliseconds,
) {
  const abortController = new AbortController()
  const timeoutId = setTimeout(() => {
    abortController.abort(
      new Error(`IndexNow request timed out after ${timeoutMilliseconds} ms`),
    )
  }, timeoutMilliseconds)

  try {
    const response = await fetchImpl(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json; charset=utf-8' },
      body: JSON.stringify(payload),
      signal: abortController.signal,
    })
    const responseBody =
      response.status === 200 || response.status === 202
        ? ''
        : (await response.text()).trim().slice(0, 500)
    return { response, responseBody }
  } finally {
    clearTimeout(timeoutId)
  }
}

export async function submitIndexNow(
  urls,
  {
    key,
    endpoint = INDEXNOW_ENDPOINT,
    fetchImpl = globalThis.fetch,
    sleepImpl = sleep,
    maxAttempts = 3,
    requestTimeoutMilliseconds = DEFAULT_REQUEST_TIMEOUT_MILLISECONDS,
  } = {},
) {
  const normalizedUrls = [...new Set(urls.map(normalizeSiteUrl))]
  if (normalizedUrls.length === 0) {
    return { status: null, attempts: 0, submitted: 0 }
  }
  if (normalizedUrls.length > MAX_URLS_PER_REQUEST) {
    throw new Error(
      `IndexNow accepts at most ${MAX_URLS_PER_REQUEST} URLs per request`,
    )
  }
  if (!key) throw new Error('IndexNow key is required')
  if (typeof fetchImpl !== 'function') {
    throw new Error('This Node.js runtime does not provide fetch')
  }
  if (
    !Number.isFinite(requestTimeoutMilliseconds) ||
    requestTimeoutMilliseconds <= 0
  ) {
    throw new Error('IndexNow request timeout must be a positive number')
  }

  const payload = {
    host: new URL(SITE_ORIGIN).host,
    key,
    keyLocation: `${SITE_ORIGIN}/${INDEXNOW_KEY_FILENAME}`,
    urlList: normalizedUrls,
  }

  let lastError
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    let response
    let responseBody
    try {
      const result = await fetchIndexNowResponse(
        fetchImpl,
        endpoint,
        payload,
        requestTimeoutMilliseconds,
      )
      response = result.response
      responseBody = result.responseBody
    } catch (error) {
      lastError = error
      if (attempt === maxAttempts) break
      await sleepImpl(1000 * 2 ** (attempt - 1))
      continue
    }

    if (response.status === 200 || response.status === 202) {
      return {
        status: response.status,
        attempts: attempt,
        submitted: normalizedUrls.length,
      }
    }

    lastError = new Error(
      `IndexNow returned HTTP ${response.status}${
        responseBody ? `: ${responseBody}` : ''
      }`,
    )
    if (!RETRYABLE_STATUS_CODES.has(response.status) || attempt === maxAttempts) {
      break
    }
    await sleepImpl(retryDelayMilliseconds(response, attempt))
  }

  throw lastError ?? new Error('IndexNow submission failed')
}

function parseArguments(argv) {
  const options = {
    base: process.env.INDEXNOW_BASE_SHA || '',
    head: process.env.INDEXNOW_HEAD_SHA || process.env.GITHUB_SHA || 'HEAD',
    dryRun: false,
    explicitUrls: [],
    output: process.env.INDEXNOW_STATUS_OUTPUT || '',
  }

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--base' || argument === '--head' || argument === '--url' || argument === '--output') {
      const value = argv[++index]
      if (!value) throw new Error(`${argument} requires a value`)

      if (argument === '--base') options.base = value
      if (argument === '--head') options.head = value
      if (argument === '--url') options.explicitUrls.push(value)
      if (argument === '--output') options.output = value
      continue
    }
    if (argument === '--dry-run') {
      options.dryRun = true
      continue
    }
    if (argument === '--help') {
      options.help = true
      continue
    }
    throw new Error(`Unknown argument: ${argument}`)
  }

  return options
}

function printHelp() {
  console.log(`Usage: node docs/scripts/submit-indexnow.mjs [options]

Options:
  --base <sha>  Diff base revision used to find changed documentation pages
  --head <sha>  Diff head revision (default: GITHUB_SHA or HEAD)
  --url <url>   Submit an explicit project-neko.online URL; may be repeated
  --output <file>  Write a machine-readable submission status artifact
  --dry-run     Print the resolved URLs without contacting IndexNow
  --help        Show this help

Environment:
  INDEXNOW_BASE_SHA     Same as --base
  INDEXNOW_HEAD_SHA     Same as --head
  INDEXNOW_ENDPOINT     Override the API endpoint (intended for testing)
  INDEXNOW_STATUS_OUTPUT  Same as --output
`)
}

function githubEvidence() {
  const repository = process.env.GITHUB_REPOSITORY
  const runId = process.env.GITHUB_RUN_ID
  return repository && runId
    ? `https://github.com/${repository}/actions/runs/${runId}`
    : null
}

export function createIndexNowStatus({
  runStatus,
  urls = [],
  result = null,
  reason = null,
  submittedAt = null,
} = {}) {
  return {
    schemaVersion: 1,
    provider: 'IndexNow',
    site: SITE_ORIGIN,
    runStatus,
    submittedAt,
    submitted: runStatus === 'complete'
      ? (Number.isFinite(result?.submitted) ? result.submitted : urls.length)
      : null,
    httpStatus: Number.isFinite(result?.status) ? result.status : null,
    attempts: Number.isFinite(result?.attempts) ? result.attempts : 0,
    urls,
    reason,
    evidence: githubEvidence(),
  }
}

async function writeStatus(path, status) {
  if (!path) return
  const outputPath = resolve(path)
  await mkdir(dirname(outputPath), { recursive: true })
  await writeFile(outputPath, `${JSON.stringify(status, null, 2)}\n`, 'utf8')
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  if (options.help) {
    printHelp()
    return
  }

  let urls = []
  try {
    const key = await readIndexNowKey()
    let changedPaths = []
    let bootstrap = process.env.GITHUB_EVENT_NAME === 'workflow_dispatch'

    if (options.base) {
      try {
        changedPaths = changedPathsFromGit(options.base, options.head)
      } catch (error) {
        if (/^0+$/.test(options.base)) {
          console.warn('IndexNow received an all-zero before SHA; submitting the home page.')
          bootstrap = true
        } else {
          throw error
        }
      }
    } else if (options.explicitUrls.length === 0) {
      bootstrap = true
    }

    urls = collectIndexNowUrls({
      changedPaths,
      explicitUrls: options.explicitUrls,
      bootstrap,
    })

    if (urls.length === 0) {
      console.log('IndexNow: no changed documentation URLs to submit.')
      await writeStatus(options.output, createIndexNowStatus({
        runStatus: 'complete',
        urls,
        reason: 'no_changed_urls',
        submittedAt: new Date().toISOString(),
      }))
      return
    }

    console.log(`IndexNow: resolved ${urls.length} URL(s):`)
    for (const url of urls) console.log(`- ${url}`)

    if (options.dryRun) {
      console.log('IndexNow: dry run complete; no request was sent.')
      await writeStatus(options.output, createIndexNowStatus({
        runStatus: 'not_run',
        urls,
        reason: 'dry_run',
      }))
      return
    }

    const result = await submitIndexNow(urls, {
      key,
      endpoint: process.env.INDEXNOW_ENDPOINT || INDEXNOW_ENDPOINT,
    })
    const submittedAt = new Date().toISOString()
    await writeStatus(options.output, createIndexNowStatus({
      runStatus: 'complete',
      urls,
      result,
      submittedAt,
    }))
    console.log(
      `IndexNow: accepted ${result.submitted} URL(s) with HTTP ${result.status} after ${result.attempts} attempt(s).`,
    )
  } catch (error) {
    await writeStatus(options.output, createIndexNowStatus({
      runStatus: 'failed',
      urls,
      reason: error.message,
    }))
    throw error
  }
}

const isEntryPoint =
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href

if (isEntryPoint) {
  main().catch((error) => {
    console.error(`IndexNow: ${error.message}`)
    process.exitCode = 1
  })
}
