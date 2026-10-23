# -*- coding: utf-8 -*-
"""启动入口：python run.py

监听地址 / 端口由 ADAPTER_HOST / ADAPTER_PORT 控制。
默认 127.0.0.1:8123（仅本机）。容器内或需要局域网访问时设 ADAPTER_HOST=0.0.0.0。

⚠️ proxy_headers 必须显式关掉
------------------------------
`uvicorn[standard]` 的 proxy_headers **默认是 True**，它会用
`X-Forwarded-For` 覆盖 `request.client.host`。后果是：

    ADAPTER_ALLOW_IPS=203.0.113.9   # 以为只放行某个反代
    curl -H "X-Forwarded-For: 203.0.113.9" ...   # ← 直接绕过白名单

这是实测出来的（scripts/check_proxy_ip.py 的第④ 例会打印"危险"）：
白名单填 127.0.0.1 时伪造 XFF 就能进来，等于白名单形同虚设。

所以这里写死 proxy_headers=False。需要「按真实客户端 IP 限流」的场景，
正确做法是在nginx 层限流，而不是让适配器信任一个可伪造的头。
"""
import os

from app.config import HOST, PORT

import uvicorn

if __name__ == "__main__":
    assert not os.getenv("ADAPTER_TRUST_PROXY_HEADERS"), (
        "ADAPTER_TRUST_PROXY_HEADERS 已废弃：信任 X-Forwarded-For 会让"
        "任何人都能伪造一个头绕过 ADAPTER_ALLOW_IPS。需要按真实 IP 限流"
        "请在反代层做。")
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        proxy_headers=False,      # 不要信任 X-Forwarded-For（见上）
        server_header=False,      # 不暴露 uvicorn 版本，减少信息泄露
    )