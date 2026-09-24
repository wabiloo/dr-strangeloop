import { nextTick } from 'vue'

// PrimeVue 4's ConfirmPopup does not align itself during its initial enter.
export function alignConfirmPopup(target: HTMLElement) {
  void nextTick(() => {
    requestAnimationFrame(() => {
      const popup = document.querySelector<HTMLElement>('.p-confirmpopup')
      if (!popup) return

      const targetRect = target.getBoundingClientRect()
      const isFixed = getComputedStyle(popup).position === 'fixed'
      const scrollY = isFixed ? 0 : window.scrollY
      const scrollX = isFixed ? 0 : window.scrollX
      const margin = 8
      const left = Math.min(
        Math.max(targetRect.left + targetRect.width / 2 - popup.offsetWidth / 2, margin),
        window.innerWidth - popup.offsetWidth - margin,
      )

      popup.style.top = `${targetRect.bottom + scrollY + 4}px`
      popup.style.left = `${left + scrollX}px`
      popup.style.setProperty(
        '--p-confirmpopup-arrow-left',
        `${targetRect.left + targetRect.width / 2 - left}px`,
      )
    })
  })
}
