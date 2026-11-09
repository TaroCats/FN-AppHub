"""AppHub — 飞牛应用聚合中心 后端入口。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator

from . import db
from .docker_scan import probe as probe_url
from .docker_scan import normalize_host
from .docker_scan import scan as docker_scan
from .proxy import proxy_http, proxy_websocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apphub")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(os.environ.get("APPHUB_STATIC", "/app/static"))
SELF_PORT = os.environ.get("APPHUB_PORT", "9080")

app = FastAPI(title="AppHub", docs_url=None, redoc_url=None)


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
        """统一配置模式下允许只给端口，地址由前端按全局主机拼好；这里兜底校验。"""
        if self.url_mode == "auto":
            if not self.url:
                if not self.port:
                    raise ValueError("统一配置模式下需要填写端口")
                host = normalize_host(db.get_setting("base_host", "")) or "127.0.0.1"
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                self.url = f"http://{host}:{self.port}"
            if not self.port:
                try:
                    self.port = str(urlparse(self.url).port or (443 if self.url.startswith("https") else 80))
                except ValueError:
                    self.port = ""
        elif not self.url:
            raise ValueError("请填写应用地址")
        return self

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

@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
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
        if db.find_by_url(item["url"]):
            continue
        db.create_app(
            {
                "name": item["suggest_name"],
                "url": item["url"],
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
    log.info("首次自动收录 %s 个应用", imported)


# ---------------------------------------------------------------- 应用列表

@app.get("/api/apps")
async def api_list_apps() -> dict[str, Any]:
    apps = db.list_apps()
    groups = ["全部"] + db.distinct_groups()
    return {"apps": apps, "groups": groups}


@app.post("/api/apps")
async def api_create_app(payload: AppPayload) -> dict[str, Any]:
    existing = db.find_by_url(payload.url)
    if existing:
        raise HTTPException(status_code=409, detail=f"该地址已存在：{existing['name']}")
    return db.create_app(payload.model_dump())


@app.put("/api/apps/{app_id}")
async def api_update_app(app_id: int, payload: AppPayload) -> dict[str, Any]:
    updated = db.update_app(app_id, payload.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="应用不存在")
    return updated


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
    """把已添加应用的访问地址主机替换成指定主机。"""
    host = normalize_host(payload.host)
    if not host:
        raise HTTPException(status_code=400, detail="访问地址无效")

    apps = db.list_apps()
    if payload.app_ids:
        wanted = set(payload.app_ids)
        apps = [item for item in apps if item["id"] in wanted]

    taken = {item["url"] for item in db.list_apps()}
    updated: list[dict[str, Any]] = []
    conflicts: list[str] = []
    skipped = 0
    for item in apps:
        current_host = _url_hostname(item["url"])
        if not current_host:
            skipped += 1
            continue
        if payload.only_loopback and (item.get("url_mode") or "auto") != "auto" and current_host not in _LOOPBACK:
            skipped += 1
            continue
        try:
            new_url = _rewrite_url_host(item["url"], host)
        except ValueError:
            skipped += 1
            continue
        if new_url == item["url"]:
            skipped += 1
            continue
        if new_url in taken:
            conflicts.append(item["name"])
            continue
        db.update_app(item["id"], {"url": new_url})
        taken.discard(item["url"])
        taken.add(new_url)
        updated.append({"id": item["id"], "name": item["name"], "url": new_url})

    if updated:
        db.set_setting("base_host", host)
    return {"ok": True, "host": host, "updated": updated, "skipped": skipped, "conflicts": conflicts}


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
    """前端上报当前访问地址，作为扫描/自动收录时的默认主机。"""
    host = normalize_host(payload.host)
    if host:
        db.set_setting("base_host", host)
    return {"ok": True, "host": host}


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

    existing = {a["url"] for a in db.list_apps()}
    for item in candidates:
        item.pop("probe_url", None)
        item["added"] = item["url"] in existing
    return {"items": candidates, "count": len(candidates), "host": base_host}


@app.post("/api/import")
async def api_import(payload: ImportPayload) -> dict[str, Any]:
    created = 0
    skipped = 0
    for item in payload.items:
        url = item.url.rstrip("/")
        if db.find_by_url(url):
            skipped += 1
            continue
        db.create_app(
            {
                "name": item.name,
                "url": url,
                "url_mode": "auto",
                "port": str(urlparse(url).port or ""),
                "group_name": item.group_name or "Docker",
                "color": item.color,
                "container": item.container,
                "open_mode": "inline",
                "mode": "proxy",
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


# ---------------------------------------------------------------- 静态前端

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
