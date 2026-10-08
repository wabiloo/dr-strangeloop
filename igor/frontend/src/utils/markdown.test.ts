import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderMarkdown, resolveRepoPath, slugify } from './markdown.ts'

const pathToSlug = { 'franken-ts/AGENTS.md': 'franken-ts-agents', 'README.md': 'readme', 'docs/overview.md': 'overview' }

test('slugify matches GitHub anchors', () => {
  assert.equal(slugify('Startover & catchup'), 'startover--catchup')
  assert.equal(slugify('`serve.py` flags'), 'servepy-flags')
})

test('resolveRepoPath normalises and refuses to leave the repo', () => {
  assert.equal(resolveRepoPath('docs', '../franken-ts/AGENTS.md'), 'franken-ts/AGENTS.md')
  assert.equal(resolveRepoPath('', './README.md'), 'README.md')
  assert.equal(resolveRepoPath('docs', '../../x'), null)
})

test('links to catalogued pages become doc routes, others are shown as paths', () => {
  const md = '[a](../franken-ts/AGENTS.md#markers) [b](../README.md) [c](../loop-dee-loop/serve.py) [d](https://example.com)'
  const { html } = renderMarkdown(md, { docPath: 'docs/overview.md', pathToSlug })
  assert.match(html, /href="#\/docs\/franken-ts-agents\?h=markers"/)
  assert.match(html, /href="#\/docs\/readme"/)
  assert.match(html, /<span class="doc-unlinked" title="loop-dee-loop\/serve.py">c<\/span>/)
  assert.match(html, /href="https:\/\/example.com" target="_blank"/)
})

test('in-page anchors stay on the current page', () => {
  const { html } = renderMarkdown('[x](#intro)', { docPath: 'docs/overview.md', pathToSlug })
  assert.match(html, /href="#\/docs\/overview\?h=intro"/)
})

test('headings get unique ids, h2/h3 go to the toc, raw HTML is escaped', () => {
  const { html, toc } = renderMarkdown('# T\n## A\n## A\n### B\n\n<script>alert(1)</script>', { docPath: 'README.md', pathToSlug })
  assert.deepEqual(toc.map((t) => t.id), ['a', 'a-1', 'b'])
  assert.match(html, /<h2 id="a-1">/)
  assert.doesNotMatch(html, /<script>/)
})

test('tables are wrapped for horizontal scrolling', () => {
  const { html } = renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |', { docPath: 'README.md', pathToSlug })
  assert.match(html, /<div class="doc-table-wrap"><table>/)
  assert.match(html, /<\/table><\/div>/)
})

test('bare file names are not auto-linked', () => {
  const { html } = renderMarkdown('see README.md and serve.py', { docPath: 'README.md', pathToSlug })
  assert.doesNotMatch(html, /<a /)
})
