import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import {
  LOCALE_HOME_PATHS,
  SITE_ORIGIN,
} from '../.vitepress/seo-shared.mjs'
import { LEGACY_REDIRECTS } from '../route-aliases.mjs'

const DIST_DIR = resolve('.vitepress/dist')
const PUBLIC_DIR = resolve('public')
const errors = []

function filesRecursively(directory, extension) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolutePath = resolve(directory, entry.name)
    if (entry.isDirectory()) return filesRecursively(absolutePath, extension)
    return entry.isFile() && entry.name.endsWith(extension)
      ? [absolutePath]
      : []
  })
}

function attributes(tag) {
  return Object.fromEntries(
    [...tag.matchAll(/([:@\w-]+)=(?:"([^"]*)"|'([^']*)')/g)].map((match) => [
      match[1].toLowerCase(),
      match[2] ?? match[3] ?? '',
    ]),
  )
}

function tags(html, tagName) {
  const tagPattern = new RegExp(
    `<${tagName}\\b(?:[^>"']|"[^"]*"|'[^']*')*>`,
    'gi',
  )
  return [...html.matchAll(tagPattern)].map((match) => attributes(match[0]))
}

function metaContent(metaTags, key, value) {
  return metaTags.find((tag) => tag[key] === value)?.content ?? ''
}

function decodeXml(value) {
  return value
    .replaceAll('&amp;', '&')
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&quot;', '"')
    .replaceAll('&apos;', "'")
}

function decodeHtml(value) {
  return decodeXml(value)
    .replaceAll('&#39;', "'")
    .replace(/&#(\d+);/g, (_, codePoint) =>
      String.fromCodePoint(Number(codePoint)),
    )
    .replace(/&#x([\da-f]+);/gi, (_, codePoint) =>
      String.fromCodePoint(Number.parseInt(codePoint, 16)),
    )
}

function fail(file, message) {
  errors.push(`${file}: ${message}`)
}

function jsonLdNodes(data) {
  if (!data || typeof data !== 'object') return []
  return Array.isArray(data['@graph']) ? data['@graph'] : [data]
}

function nodeHasType(node, type) {
  const nodeType = node?.['@type']
  return Array.isArray(nodeType)
    ? nodeType.includes(type)
    : nodeType === type
}

if (!existsSync(DIST_DIR)) {
  throw new Error(`Build output does not exist: ${DIST_DIR}`)
}

const robotsPath = resolve(DIST_DIR, 'robots.txt')
const sitemapPath = resolve(DIST_DIR, 'sitemap.xml')
if (!existsSync(robotsPath)) fail('robots.txt', 'file is missing')
if (!existsSync(sitemapPath)) fail('sitemap.xml', 'file is missing')

if (existsSync(robotsPath)) {
  const robots = readFileSync(robotsPath, 'utf8')
  if (!robots.includes('Sitemap: https://project-neko.online/sitemap.xml')) {
    fail('robots.txt', 'does not declare the production sitemap URL')
  }
}

const sitemapUrls = new Set()
if (existsSync(sitemapPath)) {
  const sitemap = readFileSync(sitemapPath, 'utf8')
  for (const match of sitemap.matchAll(/<loc>([\s\S]*?)<\/loc>/g)) {
    sitemapUrls.add(decodeXml(match[1].trim()))
  }
}

const pages = new Map()
const indexableDescriptions = new Map()
const copiedPublicHtml = new Set(
  existsSync(PUBLIC_DIR)
    ? filesRecursively(PUBLIC_DIR, '.html').map((htmlPath) =>
        relative(PUBLIC_DIR, htmlPath).replaceAll('\\', '/'),
      )
    : [],
)
let noindexCount = 0
let indexableCount = 0
const builtLegacyRedirects = new Set()

for (const htmlPath of filesRecursively(DIST_DIR, '.html')) {
  const file = relative(DIST_DIR, htmlPath).replaceAll('\\', '/')
  if (copiedPublicHtml.has(file)) continue
  const html = readFileSync(htmlPath, 'utf8')
  const isNotFound = file === '404.html'
  const metaTags = tags(html, 'meta')
  const linkTags = tags(html, 'link')
  const titleMatches = [...html.matchAll(/<title>([\s\S]*?)<\/title>/gi)]
  const htmlLang = html.match(/<html\b[^>]*\blang="([^"]+)"/i)?.[1] ?? ''
  const description = decodeHtml(metaContent(metaTags, 'name', 'description'))
  const robotsTags = metaTags.filter((tag) => tag.name === 'robots')
  const robots = robotsTags[0]?.content ?? ''
  const robotsDirectives = new Set(
    robots
      .toLowerCase()
      .split(',')
      .map((directive) => directive.trim())
      .filter(Boolean),
  )
  const canonicalLinks = linkTags.filter((tag) => tag.rel === 'canonical')
  const canonical = canonicalLinks[0]?.href ?? ''
  const builtRoute = `/${file.slice(0, -'.html'.length)}`
  const legacyRedirectTarget = LEGACY_REDIRECTS[builtRoute]

  if (legacyRedirectTarget) {
    const expectedCanonical = new URL(
      legacyRedirectTarget,
      `${SITE_ORIGIN}/`,
    ).href
    const refreshTags = metaTags.filter(
      (tag) => tag['http-equiv']?.toLowerCase() === 'refresh',
    )
    if (robotsTags.length !== 1 || !robotsDirectives.has('noindex')) {
      fail(file, 'legacy redirect must contain one noindex robots meta tag')
    }
    if (canonicalLinks.length !== 1 || canonical !== expectedCanonical) {
      fail(file, `legacy redirect canonical must be ${expectedCanonical}`)
    }
    if (
      refreshTags.length !== 1 ||
      refreshTags[0].content !== `0; url=${legacyRedirectTarget}`
    ) {
      fail(file, `legacy redirect refresh must target ${legacyRedirectTarget}`)
    }
    if (!sitemapUrls.has(expectedCanonical)) {
      fail(file, `legacy redirect target is missing from sitemap: ${expectedCanonical}`)
    }
    if (titleMatches.length !== 1 || !titleMatches[0][1].trim()) {
      fail(file, 'legacy redirect must contain one non-empty title')
    }
    if (!htmlLang || (html.match(/<h1\b/gi) ?? []).length !== 1) {
      fail(file, 'legacy redirect must contain html lang and one h1')
    }
    builtLegacyRedirects.add(builtRoute)
    noindexCount += 1
    continue
  }

  if (robotsTags.length !== 1) {
    fail(file, `expected one robots meta tag, found ${robotsTags.length}`)
  }
  if (
    Number(robotsDirectives.has('index')) +
      Number(robotsDirectives.has('noindex')) !==
    1
  ) {
    fail(file, 'robots must contain exactly one of index or noindex')
  }

  if (isNotFound) {
    if (!robotsDirectives.has('noindex')) {
      fail(file, '404 page must be noindex')
    }
    continue
  }

  if (titleMatches.length !== 1 || !titleMatches[0][1].trim()) {
    fail(file, 'must contain exactly one non-empty title')
  }
  if (!description.trim()) fail(file, 'meta description is missing or empty')
  const descriptionLength = Array.from(description).length
  if (descriptionLength < 40 || descriptionLength > 180) {
    fail(
      file,
      `meta description length must be 40-180 characters, found ${descriptionLength}`,
    )
  }
  if (!htmlLang) fail(file, 'html lang is missing')
  if ((html.match(/<h1\b/gi) ?? []).length !== 1) {
    fail(file, 'must contain exactly one h1')
  }
  if (canonicalLinks.length !== 1) {
    fail(file, `expected one canonical link, found ${canonicalLinks.length}`)
  }
  if (!canonical.startsWith(`${SITE_ORIGIN}/`)) {
    fail(file, `canonical must use ${SITE_ORIGIN}: ${canonical || '(missing)'}`)
  }

  const noindex = robotsDirectives.has('noindex')
  if (noindex) {
    noindexCount += 1
    if (sitemapUrls.has(canonical)) {
      fail(file, 'noindex page is present in sitemap.xml')
    }
  } else {
    indexableCount += 1
    const filesWithDescription = indexableDescriptions.get(description) ?? []
    filesWithDescription.push(file)
    indexableDescriptions.set(description, filesWithDescription)
    if (!sitemapUrls.has(canonical)) {
      fail(file, 'indexable page is missing from sitemap.xml')
    }
  }

  const requiredMeta = [
    ['property', 'og:type'],
    ['property', 'og:title'],
    ['property', 'og:description'],
    ['property', 'og:url'],
    ['property', 'og:image'],
    ['name', 'twitter:card'],
    ['name', 'twitter:title'],
    ['name', 'twitter:description'],
    ['name', 'twitter:image'],
  ]
  for (const [key, value] of requiredMeta) {
    if (!metaContent(metaTags, key, value)) fail(file, `${value} is missing`)
  }
  if (metaContent(metaTags, 'property', 'og:url') !== canonical) {
    fail(file, 'og:url does not match canonical')
  }

  const jsonLdBlocks = [
    ...html.matchAll(
      /<script\b[^>]*type=(?:"application\/ld\+json"|'application\/ld\+json')[^>]*>([\s\S]*?)<\/script>/gi,
    ),
  ]
  if (!noindex && jsonLdBlocks.length === 0) {
    fail(file, 'indexable page is missing JSON-LD')
  }
  const parsedJsonLd = []
  for (const block of jsonLdBlocks) {
    try {
      parsedJsonLd.push(JSON.parse(block[1]))
    } catch (error) {
      fail(file, `invalid JSON-LD: ${error.message}`)
    }
  }

  if (!noindex) {
    const nodes = parsedJsonLd.flatMap(jsonLdNodes)
    const canonicalPath = new URL(canonical).pathname
    const isLocaleHome = LOCALE_HOME_PATHS.has(canonicalPath)
    const expectedPrimaryType = isLocaleHome
      ? 'WebPage'
      : canonicalPath.endsWith('/')
        ? 'CollectionPage'
        : null
    const primaryNodes = nodes.filter((node) =>
      ['WebPage', 'CollectionPage', 'TechArticle'].some((type) =>
        nodeHasType(node, type),
      ),
    )

    if (primaryNodes.length !== 1) {
      fail(
        file,
        `expected one primary WebPage/CollectionPage/TechArticle node, found ${primaryNodes.length}`,
      )
    }
    if (
      expectedPrimaryType &&
      !primaryNodes.some((node) => nodeHasType(node, expectedPrimaryType))
    ) {
      fail(file, `expected ${expectedPrimaryType} JSON-LD for ${canonicalPath}`)
    }
    if (
      !expectedPrimaryType &&
      !primaryNodes.some((node) =>
        nodeHasType(node, 'TechArticle') || nodeHasType(node, 'WebPage'),
      )
    ) {
      fail(file, 'detail page must use TechArticle or WebPage JSON-LD')
    }
    if (!primaryNodes.some((node) => node.url === canonical)) {
      fail(file, 'primary JSON-LD node URL does not match canonical')
    }

    const hasArticle = primaryNodes.some((node) =>
      nodeHasType(node, 'TechArticle'),
    )
    const openGraphType = metaContent(metaTags, 'property', 'og:type')
    const expectedOpenGraphType = hasArticle ? 'article' : 'website'
    if (openGraphType !== expectedOpenGraphType) {
      fail(
        file,
        `og:type must be ${expectedOpenGraphType} for the selected page schema`,
      )
    }
    const modifiedTime = metaContent(
      metaTags,
      'property',
      'article:modified_time',
    )
    if (!hasArticle && modifiedTime) {
      fail(file, 'non-article page must not emit article:modified_time')
    }

    if (isLocaleHome) {
      for (const requiredType of [
        'Organization',
        'WebSite',
        'SoftwareApplication',
      ]) {
        if (!nodes.some((node) => nodeHasType(node, requiredType))) {
          fail(file, `home page JSON-LD is missing ${requiredType}`)
        }
      }

      const software = nodes.find((node) =>
        nodeHasType(node, 'SoftwareApplication'),
      )
      const downloadUrls = Array.isArray(software?.downloadUrl)
        ? software.downloadUrl
        : software?.downloadUrl
          ? [software.downloadUrl]
          : []
      const operatingSystems = Array.isArray(software?.operatingSystem)
        ? software.operatingSystem
        : software?.operatingSystem
          ? [software.operatingSystem]
          : []
      if (
        downloadUrls.some((url) =>
          String(url).includes('store.steampowered.com'),
        ) &&
        operatingSystems.some((system) =>
          String(system).toLowerCase().includes('linux'),
        )
      ) {
        fail(
          file,
          'Steam downloadUrl must not be presented as the Linux download channel',
        )
      }
    }
  }

  const alternates = linkTags
    .filter((tag) => tag.rel === 'alternate' && tag.hreflang && tag.href)
    .map((tag) => ({ hreflang: tag.hreflang, href: tag.href }))

  if (pages.has(canonical)) {
    fail(file, `canonical is also used by ${pages.get(canonical).file}`)
  }
  pages.set(canonical, { file, canonical, alternates, noindex })
}

for (const route of Object.keys(LEGACY_REDIRECTS)) {
  if (!builtLegacyRedirects.has(route)) {
    fail(route, 'legacy redirect page was not built')
  }
}

if (sitemapUrls.size !== indexableCount) {
  fail(
    'sitemap.xml',
    `contains ${sitemapUrls.size} URLs but ${indexableCount} indexable HTML pages were built`,
  )
}

for (const [description, files] of indexableDescriptions) {
  if (files.length < 2) continue
  fail(
    'descriptions',
    `duplicate description on ${files.join(', ')}: ${JSON.stringify(description)}`,
  )
}

let hreflangCount = 0
for (const page of pages.values()) {
  if (page.noindex || page.alternates.length === 0) continue
  hreflangCount += page.alternates.length

  if (!page.alternates.some((alternate) => alternate.href === page.canonical)) {
    fail(page.file, 'hreflang cluster does not include the current page')
  }

  for (const alternate of page.alternates) {
    const targetPage = pages.get(alternate.href)
    if (!targetPage) {
      fail(page.file, `hreflang target was not built: ${alternate.href}`)
      continue
    }
    if (alternate.hreflang === 'x-default') continue
    if (
      !targetPage.alternates.some(
        (targetAlternate) => targetAlternate.href === page.canonical,
      )
    ) {
      fail(
        page.file,
        `hreflang target does not link back: ${alternate.href}`,
      )
    }
  }
}

if (errors.length) {
  console.error(`SEO validation failed with ${errors.length} error(s):`)
  for (const error of errors.slice(0, 200)) console.error(`- ${error}`)
  if (errors.length > 200) {
    console.error(`- ...and ${errors.length - 200} more`)
  }
  process.exitCode = 1
} else {
  console.log(
    `SEO validation passed: ${indexableCount} indexable pages, ` +
      `${noindexCount} noindex pages, ${hreflangCount} hreflang links.`,
  )
}
