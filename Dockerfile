# （刻意不写 # syntax=docker/dockerfile:1 —— 该指令会强制联网拉取
#  docker/dockerfile 前端镜像，Docker Hub 不可达时本地构建直接失败；
#  本文件只用 COPY --from 等内置前端已支持的特性）
# agentHub 控制面一体化镜像（Evolution v3 M2）
# 包含：hermes-brain / orchestrator / state-writer / agentctl CLI + agentgateway。
# Worker agent（codex/kimi/pi...）不打包进镜像——由用户在宿主机自装，
# 经心跳向 agentHub 注册（见 docs/ 与 src/hermes/tools.py 的发现语义）。

# ── agentgateway 二进制（按构建架构拉取对应 release）──
# REGISTRY 可覆盖基础镜像源：Docker Hub 不可达时用加速镜像构建——
#   docker compose build --build-arg REGISTRY=docker.m.daocloud.io/library
# 默认 docker.io/library，CI（GitHub runner）无需任何改动。
ARG REGISTRY=docker.io/library
FROM ${REGISTRY}/python:3.12-slim AS agw
ARG AGW_VERSION=1.4.1
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/agentgateway \
    "https://github.com/agentgateway/agentgateway/releases/download/v${AGW_VERSION}/agentgateway-linux-${TARGETARCH}" \
 && install -m 0755 /tmp/agentgateway /usr/local/bin/agentgateway \
 && apt-get purge -y curl \
 && rm -rf /var/lib/apt/lists/* /tmp/agentgateway

# ── 运行时 ──
FROM ${REGISTRY}/python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    LAS_WORKSPACE=/data/workspace
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir . && mkdir -p /data/workspace

COPY infra/agentgateway/config.docker.yaml ./infra/agentgateway/config.docker.yaml
COPY --from=agw /usr/local/bin/agentgateway /usr/local/bin/agentgateway

# /data：SQLite 状态库 + 任务工作区（挂载卷持久化）
VOLUME /data

# 默认角色：state-writer；其他角色经 docker compose command 指定
CMD ["python", "-m", "state.writer"]
