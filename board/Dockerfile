# Board : une page statique (public/index.html) servie par Nginx, qui
# proxifie /api/ vers dashboard-api. Pas d'étape de build : la page est
# copiée telle quelle, et montée en volume par docker-compose.prod.yml pour
# qu'une modification soit visible sans reconstruire l'image.
FROM nginx:alpine
COPY public /usr/share/nginx/html
# .template dans /etc/nginx/templates/ : l'entrypoint officiel de l'image
# lance envsubst dessus au démarrage (TAILSCALE_HOSTNAME, voir
# docker-compose.prod.yml) avant d'écrire /etc/nginx/conf.d/default.conf.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
