import { createApp } from 'vue'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import ConfirmationService from 'primevue/confirmationservice'
import 'primeicons/primeicons.css'
import 'primeflex/primeflex.css'
import App from './App.vue'
import router from './router'
import { igorPreset } from './theme/igor-preset'
import './assets/main.css'

const app = createApp(App)

app.use(router)
app.use(ToastService)
app.use(ConfirmationService)
app.use(PrimeVue, {
  theme: {
    preset: igorPreset,
    options: {
      darkModeSelector: false,
    },
  },
})

app.mount('#app')
