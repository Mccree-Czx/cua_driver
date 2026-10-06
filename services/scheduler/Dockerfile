# scheduler 服务镜像骨架：仅拷贝源码与 contracts，无安装步骤、骨架不可构建，M2 完成化
FROM python:3.12-slim

WORKDIR /app/services/scheduler

COPY services/scheduler/ /app/services/scheduler/
COPY packages/contracts/ /app/packages/contracts/
COPY pyproject.toml /app/pyproject.toml

CMD ["python", "-m", "app.main"]
