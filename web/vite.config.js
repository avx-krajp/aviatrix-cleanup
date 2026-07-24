import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath, URL } from 'url';

const root = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: 'dist',
    rollupOptions: {
      input: {
        main: root + 'index.html',
        login: root + 'login.html',
      },
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'https://d18u55gw52pvl2.cloudfront.net',
        changeOrigin: true,
        secure: true,
      },
    },
  },
});
