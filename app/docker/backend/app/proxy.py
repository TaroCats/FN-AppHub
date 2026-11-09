"""反向代理：让任意内网应用可被 iframe 内嵌浏览。

两种工作模式：
1. 前缀模式（/p/{id}/）：应用挂在 AppHub 的路径下，需要重写 HTML 里的绝对路径；
2. 子域模式（app-xx.fnos.local:9080）：应用独占一个主机名、根路径原样转发，
   不需要路径前缀，天然规避「应用发起绝对根路径请求落到 AppHub 根 404」的问题。

公共处理要点：
- 剥离 X-Frame-Options / CSP（否则浏览器拒绝内嵌）；
- 重写 Location / Set-Cookie，避免跳转跳出与 Cookie 串味；
- 支持 WebSocket 透传（日志流、实时面板等）。
"""
from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from typing import Any, AsyncIterator, Iterable
from urllib.parse import urlsplit

import httpx
from fastapi import Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger("apphub.proxy")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
}

DROP_RESPONSE_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "strict-transport-security",
    "public-key-pins",
    # uvicorn 会自行写入 date / server；透传上游同名单值头会形成重复头，
    # Safari（CFNetwork）会判定响应无法解析并报「网页似乎有问题」。
    "date",
    "server",
}

def _dedupe_response_headers(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """丢弃禁用头，且同名头只保留第一条，避免重复单值头让客户端解析失败。"""
    out: dict[str, str] = {}
    for key, value in pairs:
        lower = key.lower()
        if lower in DROP_RESPONSE_HEADERS or lower in out:
            continue
        out[key] = value
    return out


CLIENT = httpx.AsyncClient(
    follow_redirects=False,
    verify=False,
    timeout=httpx.Timeout(120.0, connect=10.0),
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
)


def prefix_for(app_id: int) -> str:
    return f"/p/{app_id}"


RUNTIME_PATCH = """<script>(function(){
try{
var P="__PREFIX__";
function fix(u){
  if(typeof u!=="string")return u;
  if(u===P||u.indexOf(P+"/")===0)return u;
  if(u.indexOf("//")===0||u.slice(0,4)==="http"){
    try{var a=new URL(u,location.href);if(a.host===location.host)return P+a.pathname+a.search+a.hash;}catch(e){}
    return u;
  }
  if(u.charAt(0)!=="/")return u;
  return P+u;
}
var of=window.fetch;
if(of){window.fetch=function(input,init){
  try{
    if(typeof input==="string"){input=fix(input);}
    else if(input&&input.url){
      var r=input;
      input=new Request(fix(r.url),{method:r.method,headers:r.headers,mode:r.mode,credentials:r.credentials,redirect:r.redirect,body:r.body,cache:r.cache});
    }
  }catch(e){}
  return of.call(this,input,init);};}
var oo=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(){ if(arguments.length>1){arguments[1]=fix(arguments[1]);} return oo.apply(this,arguments); };
var OW=window.WebSocket;
if(OW){var NW=function(u,p){var x=fix(u).replace(/^http/,"ws");return p===undefined?new OW(x):new OW(x,p);};NW.prototype=OW.prototype;window.WebSocket=NW;}
var OE=window.EventSource;
if(OE){var NE=function(u,c){return new OE(fix(u),c);};NE.prototype=OE.prototype;window.EventSource=NE;}
var ps=history.pushState, pr=history.replaceState;
history.pushState=function(a,b,c){ return ps.call(this,a,b,typeof c==="string"?fix(c):c); };
history.replaceState=function(a,b,c){ return pr.call(this,a,b,typeof c==="string"?fix(c):c); };
// 三层嵌套（飞牛桌面 -> AppHub -> 目标应用）下，禁止任何弹窗跳出，统一在当前嵌入框内导航
window.open=function(u){ try{ if(u){location.href=fix(u);} }catch(e){} return null; };
document.addEventListener("click",function(e){
  var a=e.target&&e.target.closest?e.target.closest('a[target="_blank"],a[target="_new"],a[target="new"]'):null;
  if(a&&a.href){ e.preventDefault(); try{location.href=fix(a.href);}catch(err){} }
},true);
}catch(e){}
})();</script>"""

# 子域模式：应用已经在根路径，只需要把「目标站点的绝对地址」拉回当前子域，
# 避免前端直连真实 IP 造成跨域/Cookie 串味。
RUNTIME_PATCH_ROOT = """<script>(function(){
try{
var UP="__UPSTREAM__";
var WSUP=UP.replace(/^http/,"ws");
var ROOT=location.protocol+"//"+location.host;
var WSROOT=ROOT.replace(/^http/,"ws");
function fix(u){
  if(typeof u!=="string"||!u)return u;
  if(u.indexOf(UP)===0)return ROOT+u.slice(UP.length);
  if(u.indexOf(WSUP)===0)return WSROOT+u.slice(WSUP.length);
  return u;
}
var of=window.fetch;
if(of){window.fetch=function(input,init){
  try{
    if(typeof input==="string"){input=fix(input);}
    else if(input&&input.url){
      var r=input;
      input=new Request(fix(r.url),{method:r.method,headers:r.headers,mode:r.mode,credentials:r.credentials,redirect:r.redirect,body:r.body,cache:r.cache});
    }
  }catch(e){}
  return of.call(this,input,init);};}
var oo=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(){ if(arguments.length>1){arguments[1]=fix(arguments[1]);} return oo.apply(this,arguments); };
var OW=window.WebSocket;
if(OW){var NW=function(u,p){var x=fix(u);if(x.indexOf("ws")!==0&&x.indexOf("//")===0){x=location.protocol.replace("http","ws")+x;}return p===undefined?new OW(x):new OW(x,p);};NW.prototype=OW.prototype;window.WebSocket=NW;}
var OE=window.EventSource;
if(OE){var NE=function(u,c){return new OE(fix(u),c);};NE.prototype=OE.prototype;window.EventSource=NE;}
}catch(e){}
})();</script>"""


def _strip_blocking(text: str) -> str:
    # 移除会阻断内嵌的 meta CSP
    text = re.sub(
        r"<meta[^>]+http-equiv\s*=\s*[\"']?content-security-policy[\"']?[^>]*>",
        "",
        text,
        flags=re.I,
    )
    # 重写后 integrity 校验必然失败，直接去掉
    return re.sub(r"\sintegrity\s*=\s*[\"'][^\"']*[\"']", "", text, flags=re.I)


def _inject_head(text: str, snippet: str) -> str:
    if re.search(r"<head[^>]*>", text, flags=re.I):
        return re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + snippet, text, count=1, flags=re.I)
    if re.search(r"<html[^>]*>", text, flags=re.I):
        return re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + snippet, text, count=1, flags=re.I)
    return snippet + text


def _rewrite_text(text: str, prefix: str, origin: str) -> str:
    text = _strip_blocking(text)
    # 目标站点绝对地址 -> 代理前缀
    if origin:
        text = text.replace(origin, prefix)
        text = text.replace(origin.replace("https://", "http://"), prefix)
    # 属性中的根路径
    text = re.sub(
        r"(\s(?:src|href|action|poster|data-src|formaction)\s*=\s*[\"'])/(?!/)",
        lambda m: m.group(1) + prefix + "/",
        text,
        flags=re.I,
    )
    # 内联 / 嵌入式样式中的 url(/...)
    text = re.sub(
        r"url\(\s*([\"']?)/(?!/)",
        lambda m: "url(" + m.group(1) + prefix + "/",
        text,
        flags=re.I,
    )
    # srcset 中的绝对路径
    text = re.sub(
        r"(srcset\s*=\s*[\"'])([^\"']*)",
        lambda m: m.group(1) + re.sub(r"(^|,\s*)/(?!/)", lambda x: x.group(1) + prefix + "/", m.group(2)),
        text,
        flags=re.I,
    )
    # AList / OpenList 一类前端用 window.__dynamic_base__ 决定资源根路径
    if "__dynamic_base__" in text:
        text = re.sub(
            r"window\.__dynamic_base__\s*=\s*[^;\n]+;?",
            f'window.__dynamic_base__="{prefix}";',
            text,
        )
    # 注入 base 与运行时补丁，保证相对路径与前端发起的绝对路径请求都落在代理前缀下
    base_tag = f'<base href="{prefix}/">' + RUNTIME_PATCH.replace("__PREFIX__", prefix)
    return _inject_head(text, base_tag)


def _rewrite_text_root(text: str, origin: str) -> str:
    """子域模式：应用在根路径，无需 base；只把绝对地址拉回子域。"""
    text = _strip_blocking(text)
    if origin:
        text = text.replace(origin, "")
        alt = origin.replace("https://", "http://")
        if alt != origin:
            text = text.replace(alt, "")
    if "__dynamic_base__" in text:
        text = re.sub(r"window\.__dynamic_base__\s*=\s*[^;\n]+;?", 'window.__dynamic_base__="";', text)
    return _inject_head(text, RUNTIME_PATCH_ROOT.replace("__UPSTREAM__", origin))


def _rewrite_cookie(value: str, prefix: str) -> str:
    parts = [p.strip() for p in value.split(";")]
    out = [parts[0]]
    for part in parts[1:]:
        low = part.lower()
        if low.startswith("domain=") or low.startswith("path=") or low.startswith("secure"):
            continue
        if low.startswith("samesite=none"):
            out.append("SameSite=Lax")
            continue
        out.append(part)
    out.append(f"Path={prefix}/")
    return "; ".join(out)


def _rewrite_cookie_root(value: str) -> str:
    """子域模式：去掉 Domain（变 host-only，互不串味），保留原始 Path。"""
    parts = [p.strip() for p in value.split(";")]
    out = [parts[0]]
    has_path = False
    for part in parts[1:]:
        low = part.lower()
        if low.startswith("domain=") or low.startswith("secure"):
            continue
        if low.startswith("samesite=none"):
            out.append("SameSite=Lax")
            continue
        if low.startswith("path="):
            has_path = True
        out.append(part)
    if not has_path:
        out.append("Path=/")
    return "; ".join(out)


def _rewrite_location(location: str, base: str, prefix: str, path: str = "/") -> str:
    if not location:
        return location
    if location.startswith(base):
        return prefix + location[len(base):]
    if location.startswith("//"):
        return location
    if location.startswith("/"):
        return prefix + location
    # 相对路径：以代理前缀下的当前目录为基准再归一化，避免 ../ 跳出前缀
    current = path if path.endswith("/") or not path else path.rsplit("/", 1)[0] + "/"
    joined = posixpath.normpath("/" + current.lstrip("/") + location)
    return prefix + joined


def _rewrite_location_root(location: str, base: str, path: str = "/") -> str:
    """子域模式：绝对地址去掉真实 origin；相对地址按当前路径归一化为根相对路径。"""
    if not location:
        return location
    if location.startswith(base):
        return location[len(base):] or "/"
    if location.startswith("//"):
        return location
    if location.startswith("/"):
        return location
    current = path if path.endswith("/") or not path else path.rsplit("/", 1)[0] + "/"
    return posixpath.normpath("/" + current.lstrip("/") + location)


def build_target_url(
    app_row: dict[str, Any], path: str, query: str = "", prefix: str | None = None
) -> tuple[str, str, str]:
    base = (app_row["url"] or "").rstrip("/")
    url = f"{base}/{path}" if path else base + "/"
    if query:
        url += "?" + query
    if prefix is None:
        prefix = prefix_for(int(app_row["id"]))
    return url, base, prefix


def _filter_request_headers(request: Request, origin: str, prefix: str) -> dict[str, str]:
    """转发请求头：改写 Origin/Referer 到目标源，避免目标站点的跨站校验失败。"""
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        low = key.lower()
        if low in HOP_BY_HOP or low in ("accept-encoding", "origin", "referer"):
            continue
        headers[key] = value
    headers["Host"] = urlsplit(origin).netloc
    headers["Accept-Encoding"] = "identity"
    headers["X-Forwarded-Host"] = request.headers.get("host", "")
    headers["X-Forwarded-Proto"] = request.url.scheme
    headers["X-Forwarded-Prefix"] = prefix
    origin_header = request.headers.get("origin")
    if origin_header:
        headers["Origin"] = origin
    referer = request.headers.get("referer")
    if referer:
        try:
            parts = urlsplit(referer)
            headers["Referer"] = origin + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        except ValueError:
            pass
    return headers


async def proxy_http(
    app_row: dict[str, Any], request: Request, path: str, prefix: str | None = None
) -> Response:
    url, origin, prefix = build_target_url(app_row, path, request.url.query, prefix)
    root_mode = prefix == ""
    headers = _filter_request_headers(request, origin, prefix)
    body = await request.body()

    log.debug(
        "proxy -> %s %s (app=%s mode=%s)", request.method, url, app_row.get("name"), "root" if root_mode else prefix
    )
    upstream_request = CLIENT.build_request(
        request.method, url, headers=headers, content=body if body else None
    )
    try:
        upstream = await CLIENT.send(upstream_request, stream=True)
    except httpx.ConnectError as exc:
        log.warning("上游不可达 %s: %s", origin, exc)
        return Response(
            content=f"无法连接到 {origin}，请确认目标应用正在运行。".encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
            headers={"X-AppHub-Upstream": origin},
        )
    except httpx.TimeoutException as exc:
        log.warning("上游超时 %s: %s", origin, exc)
        return Response(
            content=f"连接 {origin} 超时。".encode("utf-8"),
            status_code=504,
            media_type="text/plain; charset=utf-8",
            headers={"X-AppHub-Upstream": origin},
        )

    content_type = upstream.headers.get("content-type", "").lower()
    is_text = any(t in content_type for t in ("text/html", "text/css", "application/xhtml"))
    debug_headers = {"X-AppHub-Upstream": origin}
    if log.isEnabledFor(logging.DEBUG):
        debug_headers["X-AppHub-Mode"] = "root" if root_mode else prefix

    if is_text:
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        charset = "utf-8"
        match = re.search(r"charset=([\w-]+)", content_type)
        if match:
            charset = match.group(1)
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        text = _rewrite_text_root(text, origin) if root_mode else _rewrite_text(text, prefix, origin)
        out_headers = _dedupe_response_headers(list(upstream.headers.multi_items()))
        out_headers["content-type"] = content_type if "charset" in content_type else content_type + "; charset=utf-8"
        out_headers.update(debug_headers)
        location = upstream.headers.get("location")
        if location:
            out_headers["location"] = (
                _rewrite_location_root(location, origin, path)
                if root_mode
                else _rewrite_location(location, origin, prefix, path)
            )
        cookies = upstream.headers.get_list("set-cookie")
        if cookies:
            out_headers.pop("set-cookie", None)
        response = Response(
            content=text.encode("utf-8"),
            status_code=upstream.status_code,
            headers=out_headers,
        )
        for cookie in cookies:
            response.headers.append(
                "set-cookie", _rewrite_cookie_root(cookie) if root_mode else _rewrite_cookie(cookie, prefix)
            )
        log.debug("proxy <- %s %s %s (%sB)", upstream.status_code, request.method, path, len(text))
        return response

    out_headers = _dedupe_response_headers(list(upstream.headers.multi_items()))
    out_headers.update(debug_headers)
    location = upstream.headers.get("location")
    if location:
        out_headers["location"] = (
            _rewrite_location_root(location, origin, path)
            if root_mode
            else _rewrite_location(location, origin, prefix, path)
        )
    cookies = upstream.headers.get_list("set-cookie")
    if cookies:
        out_headers.pop("set-cookie", None)

    async def stream_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except Exception:
            return

    async def cleanup() -> None:
        await upstream.aclose()

    response = StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=out_headers,
        background=BackgroundTask(cleanup),
    )
    for cookie in cookies:
        response.headers.append(
            "set-cookie", _rewrite_cookie_root(cookie) if root_mode else _rewrite_cookie(cookie, prefix)
        )
    return response


async def proxy_websocket(
    websocket: WebSocket, app_row: dict[str, Any], path: str, prefix: str | None = None
) -> None:
    import websockets

    base = (app_row["url"] or "").rstrip("/")
    ws_base = re.sub(r"^http", "ws", base)
    url = f"{ws_base}/{path}" if path else ws_base + "/"
    if websocket.url.query:
        url += "?" + websocket.url.query

    requested = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol=requested[0] if requested else None)

    # 透传 Cookie / Origin / Authorization，供需要登录态的 WebSocket 使用
    extra_headers: dict[str, str] = {}
    for key, value in websocket.headers.items():
        low = key.lower()
        if low in ("cookie", "authorization", "user-agent"):
            extra_headers[key] = value
        elif low == "origin":
            extra_headers["Origin"] = base

    connect_kwargs: dict[str, Any] = {
        "max_size": None,
        "open_timeout": 15,
        "close_timeout": 5,
        "additional_headers": extra_headers or None,
    }
    if requested:
        connect_kwargs["subprotocols"] = requested
    log.debug("ws proxy -> %s (app=%s)", url, app_row.get("name"))
    try:
        connector = websockets.connect(url, **connect_kwargs)  # type: ignore[attr-defined]
    except Exception:
        await websocket.close(code=1011)
        return

    try:
        async with connector as upstream:

            async def client_to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception as exc:
        log.debug("ws 结束 %s: %s", url, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
