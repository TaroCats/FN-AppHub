#!/usr/bin/env python3
"""维护 FnDepot V2 第三方源索引 fnpack.json。

用法：
    python3 scripts/gen_fnpack.py --version 1.0.13 --fpk dist/apphub_1.0.13.fpk \
        --download-url https://github.com/owner/repo/releases/download/v1.0.13/apphub_1.0.13.fpk
    python3 scripts/gen_fnpack.py --version 1.0.13 --changelog-out notes.md

脚本以仓库根目录的 manifest 为准生成应用元数据，读入已有 fnpack.json 后
只更新本次版本节点，其它历史版本原样保留。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest"
README = ROOT / "README.md"
INDEX = ROOT / "fnpack.json"

# FnDepot 固定分类，只能用下列取值
CATEGORIES = ["系统工具"]
SOURCE_INFO = {
    "name": "AppHub 第三方源",
    "author": "TaroCats",
    "homepage": "https://github.com/TaroCats/FN-AppHub",
    "description": "飞牛 fnOS 应用聚合中心 AppHub 的第三方更新源，可直接在 FnDepot 中安装与升级。",
}


def read_manifest(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def extract_changelog(version: str) -> str:
    if not README.exists():
        return ""
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^##\s+{re.escape(version)}\s*变更\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    lines = [line.rstrip() for line in match.group(1).strip().splitlines()]
    return "\n".join(lines).strip()


def version_key(version: str) -> tuple:
    """把版本号转成可比较的元组，非法版本排在最后。"""
    main, _, _pre = version.partition("-")
    parts = []
    for chunk in main.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def build_app(manifest: dict[str, str], repo_url: str) -> dict:
    appname = manifest.get("appname") or "apphub"
    return {
        "display_name": manifest.get("display_name", appname),
        "desc": manifest.get("desc", ""),
        "platform": [manifest.get("platform", "x86")],
        "categories": CATEGORIES,
        "icon_url": f"{repo_url}/raw/main/assets/apphub.png",
        "readme_url": f"{repo_url}#readme",
        "bug_report_url": f"{repo_url}/issues",
        "maintainer": manifest.get("maintainer", "TaroCats"),
        "distributor": manifest.get("distributor", manifest.get("maintainer", "TaroCats")),
        "run_as": "package",
        "install_type": "",
        "is_docker": True,
        "service_port": manifest.get("service_port", ""),
        "releases": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 / 更新 FnDepot V2 源索引")
    parser.add_argument("--version", required=True, help="本次发布的版本号")
    parser.add_argument("--fpk", help="FPK 文件路径，用于计算 size 与 sha256")
    parser.add_argument("--download-url", help="FPK 下载地址，缺省时保留已有地址")
    parser.add_argument("--repo-url", default="https://github.com/TaroCats/FN-AppHub")
    parser.add_argument("--changelog-out", help="把该版本的更新说明写到指定文件")
    args = parser.parse_args()

    manifest = read_manifest(MANIFEST)
    appname = manifest.get("appname") or "apphub"
    version = args.version
    changelog = extract_changelog(version)

    if args.changelog_out:
        Path(args.changelog_out).write_text(
            changelog or f"AppHub {version}", encoding="utf-8"
        )
        return 0

    index: dict = {"schema_version": "2", "source_info": SOURCE_INFO, "apps": {}}
    if INDEX.exists():
        try:
            existing = json.loads(INDEX.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                index = existing
                index.setdefault("schema_version", "2")
                index.setdefault("source_info", SOURCE_INFO)
                index.setdefault("apps", {})
        except json.JSONDecodeError:
            print("已有 fnpack.json 无法解析，将重新生成", file=sys.stderr)

    apps = index["apps"]
    app = apps.get(appname) or build_app(manifest, args.repo_url)

    # 元数据始终跟随 manifest 更新
    app.update(
        {
            "display_name": manifest.get("display_name", app.get("display_name", appname)),
            "desc": manifest.get("desc", app.get("desc", "")),
            "platform": [manifest.get("platform", "x86")],
            "categories": CATEGORIES,
            "icon_url": f"{args.repo_url}/raw/main/assets/apphub.png",
            "readme_url": f"{args.repo_url}#readme",
            "bug_report_url": f"{args.repo_url}/issues",
            "maintainer": manifest.get("maintainer", "TaroCats"),
            "distributor": manifest.get("distributor", "TaroCats"),
            "run_as": "package",
            "install_type": "",
            "is_docker": True,
            "service_port": manifest.get("service_port", ""),
        }
    )

    releases = app.setdefault("releases", {})
    release: dict = releases.get(version, {})
    release["os_min_version"] = manifest.get("os_min_version", "")
    release["updated_at"] = now_iso()
    if changelog:
        release["changelog"] = changelog

    packages = release.setdefault("packages", {})
    package = packages.get("all", {})
    if args.download_url:
        package["download_url"] = args.download_url
    if args.fpk:
        fpk = Path(args.fpk)
        if not fpk.is_file():
            print(f"找不到 FPK 文件：{fpk}", file=sys.stderr)
            return 1
        data = fpk.read_bytes()
        package["sha256"] = hashlib.sha256(data).hexdigest()
        package["size"] = len(data)
    if "download_url" not in package:
        print("缺少 download_url：请用 --download-url 指定或先提交一份已有的 fnpack.json", file=sys.stderr)
        return 1
    packages["all"] = package
    releases[version] = release
    app["releases"] = dict(sorted(releases.items(), key=lambda kv: version_key(kv[0])))
    apps[appname] = app

    INDEX.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"fnpack.json 已更新：{appname} {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
