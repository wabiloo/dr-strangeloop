<script setup lang="ts">
// Fired once, briefly, the moment a channel actually transitions from
// not-running to running -- see ChannelDetail.vue's onJobFinished, which
// is the only caller of `trigger()`. Purely decorative (its-a-live's own
// namesake pun), so it fails silently/never blocks anything: no props, no
// events, no state the rest of the page depends on.
import { reactive, ref } from 'vue'

const visible = ref(false)
let hideTimer: ReturnType<typeof setTimeout> | null = null

// Fallback for the resting line's height when the players aren't found in
// the DOM (shouldn't normally happen -- trigger() only fires once a channel
// goes live, by which point PlaybackPanel.vue is already mounted).
const FALLBACK_REST_TOP_REM = 24

// Number of sampled points along the curve (indices 0..CURVE_STEPS), spread
// evenly across keyframe percentages 0%-70% -- see @keyframes its-alive-enter.
// Dense enough that the per-segment linear interpolation CSS does between
// consecutive samples reads as one smooth curve rather than a polyline.
const CURVE_STEPS = 10

function initialStyle() {
  const s: Record<string, string> = {
    '--its-alive-rest-top': `${FALLBACK_REST_TOP_REM}rem`,
    '--its-alive-duration': '0.9s',
    '--its-alive-bounce': '10px',
    '--its-alive-rebound': '3px',
  }
  for (let i = 0; i <= CURVE_STEPS; i++) {
    s[`--its-alive-p${i}-dx`] = '0px'
    s[`--its-alive-p${i}-dy`] = '0px'
    s[`--its-alive-p${i}-angle`] = '0deg'
  }
  return s
}

// CSS custom properties consumed by the enter animation below -- randomized
// per trigger() call so every entrance takes a different path.
const style = reactive<Record<string, string>>(initialStyle())

function randomBetween(min: number, max: number) {
  return Math.random() * (max - min) + min
}

// Symmetric ease-in-out: 0 at u=0, 1 at u=1, S-shaped in between. Higher k
// makes the S sharper (slower start/end, faster middle) -- randomized per
// trigger so the "speed increases, then decreases" by a different amount
// each time. Closed-form (no bezier-solving needed) since it's used to
// pre-warp the SPACING of curve samples below, not as a CSS timing-function.
function easeInOut(u: number, k: number) {
  if (u <= 0) return 0
  if (u >= 1) return 1
  const a = u ** k
  const b = (1 - u) ** k
  return a / (a + b)
}

function cubicBezierPoint(
  p0: { x: number; y: number },
  p1: { x: number; y: number },
  p2: { x: number; y: number },
  p3: { x: number; y: number },
  u: number,
) {
  const m = 1 - u
  const x = m * m * m * p0.x + 3 * m * m * u * p1.x + 3 * m * u * u * p2.x + u * u * u * p3.x
  const y = m * m * m * p0.y + 3 * m * m * u * p1.y + 3 * m * u * u * p2.y + u * u * u * p3.y
  return { x, y }
}

// The entrance always starts off-screen above the page, horizontally
// centered on the header's brand icon -- comfortably clear of the header's
// own height so it's genuinely hidden, not just tucked behind it, before it
// slides down and pops out from underneath.
function startPoint() {
  const iconRect = document.querySelector('.app-brand-icon')?.getBoundingClientRect()
  const headerRect = document.querySelector('.app-header')?.getBoundingClientRect()
  const x = iconRect ? iconRect.left + iconRect.width / 2 : 60
  const headerHeight = headerRect?.height ?? 56
  const y = -(headerHeight + 80)
  return { x, y }
}

// Vertically centered on the live players (PlaybackPanel.vue's
// .video-wrapper elements) -- measured for real, since their position
// depends on page content above them (channel name, tags, etc).
function restTop() {
  const wrappers = [...document.querySelectorAll('.video-wrapper')]
  if (!wrappers.length) {
    const rootFontSizePx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16
    return FALLBACK_REST_TOP_REM * rootFontSizePx
  }
  const rects = wrappers.map((el) => el.getBoundingClientRect())
  const top = Math.min(...rects.map((r) => r.top))
  const bottom = Math.max(...rects.map((r) => r.bottom))
  return (top + bottom) / 2
}

function trigger() {
  if (hideTimer) clearTimeout(hideTimer)

  const viewportWidth = window.innerWidth || 1280
  const restTopPx = restTop()

  const start = startPoint()
  // Always centered horizontally on the page.
  const end = { x: viewportWidth / 2, y: restTopPx }

  const angle = randomBetween(-30, 30)
  const duration = randomBetween(0.8, 1.8)
  // A small landing bounce, height and rebound randomized too.
  const bounce = randomBetween(8, 16)
  const rebound = randomBetween(2, 6)
  // Higher k = a more pronounced slow-fast-slow speed profile along the
  // curve; lower k reads closer to constant speed.
  const easeK = randomBetween(1.6, 4)

  // A swoosh: drop steeply away from the header first, then flatten out into
  // a level approach into the centered resting spot -- control point 1
  // stays close to the start's x (keeping the drop near-vertical) while
  // finishing most of the descent early; control point 2 sits level with
  // the endpoint (making the final approach horizontal).
  const c1 = { x: start.x + (end.x - start.x) * 0.15, y: start.y + (end.y - start.y) * 0.75 }
  const c2 = { x: end.x - (end.x - start.x) * 0.35, y: end.y }

  style['--its-alive-rest-top'] = `${restTopPx.toFixed(0)}px`
  style['--its-alive-duration'] = `${duration.toFixed(2)}s`
  style['--its-alive-bounce'] = `${bounce.toFixed(1)}px`
  style['--its-alive-rebound'] = `${rebound.toFixed(1)}px`

  // Sample the curve at CURVE_STEPS+1 points, evenly spaced in TIME (i/n),
  // but at each one evaluate the curve at an EASED position (u) -- so points
  // bunch up near the start/end (slow) and spread out in the middle (fast),
  // which is what makes the overall motion speed up then slow down even
  // though each individual segment below is drawn with linear interpolation.
  // The base (resting) box sits at left: 50%, top: var(--its-alive-rest-top)
  // -- each dx/dy here is a delta (in px) on top of that fixed point.
  for (let i = 0; i <= CURVE_STEPS; i++) {
    const t = i / CURVE_STEPS
    const u = easeInOut(t, easeK)
    const p = cubicBezierPoint(start, c1, c2, end, u)
    style[`--its-alive-p${i}-dx`] = `${(p.x - end.x).toFixed(1)}px`
    style[`--its-alive-p${i}-dy`] = `${(p.y - end.y).toFixed(1)}px`
    style[`--its-alive-p${i}-angle`] = `${(angle * (1 - u)).toFixed(1)}deg`
  }

  // Re-trigger the enter transition even if a previous banner is still
  // fading, instead of just extending the current one -- two starts close
  // together should each get their own flourish.
  visible.value = false
  requestAnimationFrame(() => {
    visible.value = true
    hideTimer = setTimeout(
      () => {
        visible.value = false
      },
      duration * 1000 + 2800,
    )
  })
}

defineExpose({ trigger })
</script>

<template>
  <Teleport to="body">
    <Transition name="its-alive-backdrop">
      <div v-if="visible" class="its-alive-backdrop" />
    </Transition>
    <Transition name="its-alive">
      <div
        v-if="visible"
        class="its-alive-banner"
        role="status"
        aria-live="polite"
        :style="style"
      >
        <img class="its-alive-image" src="/alive.png" alt="It's a Live!" />
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.its-alive-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1999;
  background: rgba(0, 0, 0, 0.4);
  /* Decorative only -- never block the page underneath. */
  pointer-events: none;
}

.its-alive-backdrop-enter-active,
.its-alive-backdrop-leave-active {
  transition: opacity 0.4s ease;
}
.its-alive-backdrop-enter-from,
.its-alive-backdrop-leave-to {
  opacity: 0;
}

.its-alive-banner {
  position: fixed;
  top: var(--its-alive-rest-top, 24rem);
  left: 50%;
  /* translateY(-50%) is load-bearing, not decorative: top/left above are the
     CENTER point we measured (players' vertical midpoint, random spot along
     the line), but position:fixed + top/left place this box's TOP-LEFT
     CORNER there. Without this, the box's top edge -- not its center --
     sits on that point, and the whole image renders that much further down
     than intended. */
  transform: translateX(-50%) translateY(-50%) rotate(0deg);
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.its-alive-image {
  width: 24rem;
  max-width: 85vw;
  height: auto;
  display: block;
  filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22));
  animation: its-alive-flicker 1.3s ease-in-out infinite;
}

@keyframes its-alive-flicker {
  0%,
  100% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22)) drop-shadow(0 0 0 transparent);
  }
  12% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22))
      drop-shadow(0 0 16px var(--p-primary-color, #b91c1c));
  }
  24% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22)) drop-shadow(0 0 0 transparent);
  }
  34% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22))
      drop-shadow(0 0 20px var(--p-primary-color, #b91c1c));
  }
  46% {
    filter: drop-shadow(0 12px 36px rgba(0, 0, 0, 0.22)) drop-shadow(0 0 0 transparent);
  }
}

.its-alive-enter-active {
  /* 0-70%: travel the curved path (see trigger()'s cubicBezierPoint sampling
     -- steeply down first, flattening into a horizontal approach) sampled
     densely enough that per-segment linear interpolation reads as one
     smooth curve. The "speed increases, then decreases" feel is baked into
     how those samples are spaced in time (denser near the ends), not into a
     CSS timing-function -- hence `linear` here, so CSS doesn't layer a
     second, competing ease on top. 70-100%: a small overshoot-and-settle
     bounce on the way down onto the line (height/rebound randomized), which
     DOES use its own per-stop timing-functions since it's just two discrete
     stops, not a dense curve. Vue detects animationend the same way it
     detects transitionend, so this still works as a normal enter-active
     class. */
  animation: its-alive-enter var(--its-alive-duration, 0.9s) linear both;
}
.its-alive-leave-active {
  transition: opacity 0.6s ease;
}
.its-alive-leave-to {
  opacity: 0;
}

/* Curve stops 0%-70%: each one's dx/dy/angle is a sample of a real cubic
   bezier (computed in trigger()'s cubicBezierPoint loop), so consecutive
   stops trace a smooth curve rather than a couple of straight legs meeting
   at a kink. -50% on both axes at every stop is the same load-bearing
   centering as the base class's rest transform (see .its-alive-banner) --
   the per-point dx/dy/angle are deltas added on top via calc(), so the
   box's CENTER (not its corner) is what travels the path. Timing between
   stops is `linear` (set on .its-alive-enter-active) since the speed-up-
   then-slow-down feel is already baked into how densely these samples are
   spaced in time. */
@keyframes its-alive-enter {
  0% {
    transform: translateX(calc(-50% + var(--its-alive-p0-dx, 0px))) translateY(calc(-50% + var(--its-alive-p0-dy, 0px)))
      rotate(var(--its-alive-p0-angle, 0deg));
    opacity: 0;
    /* Below .app-header's z-index (10) for this first slice, so it's
       genuinely hidden behind the header's opaque background rather than
       sliding down on top of it. */
    z-index: 5;
  }
  7% {
    transform: translateX(calc(-50% + var(--its-alive-p1-dx, 0px))) translateY(calc(-50% + var(--its-alive-p1-dy, 0px)))
      rotate(var(--its-alive-p1-angle, 0deg));
    opacity: 1;
  }
  14% {
    transform: translateX(calc(-50% + var(--its-alive-p2-dx, 0px))) translateY(calc(-50% + var(--its-alive-p2-dy, 0px)))
      rotate(var(--its-alive-p2-angle, 0deg));
  }
  21% {
    transform: translateX(calc(-50% + var(--its-alive-p3-dx, 0px))) translateY(calc(-50% + var(--its-alive-p3-dy, 0px)))
      rotate(var(--its-alive-p3-angle, 0deg));
  }
  28% {
    transform: translateX(calc(-50% + var(--its-alive-p4-dx, 0px))) translateY(calc(-50% + var(--its-alive-p4-dy, 0px)))
      rotate(var(--its-alive-p4-angle, 0deg));
  }
  35% {
    transform: translateX(calc(-50% + var(--its-alive-p5-dx, 0px))) translateY(calc(-50% + var(--its-alive-p5-dy, 0px)))
      rotate(var(--its-alive-p5-angle, 0deg));
    z-index: 5;
  }
  /* By 42% of the total duration it's comfortably clear of the header --
     restore the normal above-everything z-index right after (the flip
     itself lands at the midpoint between this stop and the previous one). */
  42% {
    transform: translateX(calc(-50% + var(--its-alive-p6-dx, 0px))) translateY(calc(-50% + var(--its-alive-p6-dy, 0px)))
      rotate(var(--its-alive-p6-angle, 0deg));
    z-index: 2000;
  }
  49% {
    transform: translateX(calc(-50% + var(--its-alive-p7-dx, 0px))) translateY(calc(-50% + var(--its-alive-p7-dy, 0px)))
      rotate(var(--its-alive-p7-angle, 0deg));
  }
  56% {
    transform: translateX(calc(-50% + var(--its-alive-p8-dx, 0px))) translateY(calc(-50% + var(--its-alive-p8-dy, 0px)))
      rotate(var(--its-alive-p8-angle, 0deg));
  }
  63% {
    transform: translateX(calc(-50% + var(--its-alive-p9-dx, 0px))) translateY(calc(-50% + var(--its-alive-p9-dy, 0px)))
      rotate(var(--its-alive-p9-angle, 0deg));
  }
  70% {
    transform: translateX(calc(-50% + var(--its-alive-p10-dx, 0px))) translateY(calc(-50% + var(--its-alive-p10-dy, 0px)))
      rotate(var(--its-alive-p10-angle, 0deg));
  }
  85% {
    transform: translateX(-50%) translateY(calc(-50% - var(--its-alive-bounce, 10px))) rotate(0deg);
    animation-timing-function: ease-out;
  }
  95% {
    transform: translateX(-50%) translateY(calc(-50% + var(--its-alive-rebound, 3px))) rotate(0deg);
    animation-timing-function: ease-in-out;
  }
  100% {
    transform: translateX(-50%) translateY(-50%) rotate(0deg);
    animation-timing-function: ease-out;
  }
}
</style>
