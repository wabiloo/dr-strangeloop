import { definePreset } from '@primevue/themes'
import Aura from '@primevue/themes/aura'

// Aura's own Button/Tag theme files hardcode severity colors straight to
// these PRIMITIVE palette names (green/orange/sky/red) -- there is no
// semantic.success/warn/danger/info indirection layer to override instead
// (confirmed against node_modules/@primeuix/themes/dist/aura/{button,tag}).
// So darkening severities means overriding the primitives themselves.
// Each palette below is shifted ~2 steps darker than its normal identity
// mapping (slot N would otherwise just be the stock N) -- 50/100 stay
// readable as a light Tag background tint, everything past 800 floors out
// at the stock palette's near-black 950 instead of continuing past
// legibility.
const darkened = (
  p100: string, p200: string, p300: string, p400: string, p600: string,
  p700: string, p800: string, p900: string, p950: string,
) => ({
  50: p100, 100: p200, 200: p300, 300: p400, 400: p600,
  500: p700, 600: p800, 700: p900, 800: p950, 900: p950, 950: p950,
})

/** Aura with a dark-red "mad scientist" accent, matching the header's
 * wavy-line backdrop (#b91c1c) -- distinct from cue-graft's violet
 * branding while following the same definePreset(Aura, {...}) pattern.
 * green/orange/sky/red (success/warn/info/danger's underlying primitives)
 * are darkened the same way, so status tags and action buttons read as
 * muted/blackened rather than the stock vivid colors. */
export const igorPreset = definePreset(Aura, {
  primitive: {
    green: darkened('#dcfce7', '#bbf7d0', '#86efac', '#4ade80', '#16a34a', '#15803d', '#166534', '#14532d', '#052e16'),
    orange: darkened('#ffedd5', '#fed7aa', '#fdba74', '#fb923c', '#ea580c', '#c2410c', '#9a3412', '#7c2d12', '#431407'),
    sky: darkened('#e0f2fe', '#bae6fd', '#7dd3fc', '#38bdf8', '#0284c7', '#0369a1', '#075985', '#0c4a6e', '#082f49'),
    red: darkened('#fee2e2', '#fecaca', '#fca5a5', '#f87171', '#dc2626', '#b91c1c', '#991b1b', '#7f1d1d', '#450a0a'),
  },
  semantic: {
    primary: {
      50: '{red.50}',
      100: '{red.100}',
      200: '{red.200}',
      300: '{red.300}',
      400: '{red.400}',
      500: '#b91c1c',
      600: '{red.600}',
      700: '{red.700}',
      800: '{red.800}',
      900: '{red.900}',
      950: '{red.950}',
    },
  },
})
