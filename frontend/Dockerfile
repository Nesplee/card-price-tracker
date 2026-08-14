# Build multi-étapes : la première étape (node) compile le code
# TypeScript/React en fichiers statiques ; la seconde (nginx) ne contient QUE
# ce résultat compilé -- l'image finale ne contient ni Node.js ni les
# dépendances npm, seulement du HTML/JS/CSS servi par Nginx.
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
