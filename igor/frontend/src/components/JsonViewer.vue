<script setup lang="ts">
/* Read-only, syntax-highlighted, foldable JSON display (CodeMirror 6). Ported from
 * forge-ad-serve's CodeMirrorViewer.vue, trimmed to JSON and light mode (Igor has no dark
 * theme). Never emits edits; takes the already-parsed value and pretty-prints it. */
import { json } from '@codemirror/lang-json'
import { HighlightStyle, foldEffect, foldedRanges, syntaxHighlighting, syntaxTree } from '@codemirror/language'
import { EditorState } from '@codemirror/state'
import type { SyntaxNode } from '@lezer/common'
import { tags as t } from '@lezer/highlight'
import { EditorView, basicSetup } from 'codemirror'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ value: unknown; maxHeight?: string }>(), { maxHeight: '16rem' })

const container = ref<HTMLDivElement | null>(null)
let view: EditorView | null = null

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

const highlighting = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [t.propertyName, t.keyword], color: 'var(--code-key)' },
    { tag: t.string, color: 'var(--code-string)' },
    { tag: [t.number, t.bool, t.null], color: 'var(--code-number)' },
  ]),
)

const theme = EditorView.theme({
  '&': { fontSize: '0.75rem' },
  '.cm-content': { fontFamily: "'SFMono-Regular', Consolas, Menlo, monospace", padding: '0.25rem 0' },
  '.cm-scroller': { overflow: 'auto' },
})

onMounted(() => {
  view = new EditorView({
    doc: text(),
    parent: container.value!,
    extensions: [basicSetup, json(), EditorView.editable.of(false), EditorState.readOnly.of(true), theme, highlighting],
  })
})
onBeforeUnmount(() => view?.destroy())

watch(
  () => props.value,
  () => {
    const next = text()
    if (view && next !== view.state.doc.toString()) {
      const folded = foldedPaths(view.state)
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } })
      refold(view, folded)
    }
  },
)
</script>

<template>
  <div ref="container" class="json-viewer border-round" :style="{ maxHeight, overflow: 'auto' }"></div>
</template>

<style scoped>
.json-viewer {
  --code-key: var(--p-primary-600, #670386);
  --code-string: #1d7a3c;
  --code-number: #b5540a;
  border: 1px solid var(--p-surface-200);
  background: var(--p-surface-0, #fff);
}
.json-viewer :deep(.cm-editor) {
  height: 100%;
}
</style>
