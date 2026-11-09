"""AppHub — 容器内 nginx 配置生成（v1.0.22 起只托管面板）。

域名方案已废弃：不再生成 app-xx.<域名> 虚拟主机，也不按 Host 反向代理任何应用。
nginx 仅对外监听 APPHUB_PORT，把请求全部转给内部 127.0.0.1:19080（AppHub 面板），
应用本身一律由浏览器直接访问 http://<局域网IP>:端口。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

from . import db

log = logging.getLogger("apphub.nginx")

PUBLIC_PORT = (os.environ.get("APPHUB_PORT", "9080") or "9080").strip()
INTERNAL_PORT = (os.environ.get("APPHUB_INTERNAL_PORT", "19080") or "19080").strip()
CONF_PATH = Path(os.environ.get("APPHUB_NGINX_CONF", "/etc/nginx/conf.d/apphub.conf"))
GENERATED_AT = "# 由 AppHub 自动生成，请勿手工修改"


def enabled() -> bool:
    """是否由 nginx 接管对外端口（关闭时退回 uvicorn 直连旧模式）。"""
    if os.environ.get("APPHUB_NGINX", "1").strip() in ("0", "false", "no", "off"):
        return False
    return bool(shutil.which("nginx"))


def render(apps: Optional[Iterable[dict[str, Any]]] = None) -> str:
    """面板托管配置：不区分 Host，全部交给面板；应用访问不经过这里。"""
    return f"""{GENERATED_AT}

map $http_upgrade $apphub_upgrade {{
    default upgrade;
    ''      close;
}}

# 面板入口：http://<NAS-IP>:{PUBLIC_PORT}
# 应用一律 IP 直连（http://<NAS-IP>:应用端口），不在此处反代
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
"""


def write(apps: Optional[Iterable[dict[str, Any]]] = None) -> str:
    """把配置写到 conf.d；返回写入的文本（失败抛异常）。"""
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
    log.info("nginx 配置已重载：%s", CONF_PATH)
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
