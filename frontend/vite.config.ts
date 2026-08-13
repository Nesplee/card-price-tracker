// En dev (npm run dev), Vite sert le frontend sur le port 5173 et proxifie
// toute requête vers /api/... au serveur FastAPI local (uvicorn --reload,
// port 8000, lancé séparément) -- évite les soucis de CORS en dev sans avoir
// à configurer CORS côté FastAPI (inutile en prod, où Nginx fait ce même
// proxy à l'intérieur du réseau Docker, voir Task 10).
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
