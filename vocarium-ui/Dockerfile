FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

# Unprivilegiertes Nginx: laeuft als uid 101, Port 3000 braucht kein root.
FROM nginxinc/nginx-unprivileged:alpine
# Nur VOCARIUM_* wird per envsubst in die Konfiguration eingesetzt; $host,
# $uri usw. bleiben Nginx-Variablen.
ENV NGINX_ENVSUBST_FILTER="^VOCARIUM_" \
    VOCARIUM_PROXY_SECRET=""
COPY --from=build /app/dist /usr/share/nginx/html
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
EXPOSE 3000
