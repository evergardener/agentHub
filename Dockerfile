# （刻意不写 # syntax=docker/dockerfile:1 —— 该指令会强制联网拉取
#  docker/dockerfile 前端镜像，Docker Hub 不可达时本地构建直接失败；
#  本文件只用 COPY --from 等内置前端已支持的特性）
# agentHub 控制面一体化镜像（Evolution v3 M2）
# 包含：hermes-brain / orchestrator / state-writer / agentctl CLI + agentgateway。
# Worker agent（codex/kimi/pi...）不打包进镜像——由用户在宿主机自装，
# 经心跳向 agentHub 注册（见 docs/ 与 src/hermes/tools.py 的发现语义）。

# ── agentgateway 二进制（按构建架构拉取对应 release）──
# REGISTRY 可覆盖基础镜像源：Docker Hub 不可达时用保持 OCI digest 不变的
# pull-through cache；tag + digest 均固定，更新必须经过 review。
#   docker compose build --build-arg REGISTRY=docker.m.daocloud.io/library
# 默认 docker.io/library，CI（GitHub runner）无需任何改动。
ARG REGISTRY=docker.io/library
ARG PYTHON_TAG=3.12.14-slim-trixie
ARG PYTHON_DIGEST=sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
FROM ${REGISTRY}/python:${PYTHON_TAG}@${PYTHON_DIGEST} AS agw
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/agentgateway \
    "https://github.com/agentgateway/agentgateway/releases/download/v1.4.1/agentgateway-linux-${TARGETARCH}" \
 && case "${TARGETARCH}" in \
      amd64) expected="20f7b298e0c36eef33e7d612b0d0b91d87d43124f59b01f6e9b730477f66d982" ;; \
      arm64) expected="983a0919e30d287ec34ba51a69aa678fb81c5b893a59ae267b29d9fd30365d0e" ;; \
      *) echo "unsupported agentgateway architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && echo "${expected}  /tmp/agentgateway" | sha256sum -c - \
 && install -m 0755 /tmp/agentgateway /usr/local/bin/agentgateway \
 && apt-get purge -y curl \
 && rm -rf /var/lib/apt/lists/* /tmp/agentgateway

# ── 运行时 ──
FROM ${REGISTRY}/python:${PYTHON_TAG}@${PYTHON_DIGEST}

# Debian util-linux security update for CVE-2026-53612 through CVE-2026-53615.
ARG UTIL_LINUX_VERSION=2.41.5-0+deb13u1
ARG BSDUTILS_VERSION=1:2.41.5-0+deb13u1
ARG LOGIN_VERSION=1:4.16.0-2+really2.41.5-0+deb13u1
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bsdutils="${BSDUTILS_VERSION}" \
      libblkid1="${UTIL_LINUX_VERSION}" \
      liblastlog2-2="${UTIL_LINUX_VERSION}" \
      libmount1="${UTIL_LINUX_VERSION}" \
      libsmartcols1="${UTIL_LINUX_VERSION}" \
      libuuid1="${UTIL_LINUX_VERSION}" \
      login="${LOGIN_VERSION}" \
      mount="${UTIL_LINUX_VERSION}" \
      util-linux="${UTIL_LINUX_VERSION}" \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    LAS_WORKSPACE=/data/workspace
WORKDIR /app

# PyPI 不可达/不稳定时：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=""
COPY requirements.lock ./
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} \
      -r requirements.lock \
 && mkdir -p /data/workspace

COPY src ./src
COPY config ./config
COPY scripts/agentctl-container /usr/local/bin/agentctl
COPY infra/agentgateway/config.docker.yaml ./infra/agentgateway/config.docker.yaml
COPY --from=agw /usr/local/bin/agentgateway /usr/local/bin/agentgateway

# /data：SQLite 状态库 + 任务工作区（挂载卷持久化）
VOLUME /data

# 默认角色：state-writer；其他角色经 docker compose command 指定
CMD ["python", "-m", "state.writer"]
