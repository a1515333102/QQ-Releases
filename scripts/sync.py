#!/usr/bin/env python3
"""Fetch official QQ Windows/Linux installers and optionally publish a GitHub Release."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
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

# Same endpoint as im.qq.com SPA / NapCat UrlSign anti-hotlink.
URL_SIGN_API = (
    "https://im.qq.com/http2rpc/gotrpc/noauth/trpc.qqntv2.urlsign.UrlSign/GetSign"
)
URL_SIGN_OIDB = '{"uint32_command":"0x9b8e","uint32_service_type":1}'

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def log(msg: str, *, err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def make_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # Seed cookies used by UrlSign / CDN.
    log("warm-up: https://im.qq.com/index/ ...")
    try:
        opener.open(
            urllib.request.Request(
                "https://im.qq.com/index/",
                headers={"User-Agent": UA, "Accept": "text/html,*/*"},
            ),
            timeout=30,
        )
        log("warm-up: ok")
    except Exception as exc:  # noqa: BLE001
        log(f"warn: warm-up im.qq.com failed: {exc}", err=True)
    return opener


def http_get(opener: urllib.request.OpenerDirector, url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": "https://im.qq.com/",
        },
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(opener: urllib.request.OpenerDirector, urls: list[str]) -> str:
    last_err: Exception | None = None
    for url in urls:
        try:
            log(f"GET {url}")
            return http_get(opener, url).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"warn: failed {url}: {exc}", err=True)
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

    for key in ("ntDownloadX64Url", "ntDownloadUrl", "ntDownloadARMUrl"):
        url = win.get(key)
        if isinstance(url, str) and url.startswith("http"):
            items.append((Path(url.split("?", 1)[0]).name, url))

    def add_map(obj: object) -> None:
        if isinstance(obj, str) and obj.startswith("http"):
            items.append((Path(obj.split("?", 1)[0]).name, obj))
            return
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, str) and v.startswith("http"):
                    items.append((Path(v.split("?", 1)[0]).name, v))

    add_map(lin.get("x64DownloadUrl"))
    add_map(lin.get("armDownloadUrl"))
    add_map(lin.get("loongarchDownloadUrl"))
    add_map(lin.get("mipsDownloadUrl"))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, url in items:
        if name in seen:
            continue
        seen.add(name)
        unique.append((name, url))

    return win_ver, lin_ver, unique


def url_needs_sign(url: str) -> bool:
    lower = url.lower()
    return (
        "qqdl.gtimg.cn" in lower
        or "qqntv2" in lower
        or "gtimg.cn/qqfile" in lower
    )


def sign_download_url(opener: urllib.request.OpenerDirector, raw_url: str) -> str:
    """Exchange a bare CDN URL for a time-limited signed URL."""
    log("  UrlSign ...")
    req = urllib.request.Request(
        URL_SIGN_API,
        data=json.dumps({"url": raw_url}).encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "x-oidb": URL_SIGN_OIDB,
            "Origin": "https://im.qq.com",
            "Referer": "https://im.qq.com/index/",
        },
        method="POST",
    )
    with opener.open(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))

    retcode = payload.get("retcode", -1)
    if retcode != 0:
        msg = (
            (payload.get("error") or {}).get("message")
            or payload.get("message")
            or "unknown"
        )
        raise RuntimeError(f"UrlSign retcode={retcode}: {msg}")

    signed = ((payload.get("data") or {}).get("url") or "").strip()
    if not signed:
        raise RuntimeError("UrlSign returned empty url")
    log("  signed ok")
    return signed


def prepare_download_url(opener: urllib.request.OpenerDirector, url: str) -> str:
    if url_needs_sign(url):
        return sign_download_url(opener, url)
    return url


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _download_with_curl(url: str, tmp: Path) -> None:
    """Prefer curl: resume, connect timeout, abort on stalled transfer."""
    # Abort if slower than 8 KiB/s for 90s (common hang after UrlSign on GH runners).
    cmd = [
        "curl",
        "-fL",
        "--connect-timeout",
        "30",
        "--retry",
        "2",
        "--retry-delay",
        "3",
        "--speed-limit",
        "8192",
        "--speed-time",
        "90",
        "-A",
        UA,
        "-H",
        "Referer: https://im.qq.com/",
        "-H",
        "Origin: https://im.qq.com",
        "-C",
        "-",
        "-o",
        str(tmp),
        "--",
        url,
    ]
    log(f"  CDN via curl (resume={tmp.exists() and tmp.stat().st_size or 0} bytes) ...")
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit {proc.returncode}")


def _download_with_urllib(
    opener: urllib.request.OpenerDirector, url: str, tmp: Path
) -> None:
    existing = tmp.stat().st_size if tmp.exists() else 0
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": "https://im.qq.com/",
        "Origin": "https://im.qq.com",
    }
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
        log(f"  CDN via urllib resume from {existing} bytes ...")
    else:
        log("  CDN via urllib ...")

    req = urllib.request.Request(url, headers=headers)
    # Stall timeout: 120s without socket progress (connect + read).
    with opener.open(req, timeout=120) as resp:
        resumed = bool(existing and getattr(resp, "status", None) == 206)
        with open(tmp, "ab" if resumed else "wb") as f:
            if not resumed:
                existing = 0
            total_hdr = resp.headers.get("Content-Length")
            content_range = resp.headers.get("Content-Range") or ""
            total_mb = None
            if content_range and "/" in content_range:
                whole = content_range.rsplit("/", 1)[-1]
                if whole.isdigit():
                    total_mb = int(whole) / (1024 * 1024)
            elif total_hdr and total_hdr.isdigit():
                total_mb = (existing + int(total_hdr)) / (1024 * 1024)

            written = existing
            last_report = existing
            last_beat = time.monotonic()
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                now = time.monotonic()
                if written - last_report >= 25 * 1024 * 1024 or now - last_beat >= 30:
                    last_report = written
                    last_beat = now
                    done_mb = written / (1024 * 1024)
                    if total_mb is not None:
                        log(f"  ... {done_mb:.0f}/{total_mb:.0f} MiB")
                    else:
                        log(f"  ... {done_mb:.0f} MiB")


def download_file(opener: urllib.request.OpenerDirector, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"skip existing: {dest.name}")
        return

    log(f"download: {dest.name}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    max_attempts = 4
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            # Re-sign every attempt: signed CDN URLs expire and stall often.
            download_url = prepare_download_url(opener, url)
            if _curl_available():
                _download_with_curl(download_url, tmp)
            else:
                _download_with_urllib(opener, download_url, tmp)
            if not tmp.exists() or tmp.stat().st_size <= 0:
                raise RuntimeError("empty download")
            tmp.replace(dest)
            size_mb = dest.stat().st_size / (1024 * 1024)
            log(f"  saved {size_mb:.1f} MiB")
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"  attempt {attempt}/{max_attempts} failed: {exc}", err=True)
            # Keep .part for resume; drop it only on HTTP hard failures.
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 404):
                tmp.unlink(missing_ok=True)
            if attempt < max_attempts:
                time.sleep(min(5 * attempt, 20))

    raise RuntimeError(f"download failed for {dest.name}: {last_err}") from last_err


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

    opener = make_opener()

    log("fetch pcConfig.json ...")
    pc = json.loads(fetch_text(opener, PC_CONFIG_URLS))
    log("fetch linuxConfig.js ...")
    linux = parse_linux_config_js(fetch_text(opener, LINUX_CONFIG_URLS))

    win_ver, lin_ver, items = collect_urls(pc, linux)
    if not items:
        log("error: no download URLs found", err=True)
        return 1

    tag = f"{win_ver}+{lin_ver}"
    log(f"Windows: {win_ver}")
    log(f"Linux:   {lin_ver}")
    log(f"Tag:     {tag}")
    log(f"Files:   {len(items)}")
    for name, url in items:
        log(f"  - {name}")
        log(f"    {url}")

    write_github_output("tag", tag)
    write_github_output("win_version", win_ver)
    write_github_output("linux_version", lin_ver)

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
    write_github_output("body", body)

    # Skip expensive CDN downloads when the release already exists.
    if args.publish:
        log(f"check existing release: {tag}")
        if release_exists(tag):
            log(f"release {tag} already exists, skip publish")
            write_github_output("skipped", "true")
            return 0

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (DOWNLOAD_DIR / "RELEASE_NOTES.md").write_text(body + "\n", encoding="utf-8")
    downloaded: list[Path] = []
    for i, (name, url) in enumerate(items, start=1):
        log(f"[{i}/{len(items)}] {name}")
        dest = DOWNLOAD_DIR / name
        download_file(opener, url, dest)
        downloaded.append(dest)

    if args.download_only and not args.publish:
        log("done (download-only)")
        return 0

    title = f"QQ Windows {win_ver} / Linux {lin_ver}"
    total_mb = sum(p.stat().st_size for p in downloaded) / (1024 * 1024)
    log(f"creating release {tag} ({len(downloaded)} files, {total_mb:.0f} MiB) ...")
    create_release(tag, title, body, downloaded)
    write_github_output("skipped", "false")
    log("published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
