import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    lib: {
      entry: fileURLToPath(new URL('./src/main.js', import.meta.url)),
      formats: ['es'],
      fileName: () => 'workspace.js',
      cssFileName: 'workspace',
    },
  },
});
