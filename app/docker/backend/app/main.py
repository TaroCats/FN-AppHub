"""AppHub — 飞牛应用聚合中心 后端入口。

内嵌访问采用「子域反代」：每个应用独占 app-xx.fnos.local:9080，
根路径原样转发到目标应用，避免 /p/{id}/ 前缀模式在绝对路径应用上 404。
子域由内置 mDNS 响应器在局域网内解析（*.fnos.local -> NAS IP）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator

from . import db, logbuf, webconf
from . import mdns as mdns_mod
from .docker_scan import probe as probe_url
from .docker_scan import normalize_host
from .docker_scan import scan as docker_scan
from .proxy import proxy_http, proxy_websocket

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(os.environ.get("APPHUB_STATIC", "/app/static"))
SELF_PORT = os.environ.get("APPHUB_PORT", "9080")

# 统一配置应用的固定主机名：应用地址只保存 http://fnos.local:端口，
# 真实 IP 通过 /etc/hosts 映射 + 内置 mDNS，换 IP 时只改映射，不动任何应用地址。
ALIAS_HOST = (os.environ.get("APPHUB_ALIAS_HOST", "fnos.local").strip().lower() or "fnos.local")
# 子域后缀：app-12.<WEB_SUFFIX>:<SELF_PORT>
WEB_SUFFIX = (os.environ.get("APPHUB_WEB_SUFFIX", ALIAS_HOST).strip(".").lower() or ALIAS_HOST)
HOSTS_FILE = Path(os.environ.get("APPHUB_HOSTS_FILE", "/etc/hosts"))
HOSTS_MARK = "# apphub-managed"

logbuf.setup(db.DATA_DIR, os.environ.get("APPHUB_LOG_LEVEL", "INFO"))
log = logging.getLogger("apphub")

DEBUG = False
MDNS: Optional[mdns_mod.MDNSResponder] = None

app = FastAPI(title="AppHub", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- 访问日志


@app.middleware("http")
async def _access_log(request: Request, call_next):  # noqa: ANN001
    """记录面板请求耗时；调试模式下附带转发目标等细节。"""
    path = request.url.path
    if path == "/api/logs" or path.startswith("/api/logs?"):
        return await call_next(request)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("请求异常 %s %s", request.method, path)
        raise
    cost = (time.perf_counter() - started) * 1000
    if DEBUG:
        log.debug(
            "%s %s -> %s %.1fms host=%s ua=%s",
            request.method, path, response.status_code, cost,
            request.headers.get("host", "-"), request.headers.get("user-agent", "-")[:60],
        )
    elif not path.startswith("/api/apps/") or not path.endswith("/icon"):
        log.info("%s %s -> %s %.1fms", request.method, path, response.status_code, cost)
    response.headers["X-AppHub-Cost"] = f"{cost:.1f}ms"
    return response


def _to_alias_url(url: str, port: str = "") -> str:
    """把地址收敛成统一的别名地址：协议沿用原地址，主机固定为 ALIAS_HOST。"""
    raw = (url or "").strip()
    scheme = "http"
    real_port = str(port or "").strip()
    if raw:
        parsed = urlparse(raw if "://" in raw else "http://" + raw)
        scheme = parsed.scheme or "http"
        if not real_port:
            try:
                real_port = str(parsed.port or "")
            except ValueError:
                real_port = ""
    target = f"{ALIAS_HOST}:{real_port}" if real_port else ALIAS_HOST
    return f"{scheme}://{target}"


def sync_alias_mapping(host: str) -> str:
    """把 ALIAS_HOST 指向 host，写入 /etc/hosts（只维护带标记的行）。"""
    host = normalize_host(host or "")
    lines: list[str] = []
    try:
        if HOSTS_FILE.exists():
            lines = HOSTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        log.warning("读取 %s 失败：%s", HOSTS_FILE, exc)
    kept = [line for line in lines if HOSTS_MARK not in line]
    if host:
        kept.append(f"{host}\t{ALIAS_HOST}\t{HOSTS_MARK}")
    try:
        HOSTS_FILE.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
        log.info("%s 映射已更新：%s -> %s", ALIAS_HOST, host or "(未设置)", HOSTS_FILE)
    except OSError as exc:
        log.warning(
            "写入 %s 失败（%s）：%s 无法在容器内解析，请在宿主机 hosts 中添加映射",
            HOSTS_FILE, exc, ALIAS_HOST,
        )
    return host


def _migrate_auto_urls() -> list[dict[str, Any]]:
    """统一配置模式的历史地址收敛为别名地址，保证换 IP 时无需改应用。"""
    changed: list[dict[str, Any]] = []
    for item in db.list_apps():
        if (item.get("url_mode") or "auto") != "auto":
            continue
        new_url = _to_alias_url(item.get("url", ""), item.get("port", ""))
        if new_url and new_url != item.get("url"):
            db.update_app(item["id"], {"url": new_url})
            changed.append({"id": item["id"], "name": item["name"], "url": new_url})
    return changed


def _apply_base_host(host: str) -> str:
    """保存映射 IP：写库 + 更新 hosts，并收敛统一配置模式的应用地址。"""
    host = normalize_host(host or "")
    if host:
        db.set_setting("base_host", host)
        sync_alias_mapping(host)
        _migrate_auto_urls()
        # 映射 IP 变了就立刻通告，客户端不必等旧记录 TTL 过期
        if MDNS is not None:
            MDNS.announce()
            log.info("访问地址已更新为 %s，mDNS 应答 IP %s", host, alias_ip() or "未获取")
    webconf.apply()
    return host


# ---------------------------------------------------------------- 子域与调试辅助

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def _public_port() -> str:
    return "" if SELF_PORT in ("", "80") else f":{SELF_PORT}"


def frame_origin(slug: str) -> str:
    """子域访问源：http://app-12.fnos.local:9080"""
    return f"http://{slug}.{WEB_SUFFIX}{_public_port()}"


def frame_url(slug: str, home_path: str = "") -> str:
    path = (home_path or "").strip()
    if not path or path == "/":
        path = "/"
    elif not path.startswith("/"):
        path = "/" + path
    return f"{frame_origin(slug)}{path}"


def ensure_slug(app_row: dict[str, Any]) -> str:
    """保证每个应用有唯一子域标签：默认 app-<id>，可自定义。"""
    current = (app_row.get("slug") or "").strip().lower()
    if current and SLUG_RE.match(current):
        return current
    slug = f"app-{app_row['id']}"
    suffix = 1
    while db.slug_taken(slug, exclude_id=app_row["id"]):
        suffix += 1
        slug = f"app-{app_row['id']}-{suffix}"
    db.update_app(app_row["id"], {"slug": slug})
    app_row["slug"] = slug
    log.info("应用 %s 分配子域 %s", app_row.get("name"), slug)
    return slug


def frame_path(app_id: int, home_path: str = "") -> str:
    """内嵌加载用的同源路径：/p/<id>/...，不依赖任何 DNS/hosts 解析。"""
    path = (home_path or "").strip()
    if not path or path == "/":
        path = "/"
    elif not path.startswith("/"):
        path = "/" + path
    return f"/p/{app_id}{path}"


def decorate(app_row: dict[str, Any]) -> dict[str, Any]:
    """给应用补齐子域字段与同源内嵌路径。"""
    slug = ensure_slug(app_row)
    app_row["slug"] = slug
    app_row["frame_origin"] = frame_origin(slug)
    app_row["frame_url"] = frame_url(slug, app_row.get("home_path") or "")
    # 内嵌窗口统一走同源 /p/ 反代，避免客户端没配 hosts 时 iframe 落到错误页
    app_row["frame_path"] = frame_path(app_row["id"], app_row.get("home_path") or "")
    return app_row


def decorate_all(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [decorate(item) for item in items]


def resolve_subdomain(host: str) -> tuple[Optional[dict[str, Any]], str]:
    """解析 Host：返回 (应用, 标签)；非子域返回 (None, "")。"""
    host = (host or "").split(":")[0].strip().lower().rstrip(".")
    if not host or not host.endswith("." + WEB_SUFFIX):
        return None, ""
    label = host[: -(len(WEB_SUFFIX) + 1)]
    if not label or "." in label:
        return None, ""
    return db.find_by_slug(label), label


def local_ip() -> str:
    """探测局域网 IP，用于 mDNS 应答。

    不用「连外网看出口地址」的原因：装有 Clash/Tailscale 类工具时，
    那样拿到的是 198.18.x 或 100.x 的虚拟地址，局域网设备无法访问。
    """
    best, best_score = "", -2
    for _, ip in mdns_mod.local_ipv4_addrs():
        if ip.startswith(("198.18.", "198.19.", "169.254.")):
            score = -1
        elif ip.startswith("192.168."):
            score = 4
        elif ip.startswith("10."):
            score = 3
        elif ip.startswith("172."):
            score = 2  # 可能是 docker 网桥，优先级较低
        elif ip.startswith("100."):
            score = 1  # Tailscale/CGNAT
        else:
            score = 1
        if score > best_score:
            best, best_score = ip, score
    if best_score > 0:
        return best
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.4)
            sock.connect(("223.5.5.5", 80))
            ip = sock.getsockname()[0]
            return "" if ip.startswith(("198.18.", "198.19.")) else ip
    except OSError:
        return ""


_WARNED_HOST = ""


def _as_ipv4(value: str) -> str:
    """把文本收敛成可用作 mDNS 应答的 IPv4；不可用返回空串。"""
    text = (value or "").strip().strip("[]")
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return ""
    if parsed.version != 4 or parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
        return ""
    return str(parsed)


def alias_ip() -> str:
    """mDNS 应答使用的 IP：优先「访问地址」里保存的映射 IP。

    回环地址对局域网设备无意义；填写了域名/IPv6/非法值时同样不能直接拿来应答
    （以前这里只排除 127.*，一旦存的是域名就会静默不应答），统一回退到探测到的局域网 IP。
    """
    global _WARNED_HOST
    saved = normalize_host(db.get_setting("base_host", ""))
    if saved:
        ip = _as_ipv4(saved)
        if ip:
            _WARNED_HOST = ""
            return ip
        if saved != _WARNED_HOST:
            _WARNED_HOST = saved
            log.warning(
                "访问地址「%s」不是可用的 IPv4，mDNS 改用自动探测的局域网 IP 应答；"
                "请把「访问地址」填写为局域网 IP（如 192.168.1.10）",
                saved,
            )
    return local_ip()


def mdns_snapshot() -> dict[str, Any]:
    """调试面板用的 mDNS 状态；running/expect_ip 便于判断「域名是否映射到指定 IP」。"""
    saved = normalize_host(db.get_setting("base_host", ""))
    expect = alias_ip()
    snap: dict[str, Any] = {
        "running": MDNS is not None,
        "hits": MDNS.hits if MDNS else 0,
        "ip": expect,
        "suffix": WEB_SUFFIX,
        "alias": ALIAS_HOST,
        "mapped": saved,
        "source": "访问地址映射 IP" if saved and _as_ipv4(saved) else "自动探测局域网 IP",
        "apex": True,
    }
    if MDNS:
        snap.update(MDNS.status())
    return snap


def hosts_snippet() -> str:
    ip = alias_ip() or "<NAS-IP>"
    # 裸域名（fnos.local）也要列出来：部分客户端不做 mDNS 通配，直接访问面板时会用到
    names = [WEB_SUFFIX] if WEB_SUFFIX == ALIAS_HOST else [ALIAS_HOST, WEB_SUFFIX]
    lines = [f"{ip}\t{name}\t# AppHub 面板" for name in names]
    for item in db.list_apps():
        slug = ensure_slug(item)
        lines.append(f"{ip}\t{slug}.{WEB_SUFFIX}\t# {item['name']}")
    return "\n".join(lines)


def apply_debug(enabled: bool) -> None:
    global DEBUG
    DEBUG = bool(enabled)
    logbuf.set_level("DEBUG" if DEBUG else os.environ.get("APPHUB_LOG_LEVEL", "INFO"))
    db.set_setting("debug", "1" if DEBUG else "0")
    log.info("调试模式已%s", "开启" if DEBUG else "关闭")


# ---------------------------------------------------------------- 数据模型

class AppPayload(BaseModel):
    name: str
    url: str = ""
    url_mode: str = "auto"   # auto=统一配置（主机走全局，仅填端口）；custom=自定义完整地址
    port: str = ""
    home_path: str = ""       # 主页路径（选填），如 /admin
    group_name: str = "未分组"
    note: str = ""
    open_mode: str = "inline"
    mode: str = "proxy"
    color: str = ""
    container: str = ""
    slug: str = ""            # 子域标签，如 app-12（留空自动分配）

    @field_validator("url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if not re.match(r"^https?://", value, flags=re.I):
            value = "http://" + value
        return value.rstrip("/")

    @field_validator("url_mode")
    @classmethod
    def _normalize_url_mode(cls, value: str) -> str:
        return value if value in ("auto", "custom") else "auto"

    @field_validator("port")
    @classmethod
    def _normalize_port(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if not value.isdigit() or not (0 < int(value) < 65536):
            raise ValueError("端口需为 1-65535 之间的数字")
        return str(int(value))

    @field_validator("home_path")
    @classmethod
    def _normalize_home_path(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        value = re.sub(r"^https?://[^/]+", "", value, flags=re.I)
        if not value.startswith("/"):
            value = "/" + value
        return value.rstrip("/")

    @model_validator(mode="after")
    def _fill_url(self) -> "AppPayload":
        """统一配置模式的地址固定为「协议://fnos.local:端口」，真实 IP 靠 hosts 映射。"""
        if self.url_mode == "auto":
            if not self.url and not self.port:
                raise ValueError("统一配置模式下需要填写端口")
            self.url = _to_alias_url(self.url, self.port)
            if not self.port:
                try:
                    parsed = urlparse(self.url)
                    self.port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
                except ValueError:
                    self.port = ""
        elif not self.url:
            raise ValueError("请填写应用地址")
        return self

    @field_validator("slug")
    @classmethod
    def _normalize_slug(cls, value: str) -> str:
        """子域标签只允许小写字母、数字与中划线。"""
        value = (value or "").strip().lower()
        if not value:
            return ""
        if not SLUG_RE.match(value):
            raise ValueError("子域只能是 1-32 位小写字母、数字或中划线")
        return value

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("名称不能为空")
        return value[:60]

    @field_validator("mode")
    @classmethod
    def _normalize_mode(cls, value: str) -> str:
        """内嵌模式：proxy 走 AppHub 反向代理，direct 直接用原始地址。"""
        return value if value in ("proxy", "direct") else "proxy"

    @field_validator("open_mode")
    @classmethod
    def _normalize_open_mode(cls, value: str) -> str:
        """打开方式：inline 窗口内打开（可再选内嵌模式），window 新窗口打开。"""
        return value if value in ("inline", "window") else "inline"


class ImportItem(BaseModel):
    name: str
    url: str
    group_name: str = "Docker"
    color: str = ""
    container: str = ""


class ImportPayload(BaseModel):
    items: list[ImportItem]


class ReorderPayload(BaseModel):
    ids: list[int]


class ProbePayload(BaseModel):
    ids: list[int]


class GroupPayload(BaseModel):
    name: str


class GroupRenamePayload(BaseModel):
    old: str
    new: str


class GroupReorderPayload(BaseModel):
    names: list[str]


# ---------------------------------------------------------------- 启动

def _mdns_enabled() -> bool:
    """mDNS 已不再是必需链路（nginx + hosts 足够），默认关闭，可显式开启。"""
    return os.environ.get("APPHUB_MDNS", "0").strip().lower() in ("1", "true", "yes", "on")


@app.on_event("startup")
async def _startup() -> None:
    global MDNS
    db.init_db()
    # 容器重建后 /etc/hosts 会重置，这里按库中记录的映射 IP 重新写入
    sync_alias_mapping(db.get_setting("base_host", ""))
    _migrate_auto_urls()
    apply_debug(db.get_setting("debug", "") == "1")
    for item in db.list_apps():
        decorate(item)
    # Host 映射由容器内 nginx 承担：应用增删改后自动重写 conf 并 reload
    webconf.apply()
    if _mdns_enabled():
        try:
            # 可选：仍公布裸域名与统一后缀，方便不做 hosts 的客户端
            MDNS = await mdns_mod.start([WEB_SUFFIX, ALIAS_HOST], alias_ip)
            log.info(
                "mDNS 应答 %s / *.%s -> %s（来源：%s）",
                ALIAS_HOST, WEB_SUFFIX, alias_ip() or "未获取到局域网 IP",
                "访问地址映射 IP" if _as_ipv4(db.get_setting("base_host", "")) else "自动探测局域网 IP",
            )
            MDNS.announce()
        except Exception as exc:  # mDNS 只是便捷解析，失败不影响主功能
            log.warning("mDNS 未启动：%s", exc)
    else:
        log.info(
            "mDNS 已关闭：子域解析请用面板「复制 hosts」，路由由容器内 nginx 按 Host 完成（%s）",
            webconf.CONF_PATH,
        )
    asyncio.create_task(_first_run_import())


async def _first_run_import() -> None:
    """首次启动时把已暴露端口的容器自动收录，省去手工添加。"""
    if db.get_setting("auto_import", "") == "done":
        return
    if db.list_apps():
        db.set_setting("auto_import", "done")
        return
    await asyncio.sleep(2)
    try:
        candidates = await docker_scan(
            include_internal=False,
            host=normalize_host(db.get_setting("base_host", "")) or "127.0.0.1",
        )
    except Exception as exc:  # docker 不可用时静默跳过
        log.warning("首次自动收录跳过：%s", exc)
        return
    imported = 0
    for item in candidates:
        if not item.get("online"):
            continue
        if str(item["port"]) == SELF_PORT:
            continue
        alias_url = _to_alias_url(item["url"], str(item.get("port") or ""))
        if db.find_by_url(alias_url) or db.find_by_url(item["url"]):
            continue
        db.create_app(
            {
                "name": item["suggest_name"],
                "url": alias_url,
                "url_mode": "auto",
                "port": str(item.get("port") or ""),
                "group_name": item["suggest_group"],
                "color": item["suggest_color"],
                "container": item["container"],
                "open_mode": "inline",
                "mode": "proxy",
            }
        )
        imported += 1
        if imported >= 24:
            break
    db.set_setting("auto_import", "done")
    if imported:
        webconf.apply()
    log.info("首次自动收录 %s 个应用", imported)


# ---------------------------------------------------------------- 应用列表

@app.get("/api/apps")
async def api_list_apps() -> dict[str, Any]:
    apps = decorate_all(db.list_apps())
    groups = ["全部"] + db.distinct_groups()
    return {"apps": apps, "groups": groups, "web_suffix": WEB_SUFFIX, "self_port": SELF_PORT}


@app.post("/api/apps")
async def api_create_app(payload: AppPayload) -> dict[str, Any]:
    existing = db.find_by_url(payload.url)
    if existing:
        raise HTTPException(status_code=409, detail=f"该地址已存在：{existing['name']}")
    data = payload.model_dump()
    if data.get("slug") and db.slug_taken(data["slug"]):
        raise HTTPException(status_code=409, detail=f"子域已被占用：{data['slug']}")
    row = decorate(db.create_app(data))
    webconf.apply()
    return row


@app.put("/api/apps/{app_id}")
async def api_update_app(app_id: int, payload: AppPayload) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    if data.get("slug") and db.slug_taken(data["slug"], exclude_id=app_id):
        raise HTTPException(status_code=409, detail=f"子域已被占用：{data['slug']}")
    updated = db.update_app(app_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="应用不存在")
    webconf.apply()
    return decorate(updated)


@app.delete("/api/apps/{app_id}")
async def api_delete_app(app_id: int) -> dict[str, bool]:
    if not db.delete_app(app_id):
        raise HTTPException(status_code=404, detail="应用不存在")
    icon = db.ICON_DIR / f"{app_id}.png"
    if icon.exists():
        try:
            icon.unlink()
        except OSError:
            pass
    webconf.apply()
    return {"ok": True}


# ---------------------------------------------------------------- 分组管理

@app.get("/api/groups")
async def api_list_groups() -> dict[str, Any]:
    return {"groups": db.list_groups()}


@app.post("/api/groups")
async def api_create_group(payload: GroupPayload) -> dict[str, Any]:
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    return {"groups": db.create_group(name[:30])}


@app.post("/api/groups/rename")
async def api_rename_group(payload: GroupRenamePayload) -> dict[str, Any]:
    old = (payload.old or "").strip()
    new = (payload.new or "").strip()[:30]
    if not old or not new:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    if old == "未分组":
        raise HTTPException(status_code=400, detail="「未分组」不可重命名")
    affected = db.rename_group(old, new)
    return {"ok": True, "affected": affected, "groups": db.list_groups()}


@app.post("/api/groups/delete")
async def api_delete_group(payload: GroupPayload) -> dict[str, Any]:
    name = (payload.name or "").strip()
    if name == "未分组":
        raise HTTPException(status_code=400, detail="「未分组」不可删除")
    affected = db.delete_group(name)
    return {"ok": True, "affected": affected, "groups": db.list_groups()}


@app.post("/api/groups/reorder")
async def api_reorder_groups(payload: GroupReorderPayload) -> dict[str, Any]:
    db.reorder_groups(payload.names)
    return {"ok": True, "groups": db.list_groups()}


@app.post("/api/apps/reorder")
async def api_reorder(payload: ReorderPayload) -> dict[str, bool]:
    db.reorder(payload.ids)
    return {"ok": True}


# ---------------------------------------------------------------- 访问地址切换

_LOOPBACK = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"}


def _url_hostname(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _rewrite_url_host(url: str, host: str) -> str:
    """把地址中的主机部分替换为 host，保留协议、端口、路径与查询。"""
    raw = (url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw if "://" in raw else "http://" + raw)
    target = host
    if ":" in target and not target.startswith("["):
        target = f"[{target}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{target}:{port}" if port else target
    if parsed.username:
        auth = parsed.username + (f":{parsed.password}" if parsed.password else "")
        netloc = f"{auth}@{netloc}"
    path = "" if parsed.path in ("", "/") else parsed.path
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.scheme}://{netloc}{path}{query}{fragment}"


class RebindPayload(BaseModel):
    host: str
    only_loopback: bool = True
    app_ids: list[int] = []


@app.post("/api/apps/rebind-host")
async def api_rebind_host(payload: RebindPayload) -> dict[str, Any]:
    """保存 fnos.local 的映射 IP：统一配置的应用地址保持 fnos.local:端口 不变。"""
    host = _apply_base_host(payload.host)
    if not host:
        raise HTTPException(status_code=400, detail="访问地址无效")

    auto_apps = [item for item in db.list_apps() if (item.get("url_mode") or "auto") == "auto"]
    return {
        "ok": True,
        "host": host,
        "alias": ALIAS_HOST,
        "mdns": mdns_snapshot(),
        "updated": [{"id": a["id"], "name": a["name"], "url": a["url"]} for a in auto_apps],
        "skipped": 0,
        "conflicts": [],
    }


# ---------------------------------------------------------------- Docker 扫描

def _request_host(request: Request) -> str:
    """取用户访问飞牛网页时用的主机名，用于拼出可直接点开的地址。"""
    for header in ("x-forwarded-host", "x-real-host", "host"):
        host = normalize_host(request.headers.get(header, ""))
        if host:
            return host
    return ""


class HostSetting(BaseModel):
    host: str


@app.post("/api/settings/base-host")
async def api_set_base_host(payload: HostSetting) -> dict[str, Any]:
    """前端上报映射 IP：写库并更新 fnos.local 的 hosts 映射。"""
    host = _apply_base_host(payload.host)
    warning = ""
    if host and not _as_ipv4(host):
        warning = f"「{host}」不是可用的 IPv4，mDNS 将改用自动探测的局域网 IP 应答"
    return {"ok": True, "host": host, "alias": ALIAS_HOST, "mdns": mdns_snapshot(), "warning": warning}


@app.get("/api/settings/base-host")
async def api_get_base_host(request: Request) -> dict[str, Any]:
    host = normalize_host(db.get_setting("base_host", "")) or _request_host(request)
    return {"host": host}


@app.get("/api/scan")
async def api_scan(
    request: Request,
    include_internal: bool = False,
    all_ports: bool = False,
    host: str = "",
) -> dict[str, Any]:
    # 优先级：本次指定 > 已记录的默认主机 > 当前请求 Host > 127.0.0.1
    base_host = (
        normalize_host(host)
        or normalize_host(db.get_setting("base_host", ""))
        or _request_host(request)
        or "127.0.0.1"
    )
    try:
        candidates = await docker_scan(
            include_internal=include_internal, all_ports=all_ports, host=base_host
        )
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="未找到 docker.sock，请在应用权限中开启 Docker 访问")
    except PermissionError:
        raise HTTPException(status_code=503, detail="无权限访问 docker.sock，请检查容器挂载权限")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"读取 Docker 失败：{exc}")

    apps = db.list_apps()
    existing = {a["url"] for a in apps}
    existing |= {_to_alias_url(a["url"], a.get("port", "")) for a in apps}
    for item in candidates:
        item.pop("probe_url", None)
        item["added"] = item["url"] in existing or _to_alias_url(item["url"], item["port"]) in existing
    return {"items": candidates, "count": len(candidates), "host": base_host}


@app.post("/api/import")
async def api_import(payload: ImportPayload) -> dict[str, Any]:
    created = 0
    skipped = 0
    for item in payload.items:
        port = str(urlparse(item.url if "://" in item.url else "http://" + item.url).port or "")
        url = _to_alias_url(item.url, port)
        if db.find_by_url(url) or db.find_by_url(item.url.rstrip("/")):
            skipped += 1
            continue
        db.create_app(
            {
                "name": item.name,
                "url": url,
                "url_mode": "auto",
                "port": port,
                "group_name": item.group_name or "Docker",
                "color": item.color,
                "container": item.container,
                "open_mode": "inline",
                "mode": "proxy",
            }
        )
        created += 1
    if created:
        webconf.apply()
    return {"created": created, "skipped": skipped}


@app.post("/api/probe")
async def api_probe(payload: ProbePayload) -> dict[str, Any]:
    apps = {a["id"]: a for a in db.list_apps()}
    targets = [(i, apps[i]) for i in payload.ids if i in apps]
    results = await asyncio.gather(*[probe_url(a["url"]) for _, a in targets], return_exceptions=True)
    return {"status": {str(app_id): bool(ok) for (app_id, _), ok in zip(targets, results)}}


# ---------------------------------------------------------------- 图标

async def _fetch_icon(app_row: dict[str, Any]) -> Optional[bytes]:
    base = app_row["url"].rstrip("/")
    headers = {"User-Agent": "Mozilla/5.0 AppHub/1.0"}
    async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=6) as client:
        candidates = [base + "/favicon.ico"]
        try:
            resp = await client.get(base + "/", headers=headers)
            if resp.status_code < 400 and "html" in resp.headers.get("content-type", ""):
                html = resp.text[:200000]
                for match in re.finditer(
                    r"<link[^>]+rel=[\"']?(?:shortcut icon|icon|apple-touch-icon)[\"']?[^>]*>",
                    html,
                    flags=re.I,
                ):
                    tag = match.group(0)
                    href = re.search(r"href=[\"']([^\"']+)[\"']", tag, flags=re.I)
                    if href:
                        value = href.group(1)
                        if value.startswith("//"):
                            value = urlparse(base).scheme + ":" + value
                        elif value.startswith("/"):
                            value = base + value
                        elif not value.startswith("http"):
                            value = base + "/" + value
                        candidates.insert(0, value)
        except Exception:
            pass

        for url in candidates[:5]:
            try:
                resp = await client.get(url, headers=headers)
            except Exception:
                continue
            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code == 200 and resp.content and (
                "image" in ctype or url.lower().endswith((".ico", ".png", ".svg", ".jpg", ".webp"))
            ):
                return resp.content
    return None


@app.get("/api/apps/{app_id}/icon")
async def api_app_icon(app_id: int, refresh: bool = False) -> Response:
    row = db.get_app(app_id)
    if not row:
        raise HTTPException(status_code=404, detail="应用不存在")
    cache = db.ICON_DIR / f"{app_id}.png"
    if cache.exists() and not refresh:
        return FileResponse(cache, media_type="image/png", headers={"Cache-Control": "max-age=3600"})
    data = await _fetch_icon(row)
    if not data:
        return Response(status_code=404)
    try:
        cache.write_bytes(data)
    except OSError:
        pass
    media = "image/png"
    if data[:4] == b"\x00\x00\x01\x00":
        media = "image/x-icon"
    elif data[:4] == b"<svg" or b"<svg" in data[:200]:
        media = "image/svg+xml"
    elif data[:3] == b"\xff\xd8\xff":
        media = "image/jpeg"
    return Response(content=data, media_type=media, headers={"Cache-Control": "max-age=3600"})


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    return {"ok": True, "apps": len(db.list_apps()), "port": SELF_PORT}


# ---------------------------------------------------------------- 反向代理

@app.get("/p/{app_id}")
async def proxy_root(app_id: int, request: Request) -> Response:
    return RedirectResponse(url=f"/p/{app_id}/", status_code=307)


@app.websocket("/p/{app_id}/{path:path}")
async def ws_route(websocket: WebSocket, app_id: int, path: str) -> None:
    row = db.get_app(app_id)
    if not row:
        await websocket.close(code=1008)
        return
    await proxy_websocket(websocket, row, path)


@app.api_route(
    "/p/{app_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_route(app_id: int, path: str, request: Request) -> Response:
    row = db.get_app(app_id)
    if not row:
        raise HTTPException(status_code=404, detail="应用不存在")
    return await proxy_http(row, request, path)


# ---------------------------------------------------------------- 调试与子域

class DebugPayload(BaseModel):
    enabled: bool = True


@app.get("/api/debug")
async def api_debug_info() -> dict[str, Any]:
    apps = decorate_all(db.list_apps())
    return {
        "enabled": DEBUG,
        "suffix": WEB_SUFFIX,
        "self_port": SELF_PORT,
        "mdns": mdns_snapshot(),
        "nginx": {
            "enabled": webconf.enabled(),
            "conf": str(webconf.CONF_PATH),
            "port": webconf.PUBLIC_PORT,
            "internal_port": webconf.INTERNAL_PORT,
        },
        "log_file": logbuf.log_file_path(db.DATA_DIR),
        "apps": [
            {
                "id": item["id"],
                "name": item["name"],
                "slug": item["slug"],
                "frame_url": item["frame_url"],
                "upstream": item["url"],
                "mode": item.get("mode") or "proxy",
            }
            for item in apps
        ],
        "hosts": hosts_snippet(),
    }


@app.post("/api/debug")
async def api_set_debug(payload: DebugPayload) -> dict[str, Any]:
    apply_debug(payload.enabled)
    return {"enabled": DEBUG}


@app.get("/api/logs")
async def api_logs(level: str = "", limit: int = 200, since: int = 0, q: str = "") -> dict[str, Any]:
    items = logbuf.RING.snapshot(level=level, limit=limit, since=since, keyword=q)
    return {"items": items, "total": len(logbuf.RING.items), "debug": DEBUG, "level": level or "ALL"}


@app.delete("/api/logs")
async def api_logs_clear() -> dict[str, bool]:
    logbuf.RING.clear()
    return {"ok": True}


@app.get("/api/subdomains")
async def api_subdomains() -> dict[str, Any]:
    items = [
        {"id": item["id"], "name": item["name"], "slug": item["slug"], "frame_url": item["frame_url"]}
        for item in decorate_all(db.list_apps())
    ]
    return {"suffix": WEB_SUFFIX, "port": SELF_PORT, "items": items, "hosts": hosts_snippet()}


@app.get("/api/hosts")
async def api_hosts() -> Response:
    return Response(content=hosts_snippet(), media_type="text/plain; charset=utf-8")


@app.get("/api/mdns/status")
async def api_mdns_status() -> dict[str, Any]:
    """mDNS 运行状态：后缀列表、网卡、应答 IP 及其来源。"""
    snap = mdns_snapshot()
    snap["hosts"] = hosts_snippet()
    return snap


@app.post("/api/mdns/announce")
async def api_mdns_announce() -> dict[str, Any]:
    """手动触发一次组播通告：客户端仍解析到旧 IP 时用来强制刷新。"""
    if MDNS is None:
        raise HTTPException(status_code=503, detail="mDNS 未启动，请检查 5353 端口占用")
    MDNS.announce()
    return {"ok": True, "mdns": mdns_snapshot()}


@app.get("/api/mdns/selftest")
async def api_mdns_selftest(names: str = "") -> dict[str, Any]:
    """自检「指定域名是否映射到访问地址里的 IP」。

    logic：按响应器匹配规则，本进程会不会应答该域名；
    actual：从本机 5353 端口真实发一次查询，取实际拿到的 A 记录。
    两者不一致时（matched=True 但 actual 为空/不同），通常是客户端侧缓存、
    5353 被占用或网卡组播未加入。
    """
    expect = alias_ip()
    targets: list[str] = []
    for raw in (names or "").split(","):
        name = raw.strip().strip(".").lower()
        if name and name not in targets:
            targets.append(name)
    if not targets:
        targets = [ALIAS_HOST, WEB_SUFFIX]
        for item in decorate_all(db.list_apps())[:8]:
            targets.append(f"{item['slug']}.{WEB_SUFFIX}")

    logic = {name: (MDNS.match(name) if MDNS else None) for name in targets}
    try:
        actual = await mdns_mod.probe(targets)
    except Exception as exc:  # 自检失败不影响结论输出
        log.warning("mDNS 自检查询失败：%s", exc)
        actual = {}

    items = [
        {
            "name": name,
            "matched": bool(logic.get(name)),
            "expect": expect,
            "actual": actual.get(name) or "",
            "ok": bool(expect) and actual.get(name) == expect,
        }
        for name in targets
    ]
    return {
        "ok": bool(items) and all(item["ok"] for item in items),
        "expect_ip": expect,
        "suffixes": list(MDNS.suffixes) if MDNS else [WEB_SUFFIX, ALIAS_HOST],
        "mdns": mdns_snapshot(),
        "items": items,
    }


# ---------------------------------------------------------------- 静态前端

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------- 子域入口

class SubdomainDispatcher:
    """按 Host 分流：app-xx.fnos.local -> 目标应用；其余 -> AppHub 面板。

    放在 ASGI 最外层而不是路由里，才能同时接管 HTTP 与 WebSocket，
    且不受 StaticFiles("/") 的影响。
    """

    def __init__(self, panel) -> None:  # noqa: ANN001
        self.panel = panel

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") in ("http", "websocket"):
            host = ""
            for key, value in scope.get("headers") or []:
                if key == b"host":
                    host = value.decode("latin-1")
                    break
            row, label = resolve_subdomain(host)
            if label:
                if row is None:
                    log.warning("未知子域访问 %s", host)
                    if scope["type"] == "http":
                        response = JSONResponse(
                            {"detail": f"未知子域 {label}.{WEB_SUFFIX}，请先在 AppHub 中创建应用"},
                            status_code=404,
                        )
                        await response(scope, receive, send)
                    else:
                        await WebSocket(scope, receive, send).close(code=1008)
                    return
                path = (scope.get("path") or "/").lstrip("/")
                if scope["type"] == "http":
                    started = time.perf_counter()
                    request = Request(scope, receive)
                    response = await proxy_http(row, request, path, prefix="")
                    await response(scope, receive, send)
                    log.info(
                        "[%s] %s /%s -> %s %.1fms upstream=%s",
                        label, request.method, path, response.status_code,
                        (time.perf_counter() - started) * 1000, row.get("url"),
                    )
                else:
                    await proxy_websocket(WebSocket(scope, receive, send), row, path, prefix="")
                return
        await self.panel(scope, receive, send)


app = SubdomainDispatcher(app)  # type: ignore[assignment]
