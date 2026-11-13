import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  base: '/admin/',
  server: {
    host: true,
    // Hosts besides localhost/IPs that the dev server answers to (e.g. an SSH
    // tunnel such as *.tuns.sh). A leading dot matches the domain and all its
    // subdomains; add more with IGOR_ALLOWED_HOSTS=host1,host2.
    allowedHosts: ['.tuns.sh', ...(process.env.IGOR_ALLOWED_HOSTS?.split(',').filter(Boolean) ?? [])],
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${process.env.IGOR_PORT ?? 8090}`,
        changeOrigin: true,
      },
    },
  },
})
