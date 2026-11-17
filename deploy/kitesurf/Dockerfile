# Kitesurf 使用自己的 Node、Bazel 和 Rust/WASM 构建链，与 Fulcrum Python 镜像独立。
# 固定源码提交保证可复现；升级时核对 CDP 协议并显式修改该提交，而非运行时追踪 main。
FROM node:22-bookworm-slim AS builder
ARG KITESURF_REV=39feadfb9a3e6c14b40cf170a559ce417d0fce5d
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git build-essential python3 unzip zip pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm@9.15.4 @bazel/bazelisk@1.27.0
WORKDIR /opt/kitesurf
RUN git init . \
    && git remote add origin https://github.com/cloudflare/kitesurf.git \
    && git fetch --depth 1 origin "$KITESURF_REV" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$KITESURF_REV" \
    && pnpm install --frozen-lockfile \
    && pnpm run build

FROM node:22-bookworm-slim AS runtime
# 运行阶段仅保留预构建产物和 Wrangler 依赖，不把 Rust/Bazel 下载缓存带入镜像。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm@9.15.4
COPY --from=builder --chown=node:node /opt/kitesurf /opt/kitesurf
# 上游 local 环境还绑定开发测试用的 fixtures Worker；独立截图服务使用专用配置，
# 保留公开网络限制、动态 Worker 加载和字体资源，不依赖未启动的测试或 tracing 服务。
COPY --chown=node:node deploy/kitesurf/wrangler.fulcrum.jsonc /opt/kitesurf/packages/engine/wrangler.fulcrum.jsonc
WORKDIR /opt/kitesurf/packages/engine
# 官方建议本地长时间运行打开 GC，否则连续导航留下的 isolate 可能持续占用内存。
ENV MINIFLARE_WORKERD_V8_FLAGS=--expose-gc
ENV WRANGLER_SEND_METRICS=false
USER node
EXPOSE 8787
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8787/health', {signal: AbortSignal.timeout(3000)}).then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]
# 本地环境保留上游默认的私网请求拦截，不传 KITESURF_ALLOW_PRIVATE_NETWORK=1。
# 调试 fixtures worker 不是截图运行依赖，因此只启动引擎配置，不额外暴露测试服务。
CMD ["pnpm", "exec", "wrangler", "dev", "--config", "wrangler.fulcrum.jsonc", "--ip", "0.0.0.0", "--port", "8787"]
