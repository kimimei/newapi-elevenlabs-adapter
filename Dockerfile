FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层，改代码不触发重装
COPY requirements.txt .

# 构建期 pip 源可覆盖。境外默认 PyPI，国内网络首次构建可能卡十几分钟：
#   PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple docker compose up -d --build
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 非 root 运行：容器被拿下时，攻击者拿到的不是宿主机友好的 root 身份
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin adapter

# 账本目录必须存在且归 adapter 所有。
# 缺失时写账本会抛 PermissionError，只打一条 warning 就被吞掉，
# 表现是服务一切正常、/v1/usage 永远是 0。
RUN mkdir -p /app/logs/billing && chown -R adapter:adapter /app/logs

COPY --chown=adapter:adapter app ./app
COPY --chown=adapter:adapter static ./static
COPY --chown=adapter:adapter run.py .

ENV ADAPTER_HOST=0.0.0.0 \
    ADAPTER_PORT=8123 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8123

USER adapter

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8123/healthz',timeout=3).status==200 else 1)"

CMD ["python", "run.py"]
