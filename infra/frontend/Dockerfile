# 课程前端：依赖在镜像里装好，源码运行时挂载进来，vite 负责热更新。
FROM node:22-slim

ENV NODE_ENV=development
WORKDIR /app/frontend

# 锁文件单独一层，源码变化不触发重装。
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
