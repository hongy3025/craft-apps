"""Manage the storytold *craft applications.

Commands:
    craft list
    craft install [target|all]
    craft upgrade [target|all]
    craft check  [target|all]

The manifest (manifest.yaml) is the source of truth: it records every field
from section 一 of storytold_repositories.md plus release/install state.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "manifest.yaml"
APPS_DIR = ROOT / "apps"
GITHUB = "https://github.com/storytold"
ASSET = "{name}-{version}-windows-x64-portable.zip"


# --------------------------------------------------------------------------- #
# network helpers
# --------------------------------------------------------------------------- #
def proxy_from_cmd() -> str | None:
    """Read HTTPS_PROXY from ~/proxy.cmd when it is not in the environment."""
    candidate = Path.home() / "proxy.cmd"
    if not candidate.is_file():
        return None
    text = candidate.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*set\s+HTTPS_PROXY=(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def make_opener(cli_proxy: str | None = None) -> urllib.request.OpenerDirector:
    proxy = (
        cli_proxy
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or proxy_from_cmd()
    )
    if not proxy:
        raise SystemExit(
            "未检测到代理。请设置 HTTPS_PROXY、使用 --proxy，或确保 ~/proxy.cmd 存在。"
        )
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return urllib.request.build_opener(handler)


def repo_base(program: dict[str, Any]) -> str:
    """GitHub base URL for a program, derived from its repo URL in the manifest."""
    return program.get("url", f"{GITHUB}/{program['name']}").rsplit("/", 1)[0]


def latest_release(
    opener: urllib.request.OpenerDirector, name: str, base: str = GITHUB
) -> tuple[str, str]:
    """Return (tag, version) of the newest release, read from the redirect URL."""
    with opener.open(f"{base}/{name}/releases/latest", timeout=30) as response:
        final_url = response.geturl()
    match = re.search(r"/releases/tag/([^/?#]+)", final_url)
    if not match:
        raise RuntimeError(f"无法解析 {name} 的最新版本: {final_url}")
    tag = urllib.parse.unquote(match.group(1))
    version_match = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?", tag)
    if not version_match:
        raise RuntimeError(f"无法从 tag 解析版本号: {tag}")
    return tag, version_match.group(0)


def asset_url(
    name: str, tag: str, version: str, base: str = GITHUB
) -> tuple[str, str]:
    filename = ASSET.format(name=name, version=version)
    return filename, f"{base}/{name}/releases/download/{tag}/{filename}"


def download(opener: urllib.request.OpenerDirector, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with opener.open(url, timeout=180) as response, dest.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_manifest() -> dict[str, Any]:
    if not MANIFEST.is_file():
        raise SystemExit(f"找不到 manifest: {MANIFEST}")
    with MANIFEST.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"programs": []}


def save_manifest(data: dict[str, Any]) -> None:
    with MANIFEST.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)


def resolve(programs: list[dict[str, Any]], target: str | None) -> list[dict[str, Any]]:
    if target in (None, "all", "ALL", "All"):
        return programs
    for program in programs:
        if program["name"].lower() == target.lower():
            return [program]
    raise SystemExit(f"未知程序: {target}（用 'craft list' 查看可用程序）")


def version_key(value: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value or ""))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list(programs: list[dict[str, Any]]) -> None:
    print(f"storytold craft 程序清单（共 {len(programs)} 个）\n")
    for index, program in enumerate(programs, 1):
        exe = program.get("installed_path") or "-"
        portable = "可用" if program.get("portable") else "无（不提供便携包）"
        print(f"[{index:2d}] {program['name']}")
        print(f"     版本      : {program.get('version', '-')}")
        print(f"     已安装    : {program.get('installed_version') or '未安装'}")
        print(f"     描述      : {program.get('description') or '-'}")
        print(f"     可见性    : {program.get('visibility', '-')}   语言: {program.get('language', '-')}")
        print(f"     Stars/Forks: {program.get('stars', '-')} / {program.get('forks', '-')}")
        print(f"     更新时间  : {program.get('updated', '-')}")
        print(f"     仓库      : {program.get('url', '-')}")
        print(f"     便携包    : {portable}")
        print(f"     可执行文件: {exe}\n")


def locate_exe(target_dir: Path, name: str) -> Path | None:
    """Keep the layout flat: <target_dir>/<name>.exe.

    Portable archives sometimes wrap everything in one top-level folder;
    lift its contents into target_dir so every app has the same shape.
    """
    exe = target_dir / f"{name}.exe"
    if exe.is_file():
        return exe
    entries = list(target_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in list(inner.iterdir()):
            destination = target_dir / item.name
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(item), str(destination))
        inner.rmdir()
        exe = target_dir / f"{name}.exe"
        if exe.is_file():
            return exe
    for path in target_dir.rglob("*.exe"):
        if path.stem.lower() == name.lower():
            return path
    return None


def install_program(
    program: dict[str, Any],
    opener: urllib.request.OpenerDirector,
    tag: str,
    version: str,
    data: dict[str, Any],
) -> bool:
    target_dir = APPS_DIR / program["name"]
    installed = program.get("installed_version")
    if installed == version and target_dir.exists():
        print(f"[跳过] {program['name']} 已安装 {version}")
        return False

    filename, url = asset_url(program["name"], tag, version, repo_base(program))
    print(f"[下载] {filename}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / filename
        download(opener, url, archive)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(target_dir)

    exe = locate_exe(target_dir, program["name"])

    program["version"] = version
    program["tag"] = tag
    program["installed_version"] = version
    program["installed_path"] = (
        exe.relative_to(ROOT).as_posix() if exe else None
    )
    save_manifest(data)
    location = program["installed_path"] or target_dir.relative_to(ROOT).as_posix()
    print(f"[完成] {program['name']} {version} -> ./{location}")
    return True


def cmd_install(
    programs: list[dict[str, Any]],
    opener: urllib.request.OpenerDirector,
    data: dict[str, Any],
) -> None:
    for program in programs:
        if not program.get("portable"):
            print(f"[跳过] {program['name']}: 该仓库不提供 Windows 便携包")
            continue
        try:
            install_program(
                program, opener, program["tag"], program["version"], data
            )
        except (urllib.error.URLError, OSError, zipfile.BadZipFile, KeyError) as error:
            print(f"[失败] {program['name']}: {error}", file=sys.stderr)


def cmd_upgrade(
    programs: list[dict[str, Any]],
    opener: urllib.request.OpenerDirector,
    data: dict[str, Any],
) -> None:
    for program in programs:
        if not program.get("portable"):
            print(f"[跳过] {program['name']}: 该仓库不提供 Windows 便携包")
            continue
        if not program.get("installed_version"):
            print(f"[跳过] {program['name']} 未安装（用 install 安装）")
            continue
        try:
            tag, version = latest_release(opener, program["name"], repo_base(program))
        except (urllib.error.URLError, RuntimeError, OSError) as error:
            print(f"[失败] {program['name']} 查询版本: {error}", file=sys.stderr)
            continue
        if version_key(version) <= version_key(program.get("installed_version")):
            print(f"[最新] {program['name']} 已是 {version}")
            continue
        try:
            changed = install_program(program, opener, tag, version, data)
            if not changed:
                print(f"[最新] {program['name']} 已是 {version}")
        except (urllib.error.URLError, OSError, zipfile.BadZipFile) as error:
            print(f"[失败] {program['name']}: {error}", file=sys.stderr)


def cmd_check(
    programs: list[dict[str, Any]], opener: urllib.request.OpenerDirector
) -> None:
    print(f"{'名称':<18}{'清单版本':<12}{'已安装':<12}{'最新版本':<12}状态")
    print("-" * 68)
    for program in programs:
        try:
            _, version = latest_release(opener, program["name"], repo_base(program))
        except (urllib.error.URLError, RuntimeError, OSError) as error:
            print(f"{program['name']:<18}{'-':<12}{'-':<12}{'-':<12}查询失败: {error}")
            continue
        installed = program.get("installed_version")
        if not program.get("portable"):
            status = "无便携包"
        elif version_key(version) > version_key(installed):
            status = "有更新" if installed else "未安装"
        else:
            status = "已是最新"
        print(
            f"{program['name']:<18}"
            f"{program.get('version', '-'):<12}"
            f"{(installed or '-'):<12}"
            f"{version:<12}"
            f"{status}"
        )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="craft", description="管理 storytold 的 craft 程序"
    )
    parser.add_argument(
        "--proxy", help="代理地址，例如 http://192.168.1.2:7890（默认读取环境变量或 ~/proxy.cmd）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "install", "upgrade", "check"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--proxy", default=argparse.SUPPRESS, help="代理地址（也可写在子命令前）"
        )
        if command != "list":
            subparser.add_argument(
                "target", nargs="?", default="all", help="程序名或 all（默认 all）"
            )
    args = parser.parse_args()

    data = load_manifest()
    programs = data.get("programs", [])
    if args.command == "list":
        cmd_list(programs)
        return

    selected = resolve(programs, getattr(args, "target", None))
    opener = make_opener(args.proxy)
    if args.command == "install":
        cmd_install(selected, opener, data)
    elif args.command == "upgrade":
        cmd_upgrade(selected, opener, data)
    elif args.command == "check":
        cmd_check(selected, opener)


if __name__ == "__main__":
    main()
