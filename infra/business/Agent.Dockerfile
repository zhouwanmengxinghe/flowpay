# 先用 prepare-runtime.ps1 按 uv.lock 下载到 var/business/python-deps。
# 基础镜像沿用本机教学镜像；构建过程无需访问公网。
FROM flowpay-teaching-backend
COPY python-deps /opt/business-deps
ENV PYTHONPATH=/opt/business-deps:/app/backend/src
