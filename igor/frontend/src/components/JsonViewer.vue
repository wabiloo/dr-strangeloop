<script setup lang="ts">
/* Read-only, syntax-highlighted, foldable JSON display (CodeMirror 6). Ported from
 * forge-ad-serve's CodeMirrorViewer.vue, trimmed to JSON and light mode (Igor has no dark
 * theme). Never emits edits; takes the already-parsed value and pretty-prints it. */
import { json } from '@codemirror/lang-json'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { EditorState } from '@codemirror/state'
import { tags as t } from '@lezer/highlight'
import { EditorView, basicSetup } from 'codemirror'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ value: unknown; maxHeight?: string }>(), { maxHeight: '16rem' })

const container = ref<HTMLDivElement | null>(null)
let view: EditorView | null = null

const text = () => JSON.stringify(props.value, null, 2) ?? ''

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
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } })
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
