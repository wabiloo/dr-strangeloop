import MarkdownIt, { type StateCore } from 'markdown-it'

export interface DocLinkContext {
  /** Repo-relative path of the page being rendered, e.g. `franken-ts/AGENTS.md`. */
  docPath: string
  /** Repo-relative path of every catalogued page -> its slug. */
  pathToSlug: Record<string, string>
}

export interface TocItem {
  level: number
  id: string
  text: string
}

export interface RenderedDoc {
  html: string
  toc: TocItem[]
}

/** GitHub-style heading anchor, so `[x](#startover--catchup)` links written for
 * GitHub keep working. */
export function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .replace(/\s/g, '-')
}

function dirname(path: string): string {
  const i = path.lastIndexOf('/')
  return i < 0 ? '' : path.slice(0, i)
}

/** Resolve `href` against `fromDir`; null when it escapes the repo root. */
export function resolveRepoPath(fromDir: string, href: string): string | null {
  const out: string[] = fromDir ? fromDir.split('/') : []
  for (const part of href.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      if (!out.length) return null
      out.pop()
    } else {
      out.push(part)
    }
  }
  return out.join('/')
}

export function docRoute(slug: string, anchor?: string): string {
  return `#/docs/${slug}${anchor ? `?h=${encodeURIComponent(anchor)}` : ''}`
}

function rewriteLinks(md: InstanceType<typeof MarkdownIt>, ctx: DocLinkContext) {
  md.core.ruler.push('doc_links', (state: StateCore) => {
    for (const block of state.tokens) {
      if (block.type !== 'inline' || !block.children) continue
      let inUnlinked = false
      for (const t of block.children) {
        if (t.type === 'link_open') {
          const href = String(t.attrGet('href') ?? '')
          const [target, hash = ''] = href.split('#')
          const anchor = hash ? decodeURIComponent(hash) : ''
          if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) {
            t.attrSet('target', '_blank')
            t.attrSet('rel', 'noopener noreferrer')
          } else if (!target) {
            t.attrSet('href', docRoute(ctx.pathToSlug[ctx.docPath] ?? '', anchor))
            t.attrSet('data-doc', ctx.pathToSlug[ctx.docPath] ?? '')
            t.attrSet('data-anchor', anchor)
          } else {
            const resolved = resolveRepoPath(dirname(ctx.docPath), target.split('?')[0])
            const slug = resolved === null ? undefined : ctx.pathToSlug[resolved]
            if (slug) {
              t.attrSet('href', docRoute(slug, anchor))
              t.attrSet('data-doc', slug)
              t.attrSet('data-anchor', anchor)
            } else {
              // A source file or folder: not part of the docs, so show it as a path, not a dead link.
              t.type = 'doc_unlinked_open'
              t.attrs = [['title', resolved ?? target]]
              inUnlinked = true
            }
          }
        } else if (t.type === 'link_close' && inUnlinked) {
          t.type = 'doc_unlinked_close'
          inUnlinked = false
        }
      }
    }
  })
}

export function renderMarkdown(source: string, ctx: DocLinkContext): RenderedDoc {
  // html: false -- repo Markdown is rendered as text, never as live HTML.
  const md = new MarkdownIt({ html: false, linkify: true, typographer: false })
  // Fuzzy matching would turn bare `README.md` / `serve.py` into links (.md and .py are TLDs).
  md.linkify.set({ fuzzyLink: false, fuzzyEmail: false })
  md.renderer.rules.doc_unlinked_open = (tokens, idx) =>
    `<span class="doc-unlinked" title="${md.utils.escapeHtml(String(tokens[idx].attrGet('title') ?? ''))}">`
  md.renderer.rules.doc_unlinked_close = () => '</span>'
  rewriteLinks(md, ctx)

  const toc: TocItem[] = []
  const seen = new Map<string, number>()
  md.core.ruler.push('heading_ids', (state) => {
    state.tokens.forEach((t, i) => {
      if (t.type !== 'heading_open') return
      const text = (state.tokens[i + 1]?.children ?? []).map((c) => (c.type === 'text' || c.type === 'code_inline' ? c.content : '')).join('')
      const base = slugify(text) || 'section'
      const n = seen.get(base) ?? 0
      seen.set(base, n + 1)
      const id = n === 0 ? base : `${base}-${n}`
      t.attrSet('id', id)
      const level = Number(t.tag.slice(1))
      if (level >= 2 && level <= 3) toc.push({ level, id, text })
    })
  })

  const defaultTable = md.renderer.rules.table_open
  md.renderer.rules.table_open = (tokens, idx, options, env, self) =>
    `<div class="doc-table-wrap">${defaultTable ? defaultTable(tokens, idx, options, env, self) : self.renderToken(tokens, idx, options)}`
  md.renderer.rules.table_close = () => '</table></div>'

  return { html: md.render(source), toc }
}
