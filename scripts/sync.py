#!/usr/bin/env python3
"""Fetch official QQ Windows/Linux installers and optionally publish a GitHub Release."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_DIR = ROOT / "downloads"

PC_CONFIG_URLS = [
    "https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/pcConfig.json",
    "https://im.qq.com/proxy/domain/cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/pcConfig.json",
]
LINUX_CONFIG_URLS = [
    "https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/linuxConfig.js",
]


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 QQ-Releases-Mirror/1.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(urls: list[str]) -> str:
    last_err: Exception | None = None
    for url in urls:
        try:
            return http_get(url).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"warn: failed {url}: {exc}", file=sys.stderr)
    raise RuntimeError(f"all URLs failed: {urls}") from last_err


def parse_linux_config_js(text: str) -> dict:
    match = re.search(r"var params=\s*(\{.*?\});", text, re.DOTALL)
    if not match:
        raise ValueError("cannot parse linuxConfig.js")
    return json.loads(match.group(1))


def collect_urls(pc: dict, linux: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (win_version, linux_version, [(filename, url), ...])."""
    win = pc.get("Windows") or {}
    lin = linux or pc.get("Linux") or {}

    win_ver = str(win.get("version") or "unknown")
    lin_ver = str(lin.get("version") or "unknown")

    items: list[tuple[str, str]] = []

    win_keys = (
        ("ntDownloadX64Url", "windows"),
        ("ntDownloadUrl", "windows"),
        ("ntDownloadARMUrl", "windows"),
    )
    for key, _ in win_keys:
        url = win.get(key)
        if isinstance(url, str) and url.startswith("http"):
            items.append((Path(url).name, url))

    def add_map(obj: object) -> None:
        if isinstance(obj, str) and obj.startswith("http"):
            items.append((Path(obj).name, obj))
            return
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, str) and v.startswith("http"):
                    items.append((Path(v).name, v))

    add_map(lin.get("x64DownloadUrl"))
    add_map(lin.get("armDownloadUrl"))
    add_map(lin.get("loongarchDownloadUrl"))
    add_map(lin.get("mipsDownloadUrl"))

    # de-dupe by filename, keep first
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, url in items:
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, url))

    return win_ver, lin_ver, unique


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"skip existing: {dest.name}")
        return
    print(f"download: {dest.name}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 QQ-Releases-Mirror/1.0"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def release_exists(tag: str) -> bool:
    r = subprocess.run(
        ["gh", "release", "view", tag],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def create_release(tag: str, title: str, body: str, files: list[Path]) -> None:
    cmd = [
        "gh",
        "release",
        "create",
        tag,
        "--title",
        title,
        "--notes",
        body,
    ]
    cmd.extend(str(p) for p in files)
    subprocess.run(cmd, cwd=ROOT, check=True)


def write_github_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        if "\n" in value:
            f.write(f"{key}<<EOF\n{value}\nEOF\n")
        else:
            f.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="only download installers, do not create GitHub release",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="create GitHub release if this version tag does not exist",
    )
    args = parser.parse_args()
    if not args.download_only and not args.publish:
        args.download_only = True

    print("fetch pcConfig.json ...")
    pc = json.loads(fetch_text(PC_CONFIG_URLS))
    print("fetch linuxConfig.js ...")
    linux = parse_linux_config_js(fetch_text(LINUX_CONFIG_URLS))

    win_ver, lin_ver, items = collect_urls(pc, linux)
    if not items:
        print("error: no download URLs found", file=sys.stderr)
        return 1

    tag = f"{win_ver}+{lin_ver}"
    print(f"Windows: {win_ver}")
    print(f"Linux:   {lin_ver}")
    print(f"Tag:     {tag}")
    print(f"Files:   {len(items)}")
    for name, url in items:
        print(f"  - {name}")
        print(f"    {url}")

    write_github_output("tag", tag)
    write_github_output("win_version", win_ver)
    write_github_output("linux_version", lin_ver)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    for name, url in items:
        dest = DOWNLOAD_DIR / name
        download_file(url, dest)
        downloaded.append(dest)

    links_md = ["### Official Download Links", "", "| File | URL |", "| --- | --- |"]
    for name, url in items:
        links_md.append(f"| `{name}` | {url} |")
    body = "\n".join(
        [
            "Mirrored from official Tencent QQ downloads.",
            "",
            f"- Windows QQNT: `{win_ver}`",
            f"- Linux QQ: `{lin_ver}`",
            "",
            *links_md,
            "",
            "Unofficial mirror. Copyright belongs to Tencent.",
        ]
    )
    (DOWNLOAD_DIR / "RELEASE_NOTES.md").write_text(body + "\n", encoding="utf-8")
    write_github_output("body", body)

    if args.download_only and not args.publish:
        print("done (download-only)")
        return 0

    if release_exists(tag):
        print(f"release {tag} already exists, skip publish")
        write_github_output("skipped", "true")
        return 0

    title = f"QQ Windows {win_ver} / Linux {lin_ver}"
    print(f"creating release {tag} ...")
    create_release(tag, title, body, downloaded)
    write_github_output("skipped", "false")
    print("published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
