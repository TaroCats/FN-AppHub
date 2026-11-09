"""Docker 端口扫描：通过 docker.sock 读取容器端口映射，并探测 HTTP 可达性。"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

import httpx

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
PROBE_TIMEOUT = float(os.environ.get("APPHUB_PROBE_TIMEOUT", "2.5"))

# 常见镜像/容器名 -> (显示名, 品牌色, 默认分组)
PRESETS: list[tuple[str, str, str, str]] = [
    ("qbittorrent", "qBittorrent", "#3b6fd4", "下载"),
    ("transmission", "Transmission", "#d33f3f", "下载"),
    ("aria2", "Aria2", "#c0392b", "下载"),
    ("ariang", "AriaNg", "#2b7bd6", "下载"),
    ("cloudreve", "Cloudreve", "#2b7fe0", "网盘"),
    ("openlist", "OpenList", "#5b4bd6", "网盘"),
    ("alist", "AList", "#5b4bd6", "网盘"),
    ("clouddrive", "CloudDrive2", "#1c7ed6", "网盘"),
    ("emby", "Emby", "#52b54b", "影音"),
    ("jellyfin", "Jellyfin", "#8b5cf6", "影音"),
    ("plex", "Plex", "#e5a00d", "影音"),
    ("navidrome", "Navidrome", "#1f9d55", "影音"),
    ("home-assistant", "Home Assistant", "#18bcf2", "家庭"),
    ("homeassistant", "Home Assistant", "#18bcf2", "家庭"),
    ("mihomo", "Mihomo 面板", "#5f6b7a", "网络"),
    ("metacubexd", "Mihomo 面板", "#5f6b7a", "网络"),
    ("ddns-go", "DDNS-GO", "#2f9e44", "网络"),
    ("nginx", "Nginx", "#269539", "系统"),
    ("portainer", "Portainer", "#13bef9", "系统"),
    ("dpanel", "DPanel", "#13a0f9", "系统"),
    ("sun-panel", "Sun-Panel", "#f59f00", "系统"),
    ("symedia", "SYMedia", "#7c3aed", "影音"),
    ("moviepilot", "MoviePilot", "#e8590c", "影音"),
    ("iptv", "IPTV", "#d6336c", "影音"),
    ("postgres", "PostgreSQL", "#336791", "数据库"),
    ("mysql", "MySQL", "#00758f", "数据库"),
    ("redis", "Redis", "#d82c20", "数据库"),
    ("mongodb", "MongoDB", "#13aa52", "数据库"),
    ("elasticsearch", "Elasticsearch", "#005571", "数据库"),
    ("vaultwarden", "Vaultwarden", "#175ddc", "工具"),
    ("syncthing", "Syncthing", "#0891b2", "工具"),
    ("gitea", "Gitea", "#609926", "工具"),
    ("code-server", "Code Server", "#007acc", "工具"),
    ("filebrowser", "FileBrowser", "#4b6584", "工具"),
    ("immich", "Immich", "#4250af", "相册"),
    ("photoprism", "PhotoPrism", "#c8a200", "相册"),
    ("nextcloud", "Nextcloud", "#0082c9", "网盘"),
]

PALETTE = [
    "#4f7cff", "#7c5cff", "#0ea5e9", "#14b8a6", "#22c55e",
    "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6", "#06b6d4",
]

INTERNAL_HINTS = ("redis", "postgres", "mysql", "mongo", "rabbitmq", "memcached", "elasticsearch")

# 网关/自身端口不参与收录，避免把 fnOS 入口或本应用自己聚合进来
_EXCLUDE_RAW = os.environ.get("APPHUB_EXCLUDE_PORTS", "80,443")
EXCLUDE_PORTS = {int(x) for x in _EXCLUDE_RAW.split(",") if x.strip().isdigit()}
try:
    EXCLUDE_PORTS.add(int(os.environ.get("APPHUB_PORT", "9080") or 9080))
except ValueError:
    EXCLUDE_PORTS.add(9080)

# 常见 Web 端口，用于同容器多端口时优先挑选
WEB_PORTS = {80, 81, 443, 3000, 3001, 5000, 5055, 5212, 5244, 5666, 7000, 8000, 8008, 8080,
             8081, 8082, 8085, 8088, 8096, 8123, 8181, 8384, 8888, 8899, 9000, 9090, 9091,
             9096, 9097, 10000, 11400, 1905, 23333, 32400, 4533, 4646, 6789, 7878, 8265}


_BAD_HOST = re.compile(r"[^A-Za-z0-9\.\-\[\]:]")


def normalize_host(raw: str) -> str:
    """把用户/请求头里的地址规范成裸主机名（去协议、路径、端口）。"""
    host = (raw or "").strip()
    if not host:
        return ""
    host = re.split(r"[?#]", host)[0]
    host = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", host)
    host = host.split("/")[0]
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if host.startswith("["):  # [fe80::1]:8080
        end = host.find("]")
        host = host[: end + 1] if end > 0 else host
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if not host or len(host) > 120 or _BAD_HOST.search(host):
        return ""
    return host


def display_host(raw: str) -> str:
    """IPv6 需加方括号才能拼进 URL。"""
    host = normalize_host(raw) or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host


def _titleize(text: str) -> str:
    """全小写名称做首字母大写；含驼峰/大写则原样保留，避免破坏品牌名。"""
    cleaned = re.sub(r"[-_]+", " ", text).strip()
    if not cleaned:
        return text
    if cleaned.islower():
        return " ".join(word[:1].upper() + word[1:] for word in cleaned.split())
    return cleaned


def _preset_for(text: str) -> Optional[tuple[str, str, str]]:
    low = text.lower()
    for key, name, color, group in PRESETS:
        if key in low:
            return name, color, group
    return None


def _pick_color(text: str) -> str:
    return PALETTE[sum(ord(c) for c in text) % len(PALETTE)]


def _display_name(container: str, image: str) -> str:
    preset = _preset_for(container) or _preset_for(image)
    if preset:
        return preset[0]
    return _titleize(container) or container


async def list_containers() -> list[dict[str, Any]]:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=10) as client:
        resp = await client.get("/containers/json", params={"all": "0"})
        resp.raise_for_status()
        return resp.json()


async def probe_detail(url: str) -> dict[str, Any]:
    """只要能建立连接（含 3xx/4xx/5xx）即视为该端口上有 HTTP 服务。"""
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            verify=False,
            timeout=httpx.Timeout(PROBE_TIMEOUT, connect=PROBE_TIMEOUT),
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "AppHub/1.0"})
            ctype = resp.headers.get("content-type", "").lower()
            return {"ok": True, "html": "html" in ctype or "xhtml" in ctype, "status": resp.status_code}
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return {"ok": False, "html": False, "status": 0}
    except httpx.HTTPError:
        return {"ok": True, "html": False, "status": 0}
    except Exception:
        return {"ok": False, "html": False, "status": 0}


async def probe(url: str) -> bool:
    return bool((await probe_detail(url)).get("ok"))


async def scan(include_internal: bool = False, all_ports: bool = False,
               host: str = "127.0.0.1") -> list[dict[str, Any]]:
    """扫描运行中容器映射到宿主机的端口。

    - 默认隐藏数据库/缓存类内部服务；
    - 默认同容器多端口只保留最像 Web UI 的一个，避免 BT/RTMP 等端口混入；
    - 过滤掉 fnOS 网关端口（80/443）与本应用自身端口。
    """
    containers = await list_containers()
    candidates: list[dict[str, Any]] = []
    seen_ports: set[int] = set()

    for c in containers:
        name = (c.get("Names") or ["unknown"])[0].lstrip("/")
        image = c.get("Image", "")
        labels = c.get("Labels") or {}
        service = labels.get("com.docker.compose.service") or ""
        merged = f"{name} {image} {service}".lower()
        if not include_internal and any(h in merged for h in INTERNAL_HINTS):
            continue
        for p in c.get("Ports") or []:
            if (p.get("Type") or "tcp") != "tcp":
                continue
            public = p.get("PublicPort")
            if not public:
                continue
            port = int(public)
            if port in EXCLUDE_PORTS or port in seen_ports:
                continue
            seen_ports.add(port)
            scheme = "https" if port in (443, 8443) else "http"
            public_host = display_host(host)
            candidates.append(
                {
                    "container": name,
                    "service": service,
                    "image": image,
                    "port": port,
                    "private_port": int(p.get("PrivatePort") or 0),
                    "url": f"{scheme}://{public_host}:{port}",
                    "probe_url": f"{scheme}://127.0.0.1:{port}",
                    "state": c.get("State", ""),
                }
            )

    # 探测始终走 127.0.0.1（容器 host 网络内可达），对外地址仅用于展示与入库
    details = await asyncio.gather(*[probe_detail(item["probe_url"]) for item in candidates], return_exceptions=True)
    for item, detail in zip(candidates, details):
        if isinstance(detail, dict):
            item.update(detail)
        else:
            item.update({"ok": False, "html": False, "status": 0})
        preset = _preset_for(item["container"]) or _preset_for(item["image"])
        item["suggest_name"] = preset[0] if preset else _display_name(item["container"], item["image"])
        item["suggest_group"] = preset[2] if preset else "Docker"
        item["suggest_color"] = preset[1] if preset else _pick_color(item["container"])
        item["online"] = bool(item.get("ok"))

    if not all_ports:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in candidates:
            grouped.setdefault(item["container"], []).append(item)
        picked: list[dict[str, Any]] = []
        for items in grouped.values():
            items.sort(
                key=lambda x: (
                    not x.get("html"),
                    x["private_port"] not in WEB_PORTS,
                    not x.get("ok"),
                    x["private_port"],
                )
            )
            picked.append(items[0])
        candidates = picked

    candidates.sort(key=lambda x: (not x["online"], x["suggest_group"], x["suggest_name"]))
    return candidates
