# Static frontend + reverse proxy. The upstream production image builds frontend/dist
# but never serves it on :3000, so this stage copies it into nginx.
FROM nginx:1.27-alpine
COPY --from=app /app/frontend/dist /usr/share/nginx/html
COPY deploy/web/nginx.conf /etc/nginx/conf.d/default.conf
