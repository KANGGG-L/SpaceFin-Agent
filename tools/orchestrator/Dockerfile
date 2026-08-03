# 注：Docker Hub(registry-1.docker.io) 在本环境被墙，改用 daocloud 镜像加速源
# （等价于 python:3.10-slim）。若网络恢复可改回 FROM python:3.10-slim。
FROM docker.m.daocloud.io/library/python:3.10-slim

WORKDIR /app

# 安装依赖（redis/curl_cffi/lxml 抓取；DrissionPage 渲染 fangyuan）
RUN pip install --no-cache-dir redis curl_cffi lxml DrissionPage

# 安装 Chrome（DrissionPage 渲染 zu.anjuke.com 出租页必需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg unzip ca-certificates fonts-liberation \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
  && wget -q -O /tmp/chrome.deb "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
  && dpkg -i /tmp/chrome.deb || apt-get -f install -y \
  && rm -f /tmp/chrome.deb \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# 复制 SpaceFin 解析包（供 worker 复用解析器）
COPY tools/anjuke_crawler /app/anjuke_crawler
COPY tools/orchestrator/master.py /app/master.py
COPY tools/orchestrator/worker.py /app/worker.py

# curl_cffi 0.16 在 Python 3.10 需要 typing_extensions（typing.Unpack 3.11+ 才有）。
# 单独成层并置于 COPY 之后：复用上方 pip/apt/chrome 缓存层，加速重建。
RUN pip install --no-cache-dir typing_extensions

# 设置 Chrome 路径（DrissionPage 默认能发现系统 chrome）
ENV SPACEFIN_CHROME_BIN=/usr/bin/google-chrome
# Chrome 无沙箱（容器内必需）
ENV SPACEFIN_CHROME_ARGS=--no-sandbox

# 启动包装（B1 并行化 fangyuan）：每 worker 启动时从只读源拷贝私有 chrome_profile 副本，
# 避免 5 个 Chrome 争抢同一 user-data-dir（SingletonLock）。master 无 WORKER_ID 则直接启动。
COPY tools/orchestrator/start.sh /app/start.sh
RUN chmod +x /app/start.sh

# 覆盖入口：默认无命令，由 docker-compose 指定
ENTRYPOINT ["/app/start.sh"]
CMD ["worker.py"]
