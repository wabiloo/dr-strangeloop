<script setup lang="ts">
import Toast from 'primevue/toast'
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

const route = useRoute()
const isPlaylistsActive = computed(() => route.path.startsWith('/playlists'))
const isArchivesActive = computed(() => route.path.startsWith('/archives'))
const isManifestsActive = computed(() => route.path.startsWith('/manifests'))
const isChannelsActive = computed(() => route.path.startsWith('/channels'))
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <!-- Purely decorative: an original, repeating wavy-line pattern
           evoking a mad scientist's psychedelic backdrop, tiled across the
           full header width (not a scan/trace of any existing artwork). -->
      <svg class="app-header-waves" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <pattern
            id="app-header-wave-pattern"
            x="0"
            y="0"
            width="180"
            height="56"
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(-7)"
          >
            <g stroke="#b91c1c" stroke-width="2.2" fill="none" opacity="0.5">
              <path d="M0,5 Q45,-5 90,5 Q135,15 180,5" />
              <path d="M0,14 Q45,4 90,14 Q135,24 180,14" />
              <path d="M0,23 Q45,13 90,23 Q135,33 180,23" />
              <path d="M0,32 Q45,22 90,32 Q135,42 180,32" />
              <path d="M0,41 Q45,31 90,41 Q135,51 180,41" />
              <path d="M0,50 Q45,40 90,50 Q135,60 180,50" />
            </g>
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#app-header-wave-pattern)" />
      </svg>

      <div class="app-header-inner">
        <RouterLink to="/channels" class="app-brand">
          <img src="/brand-mark.png" alt="" class="app-brand-icon" />
          <span class="app-brand-text">Dr. Strangeloop</span>
        </RouterLink>

        <nav class="app-nav">
          <RouterLink to="/playlists" class="app-nav-link" :class="{ 'app-nav-link-active': isPlaylistsActive }">
            <i class="pi pi-objects-column" aria-hidden="true" />
            Playlists
          </RouterLink>
          <RouterLink to="/archives" class="app-nav-link" :class="{ 'app-nav-link-active': isArchivesActive }">
            <i class="pi pi-box" aria-hidden="true" />
            Archives
          </RouterLink>
          <RouterLink to="/manifests" class="app-nav-link" :class="{ 'app-nav-link-active': isManifestsActive }">
            <i class="pi pi-link" aria-hidden="true" />
            Manifests
          </RouterLink>
          <RouterLink to="/channels" class="app-nav-link" :class="{ 'app-nav-link-active': isChannelsActive }">
            <i class="pi pi-play-circle" aria-hidden="true" />
            Channels
          </RouterLink>
        </nav>
      </div>
    </header>

    <!-- Continuation of the header's brand mark, as a background layer
         behind the page content -- same source artwork, the section just
         below what the header crop shows, same width/left-offset so the
         two line up. Lives outside .app-main (which caps at 1400px and
         would otherwise clip it) so it can render at full width, the same
         way the header's own wavy background isn't confined to
         .app-header-inner either. -->
    <div class="app-body-mark-wrap">
      <img src="/brand-mark-bottom.png" alt="" class="app-body-mark" />
    </div>

    <main class="app-main">
      <RouterView />
    </main>

    <Toast />
  </div>
</template>

<style scoped>
.app-shell {
  position: relative;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  /* Clips bleed past the true viewport edge only (like .app-header's own
     overflow: hidden already does for the header mark/waves) -- not past
     .app-main's 1400px content column, which is the whole point of hosting
     .app-body-mark-wrap here instead of inside .app-main. */
  overflow-x: hidden;
}

.app-header {
  position: relative;
  /* Just needs to be a definite positive stacking level -- ItsAliveBanner
     dips its OWN z-index below this for the first slice of its entrance (so
     it's hidden behind this opaque background while behind the header),
     then raises it back above everything once clear. Deliberately kept low
     so this doesn't also out-rank real overlays like PrimeVue's Toast
     (z-index 1100) -- a higher value here previously hid toasts under the
     header too. */
  z-index: 10;
  background: #0f172a;
  border-bottom: 1px solid #1e293b;
  overflow: hidden;
}

.app-header-waves {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.app-header-inner {
  position: relative;
  z-index: 1;
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 1.5rem;
  height: 3.5rem;
  display: flex;
  align-items: center;
  gap: 2rem;
}

.app-brand {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  color: #f8fafc;
  text-decoration: none;
  white-space: nowrap;
}

.app-brand-icon {
  /* Matches .app-header-inner's fixed height directly (a percentage here
     wouldn't resolve -- .app-brand's own height is auto/content-sized) so
     the mark sits flush with the header's top and bottom edges. Shown at
     its full (wide) natural width, not cropped down -- and pulled left
     past .app-header-inner's own left padding so it isn't boxed in
     starting at the same edge as the nav text. */
  height: 3.5rem;
  width: auto;
  margin-left: -5rem;
  flex-shrink: 0;
  display: block;
}

.app-body-mark-wrap {
  /* Positioned absolute, outside the flow, so it never pushes .app-main's
     content down -- and NOT capped at 1400px itself, so the image inside
     can render at full width even where that extends past .app-main's
     content column (only .app-shell's overflow-x: hidden above, at the
     true viewport edge, ever clips it -- same as the header). The inner
     max-width/margin/padding here exists purely to reproduce
     .app-header-inner's own box (same values) so `left: -5rem` on the
     image below lands at the identical X the header mark's own
     `margin-left: -5rem` does. */
  position: absolute;
  top: 3.5rem;
  left: 0;
  right: 0;
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 1.5rem;
  z-index: 0;
  pointer-events: none;
}

.app-body-mark {
  /* Low opacity because this sits behind EVERY route via App.vue, not just
     pages with empty space up top -- channel/playlist detail pages have
     real content (name, tags, action buttons) right at the top, which a
     full-opacity image here would visibly compete with/obscure. */
  /* -3.5rem, not -5rem: an absolutely positioned child's `left` is measured
     from the containing block's PADDING edge (i.e. before
     .app-body-mark-wrap's own 1.5rem padding is applied), while the header
     mark's `margin-left: -5rem` is measured from its flex-flow position
     (already past .app-header-inner's matching 1.5rem padding) -- same
     nominal offset, two different reference points 1.5rem apart. Verified
     against getBoundingClientRect() on both marks: this value lands them
     at the identical viewport X. */
  position: absolute;
  top: 0;
  left: -3.5rem;
  width: 270px;
  height: auto;
  opacity: 0.08;
}

.app-brand-text {
  font-family: 'Irish Grover', 'Georgia', serif;
  font-size: 1.9rem;
  letter-spacing: 0.02em;
}

.app-nav {
  display: flex;
  align-items: stretch;
  align-self: stretch;
  gap: 0.25rem;
  flex: 1;
}

.app-nav-link {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  position: relative;
  color: #94a3b8;
  text-decoration: none;
  font-weight: 500;
  font-size: 1.05rem;
  padding: 0 0.9rem;
  transition: color 0.12s ease;
}

.app-nav-link > i {
  font-size: 1.1em;
  -webkit-text-stroke: 0.35px currentColor;
}

.app-nav-link:hover {
  color: #f8fafc;
}

.app-nav-link-active {
  color: #f8fafc;
}

.app-nav-link-active > i {
  color: var(--p-primary-color, #b91c1c);
}

.app-nav-link-active::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 4px;
  background: var(--p-primary-color, #b91c1c);
  z-index: 1;
}

.app-main {
  /* position + z-index so this stacks above .app-body-mark-wrap: a
     positioned element (however low its z-index) always paints above a
     static one regardless of DOM order, so without this the mark would
     render on TOP of the page content instead of behind it. */
  position: relative;
  z-index: 1;
  flex: 1;
  padding: 1.5rem;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  box-sizing: border-box;
}
</style>
