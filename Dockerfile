FROM python:3.12-slim

WORKDIR /app

# 依赖单独一层，改代码不触发重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 非 root 运行：容器被拿下时，攻击者拿到的不是宿主机友好的 root 身份
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin adapter

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
