<script setup lang="ts">
/* Read-only, syntax-highlighted, foldable JSON display (CodeMirror 6). Ported from
 * forge-ad-serve's CodeMirrorViewer.vue, trimmed to JSON and light mode (Igor has no dark
 * theme). Never emits edits; takes the already-parsed value and pretty-prints it. */
import { json } from '@codemirror/lang-json'
import { HighlightStyle, foldEffect, ensureSyntaxTree, foldedRanges, syntaxHighlighting, syntaxTree, unfoldEffect } from '@codemirror/language'
import { SearchQuery, findNext, findPrevious, search, setSearchQuery } from '@codemirror/search'
import { EditorState, StateEffect, StateField } from '@codemirror/state'
import type { SyntaxNode } from '@lezer/common'
import { tags as t } from '@lezer/highlight'
import { Decoration, type DecorationSet } from '@codemirror/view'
import { EditorView, basicSetup } from 'codemirror'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{ value: unknown; maxHeight?: string; highlight?: (string | number)[] | null }>(),
  { maxHeight: '16rem', highlight: null },
)

const emit = defineEmits<{ (e: 'search-info', info: { count: number; index: number }): void }>()

const container = ref<HTMLDivElement | null>(null)
let view: EditorView | null = null

// Find box support: the owner drives the query; matches are highlighted and navigated with Enter / Shift+Enter.
let term = ''
const MAX_COUNT = 9999
function searchInfo(state: EditorState) {
  if (!term) return { count: 0, index: 0 }
  const cursor = new SearchQuery({ search: term, caseSensitive: false }).getCursor(state)
  const at = state.selection.main.from
  let count = 0
  let index = 0
  for (let r = cursor.next(); !r.done && count < MAX_COUNT; r = cursor.next()) {
    count++
    if (r.value.from === at) index = count
  }
  return { count, index }
}
function setSearch(text: string) {
  term = text
  if (!view) return
  view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: text, caseSensitive: false })) })
  if (text) findNext(view)
  emit('search-info', searchInfo(view.state))
}
defineExpose({
  setSearch,
  next: () => view && findNext(view),
  prev: () => view && findPrevious(view),
})

const text = () => JSON.stringify(props.value, null, 2) ?? ''

// Fold state survives a content refresh: folds are remembered by their key/index path and re-applied.
const VALUE_NODES = new Set(['Object', 'Array', 'String', 'Number', 'True', 'False', 'Null'])
function containerPath(state: EditorState, node: SyntaxNode): string {
  const path: (string | number)[] = []
  for (let n: SyntaxNode | null = node; n?.parent; n = n.parent) {
    const parent: SyntaxNode = n.parent
    if (parent.name === 'Property') {
      const key = parent.getChild('PropertyName')
      path.unshift(key ? state.sliceDoc(key.from, key.to) : '')
      n = parent
    } else if (parent.name === 'Array') {
      let i = 0
      for (let sib = n.prevSibling; sib; sib = sib.prevSibling) if (VALUE_NODES.has(sib.name)) i++
      path.unshift(i)
    }
  }
  return JSON.stringify(path)
}
function foldedPaths(state: EditorState): Set<string> {
  const out = new Set<string>()
  const tree = syntaxTree(state)
  foldedRanges(state).between(0, state.doc.length, (from) => {
    const open = tree.resolveInner(from, -1)
    const container = open.name === 'Object' || open.name === 'Array' ? open : open.parent
    if (container && (container.name === 'Object' || container.name === 'Array')) out.add(containerPath(state, container))
  })
  return out
}
function refold(v: EditorView, paths: Set<string>) {
  if (!paths.size) return
  const effects: ReturnType<typeof foldEffect.of>[] = []
  syntaxTree(v.state).iterate({
    enter(ref) {
      if ((ref.name === 'Object' || ref.name === 'Array') && ref.to - ref.from > 2 && paths.has(containerPath(v.state, ref.node))) {
        effects.push(foldEffect.of({ from: ref.from + 1, to: ref.to - 1 }))
      }
    },
  })
  if (effects.length) v.dispatch({ effects })
}

// Line highlight for the value at a key/index path (e.g. the timeline item the user clicked).
const setLines = StateEffect.define<DecorationSet>()
const lineMark = Decoration.line({ class: 'cm-path-hl' })
const lineField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setLines)) return e.value
    return tr.docChanged ? Decoration.none : value
  },
  provide: (f) => EditorView.decorations.from(f),
})
function nodeAtPath(state: EditorState, path: (string | number)[]): SyntaxNode | null {
  // The tree is parsed lazily around the viewport; the target may be far below it.
  const tree = ensureSyntaxTree(state, state.doc.length, 5000)
  let node: SyntaxNode | null = tree ? tree.topNode.firstChild : null
  for (const step of path) {
    if (!node) return null
    if (typeof step === 'number') {
      if (node.name !== 'Array') return null
      let i = 0
      let child: SyntaxNode | null = null
      for (let c = node.firstChild; c; c = c.nextSibling) {
        if (!VALUE_NODES.has(c.name)) continue
        if (i++ === step) {
          child = c
          break
        }
      }
      node = child
    } else {
      if (node.name !== 'Object') return null
      let child: SyntaxNode | null = null
      for (let c = node.firstChild; c; c = c.nextSibling) {
        if (c.name !== 'Property') continue
        const key = c.getChild('PropertyName')
        if (key && state.sliceDoc(key.from, key.to) === JSON.stringify(step)) {
          child = c.lastChild
          break
        }
      }
      node = child
    }
  }
  return node
}
function applyHighlight() {
  if (!view) return
  const node = props.highlight ? nodeAtPath(view.state, props.highlight) : null
  if (!node) {
    view.dispatch({ effects: setLines.of(Decoration.none) })
    return
  }
  const doc = view.state.doc
  const first = doc.lineAt(node.from).number
  const last = doc.lineAt(node.to).number
  const ranges = []
  for (let n = first; n <= last; n++) ranges.push(lineMark.range(doc.line(n).from))
  const unfold: ReturnType<typeof unfoldEffect.of>[] = []
  foldedRanges(view.state).between(node.from, node.to, (from, to) => {
    unfold.push(unfoldEffect.of({ from, to }))
  })
  // A fold that merely contains the node (starts before it) must open too.
  foldedRanges(view.state).between(0, view.state.doc.length, (from, to) => {
    if (from <= node.from && to >= node.to) unfold.push(unfoldEffect.of({ from, to }))
  })
  view.dispatch({ effects: [...unfold, setLines.of(Decoration.set(ranges))] })
  // Heights of lines not rendered yet are estimates: aim again once they are measured.
  const at = node.from
  // Only the editor's own scroller moves: EditorView.scrollIntoView would scroll the whole page as well.
  const reveal = () => {
    if (view) view.scrollDOM.scrollTop = Math.max(0, view.lineBlockAt(at).top - 24)
  }
  reveal()
  requestAnimationFrame(() => {
    reveal()
    requestAnimationFrame(reveal)
  })
}

const highlighting = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [t.propertyName, t.keyword], color: 'var(--code-key)' },
    { tag: t.string, color: 'var(--code-string)' },
    { tag: [t.number, t.bool, t.null], color: 'var(--code-number)' },
  ]),
)

const theme = EditorView.theme({
  '&': { fontSize: '0.75rem', maxHeight: props.maxHeight },
  '.cm-content': { fontFamily: "'SFMono-Regular', Consolas, Menlo, monospace", padding: '0.25rem 0' },
  '.cm-scroller': { overflow: 'auto' },
})

onMounted(() => {
  view = new EditorView({
    doc: text(),
    parent: container.value!,
    extensions: [basicSetup, lineField, search({ top: true }), json(), EditorView.updateListener.of((u) => { if (term && (u.docChanged || u.selectionSet)) emit('search-info', searchInfo(u.state)) }), EditorView.editable.of(false), EditorState.readOnly.of(true), theme, highlighting],
  })
})
onMounted(applyHighlight)
onBeforeUnmount(() => view?.destroy())

watch(
  () => props.value,
  () => {
    const next = text()
    if (view && next !== view.state.doc.toString()) {
      const folded = foldedPaths(view.state)
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } })
      refold(view, folded)
      applyHighlight()
      if (term) {
        view.dispatch({ selection: { anchor: 0 } })
        findNext(view)
      }
    }
  },
)
watch(() => props.highlight, applyHighlight)
</script>

<template>
  <div ref="container" class="json-viewer border-round" :style="{ overflow: 'hidden' }"></div>
</template>

<style scoped>
.json-viewer {
  --code-key: var(--p-primary-600, #670386);
  --code-string: #1d7a3c;
  --code-number: #b5540a;
  border: 1px solid var(--p-surface-200);
  background: var(--p-surface-0, #fff);
}
.json-viewer :deep(.cm-path-hl) {
  background: rgba(250, 204, 21, 0.32);
}
</style>
