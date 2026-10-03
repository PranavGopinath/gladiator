FROM node:20-bookworm-slim
WORKDIR /opt/arena
COPY dist/ ./dist/
USER 1000:1000
CMD ["node", "dist/gateway.js"]
