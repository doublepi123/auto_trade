FROM node:20-alpine AS builder
WORKDIR /app
ARG VITE_AUTO_TRADE_API_KEY=
ENV VITE_AUTO_TRADE_API_KEY=$VITE_AUTO_TRADE_API_KEY
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -qO- http://127.0.0.1/ || exit 1
