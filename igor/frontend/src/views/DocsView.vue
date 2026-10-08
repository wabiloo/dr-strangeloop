<script setup lang="ts">
import Message from 'primevue/message'
import ProgressSpinner from 'primevue/progressspinner'
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { getDocPage, listDocs } from '../api/client'
import type { DocPage, DocSection } from '../api/types'
import { renderMarkdown, type RenderedDoc } from '../utils/markdown'

const props = defineProps<{ slug?: string }>()

const route = useRoute()
const router = useRouter()

// Served by Igor itself (see igor/src/igor/app/main.py); absolute so they work
// from the dev server (proxied) and from the built app alike.
const apiDocs = [
  {
    title: 'Igor API',
    summary: 'Interactive OpenAPI docs for this console’s own /api/v1 (Swagger UI).',
    href: '/api/docs',
    icon: 'pi-server',
  },
  {
    title: 'Igor API (ReDoc)',
    summary: 'The same Igor OpenAPI schema as a readable reference.',
    href: '/api/redoc',
    icon: 'pi-file',
  },
  {
    title: 'Channel API',
    summary: 'What every running channel serves: manifests, /timeline.json, /health, startover and catchup.',
    href: '/api/v1/docs/channel-api/docs',
    icon: 'pi-play-circle',
  },
]
const apiSchemas = [
  { label: 'Igor schema (JSON)', href: '/api/openapi.json' },
  { label: 'Channel API spec (YAML)', href: '/api/v1/docs/channel-api/openapi.yaml' },
]

const sections = ref<DocSection[]>([])
const catalogError = ref('')
const page = ref<DocPage | null>(null)
const rendered = ref<RenderedDoc | null>(null)
const pageError = ref('')
const loading = ref(false)
const content = ref<HTMLElement | null>(null)

const pathToSlug = computed(() => {
  const map: Record<string, string> = {}
  for (const s of sections.value) for (const e of s.entries) map[e.path] = e.slug
  return map
})

onMounted(async () => {
  try {
    sections.value = await listDocs()
  } catch (e) {
    catalogError.value = e instanceof Error ? e.message : String(e)
  }
})

function scrollToAnchor() {
  const anchor = typeof route.query.h === 'string' ? route.query.h : ''
  const el = anchor ? content.value?.querySelector(`[id="${CSS.escape(anchor)}"]`) : null
  if (el) el.scrollIntoView({ block: 'start' })
  else window.scrollTo({ top: 0 })
}

let requestId = 0
async function loadPage(slug: string | undefined) {
  const mine = ++requestId
  pageError.value = ''
  if (!slug) {
    page.value = null
    rendered.value = null
    return
  }
  loading.value = true
  try {
    const result = await getDocPage(slug)
    if (mine !== requestId) return
    page.value = result
    rendered.value = null
  } catch (e) {
    if (mine !== requestId) return
    page.value = null
    rendered.value = null
    pageError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (mine === requestId) loading.value = false
  }
}

watch(() => props.slug, loadPage, { immediate: true })

// Render once both the page and the catalog (needed to resolve links between pages) are in.
watch(
  [page, pathToSlug],
  async () => {
    if (!page.value) return
    rendered.value = renderMarkdown(page.value.markdown, { docPath: page.value.path, pathToSlug: pathToSlug.value })
    await nextTick()
    scrollToAnchor()
  },
  { immediate: true },
)

watch(() => route.query.h, () => nextTick(scrollToAnchor))

function onContentClick(ev: MouseEvent) {
  if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return
  const link = (ev.target as HTMLElement).closest<HTMLAnchorElement>('a[data-doc]')
  if (!link) return
  ev.preventDefault()
  const anchor = link.dataset.anchor
  router.push({ name: 'docs', params: { slug: link.dataset.doc }, query: anchor ? { h: anchor } : {} })
}

const tocItems = computed(() => rendered.value?.toc ?? [])
</script>

<template>
  <div class="docs-layout">
    <aside class="docs-sidebar" aria-label="Documentation">
      <RouterLink to="/docs" class="docs-sidebar-home" :class="{ 'docs-link-active': !slug }">
        <i class="pi pi-home" aria-hidden="true" /> All documentation
      </RouterLink>

      <h3 class="docs-sidebar-heading">API reference</h3>
      <ul class="docs-sidebar-list">
        <li v-for="a in apiDocs" :key="a.href">
          <a :href="a.href" target="_blank" rel="noopener" class="docs-link">
            {{ a.title }} <i class="pi pi-external-link docs-external" aria-hidden="true" />
          </a>
        </li>
      </ul>

      <template v-for="s in sections" :key="s.id">
        <h3 class="docs-sidebar-heading">{{ s.title }}</h3>
        <ul class="docs-sidebar-list">
          <li v-for="e in s.entries" :key="e.slug">
            <RouterLink :to="{ name: 'docs', params: { slug: e.slug } }" class="docs-link" :class="{ 'docs-link-active': slug === e.slug }">
              {{ e.title }}
            </RouterLink>
          </li>
        </ul>
      </template>
    </aside>

    <section class="docs-main">
      <Message v-if="catalogError" severity="error" :closable="false">Could not load the documentation index: {{ catalogError }}</Message>

      <!-- Landing -->
      <div v-if="!slug">
        <h2 class="docs-title">Documentation</h2>
        <p class="docs-lead">
          Everything about Dr. Strangeloop: how the pipeline fits together, every CLI, and the HTTP APIs. Start with the
          <RouterLink :to="{ name: 'docs', params: { slug: 'overview' } }">Overview</RouterLink>.
        </p>

        <h3 class="docs-section-title">API reference</h3>
        <div class="docs-cards">
          <a v-for="a in apiDocs" :key="a.href" :href="a.href" target="_blank" rel="noopener" class="docs-card">
            <span class="docs-card-title"><i class="pi" :class="a.icon" aria-hidden="true" /> {{ a.title }}</span>
            <span class="docs-card-summary">{{ a.summary }}</span>
          </a>
        </div>
        <p class="docs-schemas">
          Raw schemas:
          <template v-for="(a, i) in apiSchemas" :key="a.href">
            <span v-if="i"> · </span>
            <a :href="a.href" target="_blank" rel="noopener">{{ a.label }}</a>
          </template>
        </p>

        <template v-for="s in sections" :key="s.id">
          <h3 class="docs-section-title">{{ s.title }}</h3>
          <div class="docs-cards">
            <RouterLink v-for="e in s.entries" :key="e.slug" :to="{ name: 'docs', params: { slug: e.slug } }" class="docs-card">
              <span class="docs-card-title">{{ e.title }}</span>
              <span class="docs-card-summary">{{ e.summary }}</span>
            </RouterLink>
          </div>
        </template>
      </div>

      <!-- Page -->
      <div v-else>
        <Message v-if="pageError" severity="error" :closable="false">{{ pageError }}</Message>
        <div v-else-if="loading && !page" class="docs-loading"><ProgressSpinner style="width: 2rem; height: 2rem" /></div>
        <div v-else-if="rendered && page" class="docs-page">
          <div class="docs-page-path">{{ page.path }}</div>
          <!-- eslint-disable-next-line vue/no-v-html -- markdown-it runs with html: false, so repo text is escaped -->
          <article ref="content" class="doc-content" @click="onContentClick" v-html="rendered.html" />
        </div>
      </div>
    </section>

    <aside v-if="tocItems.length > 1" class="docs-toc" aria-label="On this page">
      <h3 class="docs-sidebar-heading">On this page</h3>
      <ul class="docs-sidebar-list">
        <li v-for="t in tocItems" :key="t.id" :class="{ 'docs-toc-sub': t.level === 3 }">
          <RouterLink :to="{ name: 'docs', params: { slug }, query: { h: t.id } }" class="docs-link">{{ t.text }}</RouterLink>
        </li>
      </ul>
    </aside>
  </div>
</template>

<style scoped>
.docs-layout {
  display: grid;
  grid-template-columns: 16rem minmax(0, 1fr) 14rem;
  gap: 2rem;
  align-items: start;
}

.docs-sidebar,
.docs-toc {
  position: sticky;
  top: 1rem;
  max-height: calc(100vh - 2rem);
  overflow-y: auto;
  font-size: 0.92rem;
}

.docs-sidebar-home {
  display: block;
  padding: 0.35rem 0.6rem;
  margin-bottom: 0.5rem;
  color: #334155;
  text-decoration: none;
  font-weight: 600;
  border-radius: 6px;
}

.docs-sidebar-heading {
  margin: 1.1rem 0 0.35rem;
  padding: 0 0.6rem;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #64748b;
}

.docs-sidebar-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.docs-link {
  display: block;
  padding: 0.28rem 0.6rem;
  color: #334155;
  text-decoration: none;
  border-radius: 6px;
  border-left: 3px solid transparent;
}

.docs-link:hover,
.docs-sidebar-home:hover {
  background: #e2e8f0;
}

.docs-link-active {
  background: #fee2e2;
  border-left-color: var(--p-primary-color, #b91c1c);
  color: #7f1d1d;
  font-weight: 600;
}

.docs-external {
  font-size: 0.7em;
  margin-left: 0.2rem;
  color: #94a3b8;
}

.docs-toc-sub .docs-link {
  padding-left: 1.4rem;
  font-size: 0.86rem;
}

.docs-main {
  min-width: 0;
}

.docs-title {
  margin: 0 0 0.5rem;
}

.docs-lead {
  max-width: 46rem;
  color: #475569;
}

.docs-section-title {
  margin: 1.8rem 0 0.7rem;
}

.docs-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr));
  gap: 0.8rem;
}

.docs-card {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  padding: 0.85rem 1rem;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  text-decoration: none;
  color: inherit;
  transition: border-color 0.12s ease, box-shadow 0.12s ease;
}

.docs-card:hover {
  border-color: var(--p-primary-color, #b91c1c);
  box-shadow: 0 1px 6px rgb(15 23 42 / 0.1);
}

.docs-card-title {
  font-weight: 600;
  color: #0f172a;
}

.docs-card-title > i {
  margin-right: 0.3rem;
  color: var(--p-primary-color, #b91c1c);
}

.docs-card-summary {
  font-size: 0.88rem;
  color: #64748b;
}

.docs-schemas {
  margin: 0.7rem 0 0;
  font-size: 0.88rem;
  color: #64748b;
}

.docs-loading {
  display: flex;
  justify-content: center;
  padding: 3rem;
}

.docs-page {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 1.25rem 2rem 2rem;
}

.docs-page-path {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: 0.78rem;
  color: #94a3b8;
}

/* Rendered Markdown is injected via v-html, so it needs :deep(). */
.doc-content {
  color: #1e293b;
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.doc-content :deep(h1) {
  margin: 0.4rem 0 1rem;
  font-size: 1.9rem;
}

.doc-content :deep(h2) {
  margin: 2rem 0 0.7rem;
  padding-bottom: 0.3rem;
  border-bottom: 1px solid #e2e8f0;
  font-size: 1.4rem;
}

.doc-content :deep(h3) {
  margin: 1.5rem 0 0.5rem;
  font-size: 1.1rem;
}

.doc-content :deep(h4) {
  margin: 1.2rem 0 0.4rem;
  font-size: 1rem;
}

.doc-content :deep(h1),
.doc-content :deep(h2),
.doc-content :deep(h3),
.doc-content :deep(h4) {
  scroll-margin-top: 1rem;
}

.doc-content :deep(a) {
  color: var(--p-primary-color, #b91c1c);
}

.doc-content :deep(code) {
  background: #f1f5f9;
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-size: 0.88em;
}

.doc-content :deep(pre) {
  background: #0f172a;
  color: #e2e8f0;
  padding: 0.85rem 1rem;
  border-radius: 6px;
  overflow-x: auto;
  line-height: 1.5;
}

.doc-content :deep(pre code) {
  background: none;
  padding: 0;
  color: inherit;
  font-size: 0.82rem;
}

.doc-content :deep(blockquote) {
  margin: 1rem 0;
  padding: 0.1rem 1rem;
  border-left: 4px solid #cbd5e1;
  color: #475569;
}

.doc-content :deep(.doc-table-wrap) {
  overflow-x: auto;
  margin: 1rem 0;
}

.doc-content :deep(table) {
  border-collapse: collapse;
  font-size: 0.9rem;
}

.doc-content :deep(th),
.doc-content :deep(td) {
  border: 1px solid #e2e8f0;
  padding: 0.4rem 0.7rem;
  text-align: left;
  vertical-align: top;
}

.doc-content :deep(th) {
  background: #f8fafc;
}

.doc-content :deep(.doc-unlinked) {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: 0.88em;
  color: #475569;
  border-bottom: 1px dotted #94a3b8;
  cursor: help;
}

.doc-content :deep(hr) {
  border: 0;
  border-top: 1px solid #e2e8f0;
  margin: 1.5rem 0;
}

@media (max-width: 1250px) {
  .docs-layout {
    grid-template-columns: 16rem minmax(0, 1fr);
  }
  .docs-toc {
    display: none;
  }
}

@media (max-width: 800px) {
  .docs-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .docs-sidebar {
    position: static;
    max-height: none;
  }
  .docs-page {
    padding: 1rem;
  }
}
</style>
