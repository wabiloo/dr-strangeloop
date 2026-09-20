import { definePreset } from '@primevue/themes'
import Aura from '@primevue/themes/aura'

/** Aura with a teal/blue "broadcast console" accent -- distinct from
 * cue-graft's violet branding while following the same
 * definePreset(Aura, {...}) pattern. */
export const igorPreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: '{cyan.50}',
      100: '{cyan.100}',
      200: '{cyan.200}',
      300: '{cyan.300}',
      400: '{cyan.400}',
      500: '#0e7490',
      600: '{cyan.600}',
      700: '{cyan.700}',
      800: '{cyan.800}',
      900: '{cyan.900}',
      950: '{cyan.950}',
    },
  },
})
