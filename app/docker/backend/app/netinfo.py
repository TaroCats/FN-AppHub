"""本机网络地址探测（不依赖 iproute2、psutil 与 mDNS）。"""
from __future__ import annotations

import platform
import re
import socket
import struct
import subprocess


def local_ipv4_addrs() -> list[tuple[str, str]]:
    """枚举本机各网卡的 IPv4 地址，返回 [(网卡名, IP)]。

    不用「连外网看出口地址」的原因：装有 Clash/Tailscale 类工具时，
    那样拿到的是 198.18.x 或 100.x 的虚拟地址，局域网设备无法访问。
    """
    addrs = _ioctl_addrs()
    if not addrs and platform.system() == "Darwin":
        addrs = _ifconfig_addrs()
    if not addrs:
        addrs = _route_probe_addrs()
    return addrs


def _ioctl_addrs() -> list[tuple[str, str]]:
    """Linux：逐个网卡读 SIOCGIFADDR。"""
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


def _ifconfig_addrs() -> list[tuple[str, str]]:
    """macOS：解析 ifconfig 输出的 inet 地址。"""
    try:
        text = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=3, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    addrs: list[tuple[str, str]] = []
    iface = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            iface = line.split(":", 1)[0]
        found = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if not found:
            continue
        ip = found.group(1)
        if ip.startswith(("127.", "169.254.")):
            continue
        addrs.append((iface, ip))
    return addrs


def _route_probe_addrs() -> list[tuple[str, str]]:
    """最后兜底：按默认路由探测（可能拿到代理虚拟地址，交由调用方打分过滤）。"""
    addrs: list[tuple[str, str]] = []
    for target in ("223.5.5.5", "114.114.114.114", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.4)
                sock.connect((target, 80))
                ip = sock.getsockname()[0]
        except OSError:
            continue
        if ip and not ip.startswith("127."):
            addrs.append(("route", ip))
            break
    if not addrs:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and not ip.startswith("127."):
                addrs.append(("hostname", ip))
        except OSError:
            pass
    return addrs
