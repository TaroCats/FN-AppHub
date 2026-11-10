"""AppHub — 飞牛应用聚合中心 后端入口。

所有应用统一按 http://<局域网IP>:端口 直连访问：不使用域名、hosts、mDNS，
也不做子域路由与 /p/ 同源代理（面板只负责收录与跳转）。
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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator

from . import db, logbuf
from .netinfo import local_ipv4_addrs
from .docker_scan import probe as probe_url
from .docker_scan import normalize_host
from .docker_scan import scan as docker_scan

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(os.environ.get("APPHUB_STATIC", "/app/static"))
SELF_PORT = os.environ.get("APPHUB_PORT", "9080")

# 统一访问主机：不再使用域名，也不做任何反向代理。
# 应用地址直接保存为 http://<局域网IP>:端口，浏览器按「IP 直连」访问，
# 不依赖 hosts、mDNS、子域路由，也不用 /p/ 同源代理。
DEFAULT_LOCAL_IP = "127.0.0.1"

logbuf.setup(db.DATA_DIR, os.environ.get("APPHUB_LOG_LEVEL", "INFO"))
log = logging.getLogger("apphub")

DEBUG = False

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


def access_ip() -> str:
    """统一访问 IP：优先「访问地址」保存的 IPv4，否则自动探测局域网 IP。

    域名方案已废弃：地址里直接写 IP，浏览器直连，不做 hosts / mDNS / 子域。
    """
    return _as_ipv4(normalize_host(db.get_setting("base_host", ""))) or _as_ipv4(local_ip())


def _to_ip_url(url: str, port: str = "") -> str:
    """把地址收敛成 IP 直连地址：协议沿用原地址，主机固定为当前局域网 IP。"""
    raw = (url or "").strip()
    scheme = "http"
    real_port = str(port or "").strip()
    fallback_host = ""
    if raw:
        parsed = urlparse(raw if "://" in raw else "http://" + raw)
        scheme = parsed.scheme or "http"
        fallback_host = parsed.hostname or ""
        if not real_port:
            try:
                real_port = str(parsed.port or "")
            except ValueError:
                real_port = ""
    host = access_ip() or fallback_host
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    target = f"{host}:{real_port}" if real_port else host
    return f"{scheme}://{target}"

def _target_of(url: str, port: str = "", home_path: str = "") -> tuple[str, str, str]:
    """应用唯一目标：主机 + 端口 + 主页地址，用于重复判定。

    主页地址为空或只有 / 时一律视为 /；url 自带路径（自定义模式）也会算进主页地址，
    避免 http://1.2.3.4:8080/admin 与 home_path=/admin 被判成两个不同应用。
    """
    raw = (url or "").strip()
    host = ""
    url_path = ""
    real_port = str(port or "").strip()
    if raw:
        parsed = urlparse(raw if "://" in raw else "http://" + raw)
        host = (parsed.hostname or "").strip().lower()
        url_path = parsed.path or ""
        if not real_port:
            try:
                explicit = parsed.port
            except ValueError:
                explicit = None
            if explicit:
                real_port = str(explicit)
            else:
                real_port = "443" if parsed.scheme.lower() == "https" else "80"
    if not real_port:
        real_port = "80"
    path = (home_path or "").strip() or url_path
    path = re.sub(r"^https?://[^/]+", "", path, flags=re.I)
    if not path or path == "/":
        path = "/"
    elif not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/") or "/"
    return host, real_port, path


def _find_duplicate(
    url: str, port: str = "", home_path: str = "", exclude_id: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """主机、端口、主页地址三者都相同才算重复。"""
    target = _target_of(url, port, home_path)
    if not target[0]:
        return None
    for item in db.list_apps():
        if exclude_id is not None and item["id"] == exclude_id:
            continue
        if _target_of(item.get("url"), item.get("port"), item.get("home_path")) == target:
            return item
    return None


def _migrate_auto_urls() -> list[dict[str, Any]]:
    """把统一配置模式的应用地址重写为当前局域网 IP:端口（换 IP 后自动收敛）。"""
    changed: list[dict[str, Any]] = []
    for item in db.list_apps():
        if (item.get("url_mode") or "auto") != "auto":
            continue
        new_url = _to_ip_url(item.get("url", ""), item.get("port", ""))
        if new_url and new_url != item.get("url"):
            db.update_app(item["id"], {"url": new_url})
            changed.append({"id": item["id"], "name": item["name"], "url": new_url})
    return changed


def _apply_base_host(host: str) -> str:
    """保存「访问地址」里的局域网 IP，并把统一配置模式的应用地址改成该 IP。

    只接受 IPv4：域名/主机名（历史遗留）一律不写入，避免把应用地址带回域名。
    """
    raw = normalize_host(host or "")
    ip = _as_ipv4(raw)
    if not ip:
        if raw:
            log.info("忽略非 IPv4 的访问地址：%s", raw)
        return ""
    db.set_setting("base_host", ip)
    _migrate_auto_urls()
    log.info("访问地址已更新为 %s，统一配置应用地址已按该 IP 重写", ip)
    return ip


# ---------------------------------------------------------------- 应用字段辅助

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def ensure_slug(app_row: dict[str, Any]) -> str:
    """保证每个应用有唯一标签（仅用于排障展示，不参与路由）。"""
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
    return slug


def open_url(app_row: dict[str, Any]) -> str:
    """应用的实际打开地址：IP:端口 + 主页路径。"""
    base = (app_row.get("url") or "").rstrip("/")
    path = (app_row.get("home_path") or "").strip()
    if not path or path == "/":
        path = "/"
    elif not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def decorate(app_row: dict[str, Any]) -> dict[str, Any]:
    """内嵌与跳转都使用同一个 IP 直连地址，不再提供任何代理路径。"""
    app_row["slug"] = ensure_slug(app_row)
    url = open_url(app_row)
    app_row["frame_origin"] = ""
    app_row["frame_url"] = url
    return app_row


def decorate_all(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [decorate(item) for item in items]


def local_ip() -> str:
    """探测局域网 IP，用于生成应用的访问地址。

    不用「连外网看出口地址」的原因：装有 Clash/Tailscale 类工具时，
    那样拿到的是 198.18.x 或 100.x 的虚拟地址，局域网设备无法访问。
    """
    best, best_score = "", -2
    for _, ip in local_ipv4_addrs():
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
    """把文本收敛成可用作访问地址的 IPv4；不可用返回空串。"""
    text = (value or "").strip().strip("[]")
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return ""
    if parsed.version != 4 or parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
        return ""
    return str(parsed)


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
    mode: str = "direct"
    color: str = ""
    container: str = ""
    slug: str = ""            # 标签，如 app-12（留空自动分配，仅排障展示）

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
        """统一配置模式的地址固定为「协议://当前局域网IP:端口」，浏览器直连。"""
        if self.url_mode == "auto":
            if not self.url and not self.port:
                raise ValueError("统一配置模式下需要填写端口")
            self.url = _to_ip_url(self.url, self.port)
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
        """内嵌方式已统一为 IP 直连：历史 proxy/direct 记录一律归为 direct。"""
        return "direct"

    @field_validator("open_mode")
    @classmethod
    def _normalize_open_mode(cls, value: str) -> str:
        """打开方式：inline 窗口内打开，window 新窗口打开。"""
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

@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    _migrate_auto_urls()
    apply_debug(db.get_setting("debug", "") == "1")
    for item in db.list_apps():
        decorate(item)
    log.info(
        "AppHub 以 IP 直连模式运行：应用地址为 http://<局域网IP>:端口（当前 IP %s），不使用域名与反代",
        access_ip() or "未获取到局域网 IP",
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
        alias_url = _to_ip_url(item["url"], str(item.get("port") or ""))
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
                "mode": "direct",
            }
        )
        imported += 1
        if imported >= 24:
            break
    db.set_setting("auto_import", "done")
    if imported:
        log.info("首次自动收录 %s 个应用", imported)


# ---------------------------------------------------------------- 应用列表

@app.get("/api/apps")
async def api_list_apps() -> dict[str, Any]:
    apps = decorate_all(db.list_apps())
    groups = ["全部"] + db.distinct_groups()
    return {"apps": apps, "groups": groups, "self_port": SELF_PORT}


@app.post("/api/apps")
async def api_create_app(payload: AppPayload) -> dict[str, Any]:
    existing = _find_duplicate(payload.url, payload.port, payload.home_path)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"该地址已存在：{existing['name']}（同 IP、端口与主页地址）",
        )
    data = payload.model_dump()
    if data.get("slug") and db.slug_taken(data["slug"]):
        raise HTTPException(status_code=409, detail=f"标签已被占用：{data['slug']}")
    row = decorate(db.create_app(data))
    return row


@app.put("/api/apps/{app_id}")
async def api_update_app(app_id: int, payload: AppPayload) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    if not db.get_app(app_id):
        raise HTTPException(status_code=404, detail="应用不存在")
    existing = _find_duplicate(
        payload.url, payload.port, payload.home_path, exclude_id=app_id
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"该地址已存在：{existing['name']}（同 IP、端口与主页地址）",
        )
    if data.get("slug") and db.slug_taken(data["slug"], exclude_id=app_id):
        raise HTTPException(status_code=409, detail=f"标签已被占用：{data['slug']}")
    updated = db.update_app(app_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="应用不存在")
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
    """保存局域网 IP，并把所有统一配置的应用地址重写为 http://该IP:端口。"""
    host = _apply_base_host(payload.host)
    if not host:
        raise HTTPException(status_code=400, detail="访问地址无效")

    auto_apps = [item for item in db.list_apps() if (item.get("url_mode") or "auto") == "auto"]
    return {
        "ok": True,
        "host": host,
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
    """前端上报访问地址：写库并把统一配置的应用地址改成该 IP。"""
    host = _apply_base_host(payload.host)
    warning = ""
    if host and not _as_ipv4(host):
        warning = f"「{host}」不是可用的 IPv4，建议填写局域网 IP（如 192.168.1.10）"
    return {"ok": True, "host": host, "warning": warning}


@app.get("/api/settings/base-host")
async def api_get_base_host(request: Request) -> dict[str, Any]:
    saved = _as_ipv4(normalize_host(db.get_setting("base_host", "")))
    host = saved or _as_ipv4(_request_host(request)) or access_ip() or ""
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
        _as_ipv4(normalize_host(host))
        or _as_ipv4(normalize_host(db.get_setting("base_host", "")))
        or _as_ipv4(_request_host(request))
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
    existing |= {_to_ip_url(a["url"], a.get("port", "")) for a in apps}
    for item in candidates:
        item.pop("probe_url", None)
        item["added"] = item["url"] in existing or _to_ip_url(item["url"], item["port"]) in existing
    return {"items": candidates, "count": len(candidates), "host": base_host}


@app.post("/api/import")
async def api_import(payload: ImportPayload) -> dict[str, Any]:
    created = 0
    skipped = 0
    for item in payload.items:
        port = str(urlparse(item.url if "://" in item.url else "http://" + item.url).port or "")
        url = _to_ip_url(item.url, port)
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
                "mode": "direct",
            }
        )
        created += 1
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


@app.get("/api/logs")
async def api_logs(level: str = "", limit: int = 200, since: int = 0, q: str = "") -> dict[str, Any]:
    items = logbuf.RING.snapshot(level=level, limit=limit, since=since, keyword=q)
    return {"items": items, "total": len(logbuf.RING.items), "debug": DEBUG, "level": level or "ALL"}


@app.delete("/api/logs")
async def api_logs_clear() -> dict[str, bool]:
    logbuf.RING.clear()
    return {"ok": True}


# ---------------------------------------------------------------- 静态前端

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
