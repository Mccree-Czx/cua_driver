# screening 服务镜像骨架：仅拷贝源码与 contracts，无安装步骤、骨架不可构建，M2 完成化
FROM python:3.12-slim

WORKDIR /app/services/screening

COPY services/screening/ /app/services/screening/
COPY packages/contracts/ /app/packages/contracts/
COPY pyproject.toml /app/pyproject.toml

# 端口约定 8001（与 pipeline SCREENING_URL 默认对齐）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
