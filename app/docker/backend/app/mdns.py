"""轻量 mDNS 响应器：让 fnos.local / *.fnos.local 这类域名在局域网内解析。

飞牛自带的 avahi 只发布 fnos.local，`*.fnos.local` 通配解析并不存在，
所以 AppHub 自己在 5353 端口加入组播组，收到后缀下任意主机名（含裸域名本身）
的 A/ANY 查询时返回「访问地址」里保存的映射 IP。这样：

* 主面板 https://fnos.local:9080 本身就是 AppHub 自己在应答，不再依赖 avahi；
* 每个应用一个子域（app-12.fnos.local）都能直接访问，不需要逐个往客户端 hosts 里加条目。

注意：应答值是运行时回调（main.alias_ip），「访问地址」保存映射 IP 后立即生效，
TTL 取短值，避免客户端缓存住旧 IP。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import time
from typing import Callable, Iterable, Optional

log = logging.getLogger("apphub.mdns")

MCAST_GRP = "224.0.0.251"
MCAST_PORT = 5353
QTYPE_A = 1
QTYPE_AAAA = 28
QTYPE_ANY = 255
QTYPE_NSEC = 47
# 映射 IP 随时可能改（换网段、多网卡），TTL 取短值让客户端尽快重新查询
DEFAULT_TTL = 30


def _normalize_suffixes(suffixes: str | Iterable[str]) -> list[str]:
    """统一后缀列表：去点、小写、去重（保持顺序）。"""
    raw = [suffixes] if isinstance(suffixes, str) else list(suffixes)
    out: list[str] = []
    for item in raw:
        name = (item or "").strip().strip(".").lower()
        if name and name not in out:
            out.append(name)
    return out


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode("idna") if label else b""
        if not raw:
            continue
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _parse_question(data: bytes, offset: int) -> Optional[tuple[str, int, int, int]]:
    """解析单个 question，返回 (name, qtype, qclass, 结束偏移)。

    macOS 的 mDNSResponder 在同包多 question 时，第二个 question 的名字常用压缩
    指针（如 A 后跟 `c00c` + AAAA）。必须跟随指针解析，否则该 question 的 name
    为空、被当成无关域名跳过，AAAA 就永远得不到应答。
    """
    labels: list[str] = []
    pos = offset
    end: Optional[int] = None  # 指针后（或根标签后）的 qtype 起始偏移
    hops = 0
    while pos < len(data):
        length = data[pos]
        if length == 0:
            pos += 1
            if end is None:
                end = pos
            break
        if length & 0xC0 == 0xC0:  # 压缩指针
            if pos + 1 >= len(data):
                return None
            ptr = ((length & 0x3F) << 8) | data[pos + 1]
            if end is None:
                end = pos + 2
            hops += 1
            if hops > 16 or ptr >= len(data):
                return None
            pos = ptr
            continue
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
    qpos = end if end is not None else pos
    if qpos + 4 > len(data):
        return None
    qtype, qclass = struct.unpack("!HH", data[qpos:qpos + 4])
    return ".".join(labels).lower(), qtype, qclass, qpos + 4


def build_response(
    query: bytes,
    q_start: int,
    q_end: int,
    ip: str,
    unicast: bool,
    qid: int,
    ttl: int = DEFAULT_TTL,
) -> bytes:
    """组装 A 记录应答。

    question 段必须只带「当前这一个」question：一个查询包里常有多个 question
    （例如 A + AAAA），若把前面的 question 也带进来而 header 里 qdcount 写 1，
    整包会因结构错乱被客户端丢弃——这正是「域名解析不到」的典型原因。
    """
    addr = socket.inet_aton(ip)
    question = query[q_start:q_end]
    flags = 0x8400  # QR=1, AA=1
    header = struct.pack("!HHHHHH", 0 if not unicast else qid, flags, 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack(
        "!HHIH", QTYPE_A, 0x0001 if unicast else 0x8001, max(1, int(ttl)), 4
    ) + addr
    return header + question + answer


def build_nodata(
    query: bytes,
    q_start: int,
    q_end: int,
    unicast: bool,
    qid: int,
    name: str = "",
    ttl: int = DEFAULT_TTL,
) -> bytes:
    """对 AAAA 等无法提供的类型回「带 NSEC 的否定应答」。

    单纯回 NOERROR + 0 条回答（裸 NODATA）在 mDNS 里不构成权威否认：
    macOS 的 mDNSResponder 收不到 NSEC 会认为「对方还没答完」，继续等重传，
    getaddrinfo 双栈（A+AAAA）解析因此固定多花约 2.5s，curl 再经一次 302
    重解析就变成 5s。按 RFC 6762 §6.1，否定应答必须附一条 NSEC 记录，
    其 type bitmap 只声明「该名字存在 A 记录」，客户端才会立即放弃 AAAA。
    """
    question = query[q_start:q_end]
    if not name:
        header = struct.pack("!HHHHHH", 0 if not unicast else qid, 0x8400, 1, 0, 0, 0)
        return header + question
    # NSEC RDATA：Next Domain Name 不得压缩（RFC 4034 §4.1.1）+ type bitmap
    # window block 0 / bitmap 长度 1 / 0x40 表示只存在 A（type 1）
    rdata = _encode_name(name) + b"\x00\x01\x40"
    answer = (
        b"\xc0\x0c"
        + struct.pack(
            "!HHIH",
            QTYPE_NSEC,
            0x0001 if unicast else 0x8001,
            max(1, int(ttl)),
            len(rdata),
        )
        + rdata
    )
    header = struct.pack("!HHHHHH", 0 if not unicast else qid, 0x8400, 1, 1, 0, 0)
    return header + question + answer


def _a_record(name: str, ip: str, unicast: bool, ttl: int = DEFAULT_TTL) -> bytes:
    """单条 A 资源记录（名字完整编码，合并应答时不依赖压缩指针偏移）。"""
    return (
        _encode_name(name)
        + struct.pack(
            "!HHIH", QTYPE_A, 0x0001 if unicast else 0x8001, max(1, int(ttl)), 4
        )
        + socket.inet_aton(ip)
    )


def _nsec_record(name: str, unicast: bool, ttl: int = DEFAULT_TTL) -> bytes:
    """单条 NSEC 资源记录：声明该名字只存在 A，不存在 AAAA。"""
    rdata = _encode_name(name) + b"\x00\x01\x40"
    return (
        _encode_name(name)
        + struct.pack(
            "!HHIH",
            QTYPE_NSEC,
            0x0001 if unicast else 0x8001,
            max(1, int(ttl)),
            len(rdata),
        )
        + rdata
    )


def build_answers(
    questions: list[bytes], records: list[bytes], unicast: bool, qid: int
) -> bytes:
    """把同一次查询内所有 question 的应答合并成一个包。

    macOS 的 mDNSResponder 对一次查询只接受第一个应答包：A 与 AAAA 若各回一个包，
    后面那个（NSEC 否定应答）会被丢弃，AAAA 分支照样等满 2.5s。合并成一个包、
    question 与 answer 一一对应，双栈解析才能立即拿到结论。
    """
    flags = 0x8400  # QR=1, AA=1
    header = struct.pack(
        "!HHHHHH",
        0 if not unicast else qid,
        flags,
        len(questions),
        len(records),
        0,
        0,
    )
    return header + b"".join(questions) + b"".join(records)


def build_announce(names: Iterable[str], ip: str, ttl: int = DEFAULT_TTL) -> bytes:
    """主动通告：qdcount=0、每个名字一条 A 记录（名称不压缩，便于对端直接比对）。"""
    items = _normalize_suffixes(names)
    if not items:
        return b""
    addr = socket.inet_aton(ip)
    body = b"".join(
        _encode_name(name) + struct.pack("!HHIH", QTYPE_A, 0x8001, max(1, int(ttl)), 4) + addr
        for name in items
    )
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(items), 0, 0) + body


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
        suffixes: str | Iterable[str],
        get_ip: Callable[[], str],
        label_filter: bool = True,
        ifaces: Optional[list[str]] = None,
        ttl: int = DEFAULT_TTL,
        allow_apex: bool = True,
    ) -> None:
        self.suffixes = _normalize_suffixes(suffixes) or ["fnos.local"]
        self.get_ip = get_ip
        self.label_filter = label_filter
        self.ifaces = ifaces or []
        self.ttl = max(1, int(ttl))
        self.allow_apex = allow_apex
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.hits = 0

    @property
    def suffix(self) -> str:
        """主后缀（历史字段，保留兼容）。"""
        return self.suffixes[0]

    def connection_made(self, transport) -> None:  # noqa: D102
        self.transport = transport

    def _match(self, name: str) -> Optional[str]:
        """命中返回对应后缀；未命中返回 None。

        allow_apex 打开时，裸域名本身（fnos.local）也由本响应器应答，
        这样「访问地址」里的 hosts 映射不再依赖 avahi 是否在跑。
        """
        name = (name or "").rstrip(".").lower()
        if not name:
            return None
        for suffix in self.suffixes:
            if name == suffix:
                if self.allow_apex:
                    return suffix
                continue
            if not name.endswith("." + suffix):
                continue
            label = name[: -(len(suffix) + 1)]
            if not label:
                continue
            if self.label_filter and "." in label:
                continue
            return suffix
        return None

    def _matches(self, name: str) -> bool:
        return self._match(name) is not None

    def match(self, name: str) -> Optional[str]:
        """公开匹配入口，调试自检用。"""
        return self._match(name)

    def answer_ip(self) -> str:
        """当前应答 IP；非法（空、域名、IPv6、回环网段）时返回空串。"""
        try:
            ip = (self.get_ip() or "").strip()
        except Exception:  # 回调里读库失败不能拖垮响应器
            return ""
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        if parsed.version != 4 or parsed.is_loopback or parsed.is_unspecified:
            return ""
        return str(parsed)

    def status(self) -> dict:
        """给调试面板用的运行状态快照。"""
        return {
            "suffixes": list(self.suffixes),
            "apex": self.allow_apex,
            "ttl": self.ttl,
            "ifaces": list(self.ifaces),
            "hits": self.hits,
        }

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
        offset = 12
        ip: Optional[str] = None
        questions: list[bytes] = []
        records: list[bytes] = []
        names: list[str] = []
        unicast = False
        for _ in range(min(qdcount, 8)):
            q_start = offset
            parsed = _parse_question(data, offset)
            if not parsed:
                return
            name, qtype, qclass, offset = parsed
            if (qclass & 0x7FFF) != 1:  # 只处理 IN 类
                continue
            if not self._match(name):
                continue
            # QU 位（0x8000）置位或来源端口不是 5353 时，单播回给查询方
            qu = bool(qclass & 0x8000) or addr[1] != MCAST_PORT
            # question 段一律用完整名字重编码：原查询里的压缩指针在新包里偏移会失效
            question = _encode_name(name) + struct.pack("!HH", qtype, qclass)
            if qtype == QTYPE_AAAA:
                # 本服务只有 IPv4：明确回带 NSEC 的权威否定，客户端立即放弃 AAAA
                questions.append(question)
                records.append(_nsec_record(name, qu, self.ttl))
                names.append(name)
                unicast = unicast or qu
                continue
            if qtype not in (QTYPE_A, QTYPE_ANY):
                continue
            if ip is None:
                ip = self.answer_ip()
            if not ip:
                log.debug("mDNS 跳过 %s：访问地址里没有可用 IPv4", name)
                continue
            questions.append(question)
            records.append(_a_record(name, ip, qu, self.ttl))
            names.append(name)
            unicast = unicast or qu
        if not records:
            return
        # 同一查询的 A + AAAA 必须合并成一个包回，拆包会让 NSEC 被客户端丢弃
        self._reply(
            build_answers(questions, records, unicast, qid),
            ",".join(names),
            ip,
            unicast,
            addr,
        )

    def _reply(self, packet: bytes, name: str, ip: Optional[str], unicast: bool, addr) -> None:
        if self.transport is None:
            return
        try:
            if unicast:
                self.transport.sendto(packet, addr)
            else:
                self._send_multicast(packet)
        except OSError as exc:
            log.debug("mDNS 回应失败 %s: %s", name, exc)
            return
        self.hits += 1
        log.debug(
            "mDNS 解析 %s -> %s (%s)",
            name, ip or "NODATA", "unicast" if unicast else "multicast",
        )

    def _send_multicast(self, packet: bytes) -> None:
        """组播回包：逐个网卡发出，避免默认路由被 tun 类网卡劫持。"""
        if self.transport is None:
            return
        raw = self.transport.get_extra_info("socket")
        if not self.ifaces or raw is None:
            self.transport.sendto(packet, (MCAST_GRP, MCAST_PORT))
            return
        for ip in self.ifaces:
            try:
                raw.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                self.transport.sendto(packet, (MCAST_GRP, MCAST_PORT))
            except OSError:
                continue

    def announce(self, names: Optional[Iterable[str]] = None) -> None:
        """主动组播通告当前应答复 IP（启动完成、映射 IP 变更时调用）。

        客户端缓存遵循 TTL，但「刚改完访问地址却仍解析到旧 IP」很常见，
        一次主动通告可以让局域网设备立刻刷新记录。
        """
        ip = self.answer_ip()
        if not ip:
            log.debug("mDNS 跳过通告：访问地址里没有可用 IPv4")
            return
        targets = _normalize_suffixes(names) if names else list(self.suffixes)
        if not targets:
            return
        try:
            packet = build_announce(targets, ip, self.ttl)
        except OSError as exc:
            log.debug("mDNS 通告报文构造失败：%s", exc)
            return
        self._send_multicast(packet)
        log.info("mDNS 主动通告 %s -> %s", " / ".join(targets), ip)


async def start(
    suffixes: str | Iterable[str],
    get_ip: Callable[[], str],
    ttl: int = DEFAULT_TTL,
) -> Optional[MDNSResponder]:
    """启动 mDNS 响应器；端口/权限问题只记录告警，不影响主服务。"""
    names = _normalize_suffixes(suffixes) or ["fnos.local"]
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
        lambda: MDNSResponder(names, get_ip, ifaces=iface_ips, ttl=ttl), sock=sock
    )
    log.info(
        "mDNS 已启动：%s（含裸域名）-> 动态映射（组播 %s:%s，TTL %ss，网卡 %s）",
        " / ".join(names), MCAST_GRP, MCAST_PORT, ttl, ", ".join(iface_ips) or "default",
    )
    return protocol


def encode_query(name: str, qtype: int = QTYPE_A) -> bytes:
    """生成 mDNS 查询包，供自检 / 调试使用。"""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(name) + struct.pack("!HH", qtype, 0x0001)


def _skip_name(data: bytes, pos: int) -> int:
    """跳过 DNS 名称（支持压缩指针）。"""
    while pos < len(data):
        length = data[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + length
    return pos


def parse_response(data: bytes) -> Optional[tuple[int, str]]:
    """从响应包里取第一个 A 记录，返回 (报文 id, IP)。"""
    if len(data) < 12:
        return None
    qid, flags, qdcount, ancount = struct.unpack("!HHHH", data[:8])
    if not flags & 0x8000 or not ancount:
        return None
    pos = 12
    for _ in range(min(qdcount, 8)):
        pos = _skip_name(data, pos) + 4
    for _ in range(min(ancount, 8)):
        if pos >= len(data):
            return None
        pos = _skip_name(data, pos)
        if pos + 10 > len(data):
            return None
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == QTYPE_A and rdlen == 4 and pos + 4 <= len(data):
            return qid, socket.inet_ntoa(data[pos:pos + 4])
        pos += rdlen
    return None


def query_once(
    name: str,
    host: str = "127.0.0.1",
    port: int = MCAST_PORT,
    timeout: float = 0.8,
) -> Optional[tuple[int, str]]:
    """向本机 5353 发一次查询并等待应答，供自检使用。

    非 5353 源端口会被响应器当成 legacy unicast 查询，正好用于「本机自问自答」。
    """
    qid = int(time.time() * 1000) & 0xFFFF or 1
    packet = (
        struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0)
        + _encode_name(name)
        + struct.pack("!HH", QTYPE_A, 0x0001)
    )
    # 5353 可能被系统 mDNS 守护进程共享（SO_REUSEPORT），单次发包有概率被
    # 投递到别的进程，分几轮重发提高自检可靠性。
    per = max(0.05, timeout / 3)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(per)
        for _ in range(3):
            try:
                sock.sendto(packet, (host, port))
                deadline = time.monotonic() + per
                while time.monotonic() < deadline:
                    data, _ = sock.recvfrom(2048)
                    parsed = parse_response(data)
                    if parsed:
                        return parsed
            except socket.timeout:
                continue
            except OSError:
                return None
    return None


async def probe(
    names: Iterable[str],
    host: str = "127.0.0.1",
    timeout: float = 0.8,
) -> dict[str, Optional[str]]:
    """并发解析多个域名，返回 {域名: 实际应答 IP}；无应答为 None。"""
    loop = asyncio.get_running_loop()
    items = list(dict.fromkeys([n.strip().rstrip(".").lower() for n in names if n and n.strip()]))
    if not items:
        return {}
    futures = [loop.run_in_executor(None, query_once, name, host, MCAST_PORT, timeout) for name in items]
    results = await asyncio.gather(*futures, return_exceptions=True)
    out: dict[str, Optional[str]] = {}
    for name, result in zip(items, results):
        out[name] = result[1] if isinstance(result, tuple) and len(result) == 2 else None
    return out

