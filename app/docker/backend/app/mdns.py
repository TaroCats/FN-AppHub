"""轻量 mDNS 响应器：让 *.fnos.local 这类子域在局域网内被解析。

飞牛自带的 avahi 只发布 fnos.local，`*.fnos.local` 通配解析并不存在，
所以 AppHub 自己在 5353 端口加入组播组，收到该后缀下任意主机名的 A/ANY
查询时返回 NAS IP。这样每个应用一个子域（app-12.fnos.local）都能直接访问，
不需要逐个往客户端 hosts 里加条目。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
from typing import Callable, Optional

log = logging.getLogger("apphub.mdns")

MCAST_GRP = "224.0.0.251"
MCAST_PORT = 5353
QTYPE_A = 1
QTYPE_ANY = 255


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode("idna") if label else b""
        if not raw:
            continue
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _parse_question(data: bytes, offset: int) -> Optional[tuple[str, int, int, int]]:
    """解析单个 question，返回 (name, qtype, qclass, 结束偏移)。"""
    labels: list[str] = []
    pos = offset
    while pos < len(data):
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:  # 压缩指针（question 里极少见）
            pos += 2
            break
        pos += 1
        if pos + length > len(data):
            return None
        chunk = data[pos:pos + length]
        try:
            # idna 编解码器不支持自定义 errors，必须先 strict 再回退
            labels.append(chunk.decode("idna"))
        except (UnicodeError, ValueError):
            labels.append(chunk.decode("latin-1", errors="ignore"))
        pos += length
    if pos + 4 > len(data):
        return None
    qtype, qclass = struct.unpack("!HH", data[pos:pos + 4])
    return ".".join(labels).lower(), qtype, qclass, pos + 4


def build_response(query: bytes, question_end: int, ip: str, unicast: bool, qid: int) -> bytes:
    addr = socket.inet_aton(ip)
    question = query[12:question_end]
    flags = 0x8400  # QR=1, AA=1
    header = struct.pack("!HHHHHH", 0 if not unicast else qid, flags, 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack(
        "!HHIH", QTYPE_A, 0x0001 if unicast else 0x8001, 120, 4
    ) + addr
    return header + question + answer


def local_ipv4_addrs() -> list[tuple[str, str]]:
    """枚举本机各网卡的 IPv4 地址（不依赖 iproute2）。

    某些环境（如 Clash/Meta、Tailscale 的 tun 网卡）会抢占默认路由，
    只往默认网卡加入组播组会导致局域网查询收不到，所以逐个网卡加入。
    """
    import fcntl

    addrs: list[tuple[str, str]] = []
    try:
        names = socket.if_nameindex()
    except OSError:
        return addrs
    for _, name in names:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            ip = socket.inet_ntoa(packed[20:24])
        except OSError:
            continue
        finally:
            sock.close()
        if ip.startswith("127."):
            continue
        addrs.append((name, ip))
    return addrs


class MDNSResponder(asyncio.DatagramProtocol):
    def __init__(
        self,
        suffix: str,
        get_ip: Callable[[], str],
        label_filter: bool = True,
        ifaces: Optional[list[str]] = None,
    ) -> None:
        self.suffix = suffix.strip(".").lower()
        self.get_ip = get_ip
        self.label_filter = label_filter
        self.ifaces = ifaces or []
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.hits = 0

    def connection_made(self, transport) -> None:  # noqa: D102
        self.transport = transport

    def _matches(self, name: str) -> bool:
        name = name.rstrip(".")
        if not name.endswith("." + self.suffix):
            return False
        label = name[: -(len(self.suffix) + 1)]
        if not label:
            return False
        if self.label_filter and "." in label:
            return False
        return True

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        if len(data) < 12:
            return
        try:
            qid, flags, qdcount = struct.unpack("!HHH", data[:6])
            if flags & 0x8000:  # 响应包，忽略
                return
            log.debug("mDNS 收到查询 %s:%s %dB", addr[0], addr[1], len(data))
            self._handle(data, qid, qdcount, addr)
        except Exception as exc:  # mDNS 解析失败不能影响主服务
            log.debug("mDNS 处理异常：%s", exc)
            return

    def _handle(self, data: bytes, qid: int, qdcount: int, addr) -> None:
        ip = self.get_ip()
        if not ip:
            return
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return
        offset = 12
        for _ in range(min(qdcount, 8)):
            parsed = _parse_question(data, offset)
            if not parsed:
                return
            name, qtype, qclass, offset = parsed
            if qtype not in (QTYPE_A, QTYPE_ANY):
                continue
            if (qclass & 0x7FFF) != 1:  # 只处理 IN 类
                continue
            if not self._matches(name):
                continue
            # QU 位（0x8000）置位或来源端口不是 5353 时，单播回给查询方
            unicast = bool(qclass & 0x8000) or addr[1] != MCAST_PORT
            packet = build_response(data, offset, ip, unicast, qid)
            try:
                if unicast:
                    self.transport.sendto(packet, addr)
                else:
                    self._send_multicast(packet)
            except OSError as exc:
                log.debug("mDNS 回应失败 %s: %s", name, exc)
                return
            self.hits += 1
            log.debug("mDNS 解析 %s -> %s (%s)", name, ip, "unicast" if unicast else "multicast")
            return


    def _send_multicast(self, packet: bytes) -> None:
        """组播回包：逐个网卡发出，避免默认路由被 tun 类网卡劫持。"""
        raw = self.transport.get_extra_info("socket") if self.transport else None
        if not self.ifaces or raw is None:
            self.transport.sendto(packet, (MCAST_GRP, MCAST_PORT))
            return
        for ip in self.ifaces:
            try:
                raw.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                self.transport.sendto(packet, (MCAST_GRP, MCAST_PORT))
            except OSError:
                continue


async def start(suffix: str, get_ip: Callable[[], str]) -> Optional[MDNSResponder]:
    """启动 mDNS 响应器；端口/权限问题只记录告警，不影响主服务。"""
    loop = asyncio.get_running_loop()
    ifaces: list[tuple[str, str]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        sock.bind(("", MCAST_PORT))
        ifaces = local_ipv4_addrs()
        joined = 0
        for _, ip in ifaces:
            try:
                mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton(ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined += 1
            except OSError:
                continue
        if not joined:
            mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        log.warning("mDNS 启动失败（%s）：子域需在客户端 hosts 里手动映射", exc)
        return None
    iface_ips = [ip for _, ip in ifaces]
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: MDNSResponder(suffix, get_ip, ifaces=iface_ips), sock=sock
    )
    log.info(
        "mDNS 已启动：*.%s -> 动态映射（组播 %s:%s，网卡 %s）",
        suffix, MCAST_GRP, MCAST_PORT, ", ".join(iface_ips) or "default",
    )
    return protocol


def encode_query(name: str, qtype: int = QTYPE_A) -> bytes:
    """生成 mDNS 查询包，供自检 / 调试使用。"""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(name) + struct.pack("!HH", qtype, 0x0001)
