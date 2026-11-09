"""AppHub — 容器内 nginx 虚拟主机配置生成。

思路：容器里跑一个 nginx 对外监听 APPHUB_PORT，FastAPI 退到 127.0.0.1 内部端口，
由 nginx 按 Host 头做映射：

    app-12.fnos.local:9080  ->  127.0.0.1:8080（该应用真实端口）
    其它 / fnos.local       ->  127.0.0.1:19080（AppHub 面板）

解析交给客户端 hosts（面板「复制 hosts」按钮给出完整片段），不再依赖 mDNS。
配置由后端在应用增删改后自动重写并 reload，无需手工维护。
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from . import db

log = logging.getLogger("apphub.nginx")

PUBLIC_PORT = (os.environ.get("APPHUB_PORT", "9080") or "9080").strip()
INTERNAL_PORT = (os.environ.get("APPHUB_INTERNAL_PORT", "19080") or "19080").strip()
ALIAS_HOST = (os.environ.get("APPHUB_ALIAS_HOST", "fnos.local").strip().lower() or "fnos.local")
WEB_SUFFIX = (os.environ.get("APPHUB_WEB_SUFFIX", ALIAS_HOST).strip(".").lower() or ALIAS_HOST)
CONF_PATH = Path(os.environ.get("APPHUB_NGINX_CONF", "/etc/nginx/conf.d/apphub.conf"))
GENERATED_AT = "# 由 AppHub 自动生成，请勿手工修改（应用增删改会自动重写）"

# 这些主机名在容器内不可解析，统一走本机回环（compose 使用 host 网络，应用端口即宿主端口）
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0", ALIAS_HOST, WEB_SUFFIX, socket.gethostname().lower()}


def enabled() -> bool:
    """是否由 nginx 接管对外端口（关闭时退回 uvicorn 直连旧模式）。"""
    if os.environ.get("APPHUB_NGINX", "1").strip() in ("0", "false", "no", "off"):
        return False
    return bool(shutil.which("nginx"))


def _upstream(row: dict[str, Any]) -> tuple[str, str]:
    """从应用记录推导 nginx 上游：返回 (scheme, host:port)；不需要反代返回空。"""
    if (row.get("mode") or "proxy") != "proxy":
        return "", ""
    raw = (row.get("url") or "").strip()
    port = str(row.get("port") or "").strip()
    scheme = "http"
    host = ""
    if raw:
        parsed = urlparse(raw if "://" in raw else "http://" + raw)
        scheme = (parsed.scheme or "http").lower()
        host = (parsed.hostname or "").lower()
        if not port:
            try:
                port = str(parsed.port or "")
            except ValueError:
                port = ""
    if scheme not in ("http", "https"):
        scheme = "http"
    if host in _LOCAL_HOSTS:
        host = "127.0.0.1"
    if not port:
        port = "443" if scheme == "https" else "80"
    return scheme, f"{host}:{port}"


def _server_block(name: str, target: str, scheme: str) -> str:
    """应用虚拟主机：根路径原样反代，保留 WebSocket 与大文件上传。"""
    ssl = ""
    if scheme == "https":
        ssl = "\n        proxy_ssl_server_name on;\n        proxy_ssl_verify off;"
    return f"""
server {{
    listen {PUBLIC_PORT};
    server_name {name};

    # 应用可能有自己的大文件上传（备份、媒体库）
    client_max_body_size 0;

    location / {{
        proxy_pass {scheme}://{target};
        proxy_http_version 1.1;
        # 与旧 Python 代理保持一致：Host 用上游自身，原始 Host 放 X-Forwarded-Host
        proxy_set_header Host $proxy_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Prefix "";
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $apphub_upgrade;
        # 上游若返回自身绝对地址跳转，改回子域地址，避免浏览器跳出面板
        proxy_redirect ~^{scheme}://{target}(.*)$ $scheme://$host$1;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;{ssl}
    }}
}}
"""


def render(apps: Iterable[dict[str, Any]]) -> str:
    """生成完整 conf.d 片段。"""
    blocks = [
        f"""{GENERATED_AT}
# 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}

map $http_upgrade $apphub_upgrade {{
    default upgrade;
    ''      close;
}}
""",
        f"""
# 兜底：面板本身（fnos.local、NAS IP、未知子域都交给面板判断）
server {{
    listen {PUBLIC_PORT} default_server;
    server_name _;

    client_max_body_size 0;

    location / {{
        proxy_pass http://127.0.0.1:{INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $apphub_upgrade;
        # /api/logs 与 /api/apps/{id}/icon 都是流式/长连接，禁止缓冲
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }}
}}
""",
    ]
    seen: set[str] = set()
    for row in apps:
        slug = (row.get("slug") or f"app-{row.get('id')}").strip().lower()
        if not slug or slug in seen:
            continue
        scheme, target = _upstream(row)
        if not target:
            continue
        seen.add(slug)
        name = f"{slug}.{WEB_SUFFIX}"
        blocks.append(_server_block(name, target, scheme))
    return "".join(blocks)


def write(apps: Optional[Iterable[dict[str, Any]]] = None) -> str:
    """把配置写到 conf.d；返回写入的文本（失败抛异常）。"""
    if apps is None:
        apps = db.list_apps()
    text = render(apps)
    CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF_PATH.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(CONF_PATH)
    return text


def test() -> tuple[bool, str]:
    """nginx -t 校验；返回 (ok, 输出)。"""
    try:
        proc = subprocess.run(
            ["nginx", "-t"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()


def reload() -> bool:
    """重载 nginx；未启用或失败都只记日志，不影响面板本身。"""
    if not enabled():
        return False
    try:
        proc = subprocess.run(
            ["nginx", "-s", "reload"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("nginx reload 失败：%s", exc)
        return False
    if proc.returncode != 0:
        log.warning("nginx reload 返回 %s：%s", proc.returncode, (proc.stderr or "").strip())
        return False
    log.info("nginx 配置已重载，Host 映射配置：%s", CONF_PATH)
    return True


def apply() -> bool:
    """应用增删改后调用：重写配置 + reload。"""
    if not enabled():
        return False
    try:
        write()
    except OSError as exc:
        log.warning("写入 nginx 配置失败：%s", exc)
        return False
    ok, output = test()
    if not ok:
        log.warning("生成的 nginx 配置未通过校验，保持旧配置运行：%s", output)
        return False
    return reload()


def _cli() -> int:
    """启动脚本用：只写配置（nginx 还没起来，不能 reload）。"""
    try:
        if enabled():
            db.init_db()
            write()
            ok, output = test()
            if not ok:
                log.warning("nginx 配置校验失败：%s", output)
                return 1
            log.info("nginx 配置已生成：%s", CONF_PATH)
        return 0
    except Exception as exc:  # noqa: BLE001 启动期不允许抛错
        log.warning("生成 nginx 配置异常：%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[apphub] %(message)s")
    raise SystemExit(_cli())
