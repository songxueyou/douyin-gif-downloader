from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Page, Response, sync_playwright


sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# Final working Douyin chat GIF capture recipe, verified on 2026-05-25:
# 1. Restart Chrome first so the Douyin React chat component mounts cleanly.
# 2. Connect over CDP and call Network.setCacheDisabled before scrolling, otherwise
#    cached GIFs may not emit response events.
# 3. Use real mouse wheel events. Directly assigning scrollTop does not reliably
#    trigger Douyin's virtual-list history loader.
# 4. Date markers live in .MessageBoxTimetimeLayout. Do not scan arbitrary chat text
#    for stop boundaries.
# 5. Douyin displays dates within the last 7 days as weekday text. For the known-good
#    run, today=2026-05-25, target=2026-05-23 displayed as "前天", and the older
#    boundary 2026-05-22 displayed as "周五".
# 6. Poll date markers frequently around wheel events because virtual-list nodes can
#    appear briefly and then be recycled.
# 7. Known-good run: stopped at "周五"; 440 GIF URLs captured; 440 downloaded.

DEFAULT_CDP_URL = "http://127.0.0.1:10222"
DEFAULT_CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
DEFAULT_DOWNLOAD_DIR = Path("downloads")
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_WHEEL_DELTA_Y = -800
DEFAULT_WAIT_MS = 1500
DEFAULT_POLL_MS = 100
DEFAULT_PREHEAT_MAX_ROUNDS = 80
DEFAULT_CAPTURE_MAX_ROUNDS = 180

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@dataclass(frozen=True)
class Config:
    cdp_url: str
    chat_url: str
    friend_name: str | None
    target_date: date
    today: date
    download_dir: Path
    log_dir: Path
    wheel_delta_y: int
    wait_ms: int
    poll_ms: int
    preheat_max_rounds: int
    capture_max_rounds: int
    no_download: bool


@dataclass(frozen=True)
class GifHit:
    url: str
    ts: float


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def md_label(value: date) -> str:
    return f"{value.month:02d}/{value.day:02d}"


def m_d_label(value: date) -> str:
    return f"{value.month}/{value.day}"


def douyin_date_label(value: date, today: date) -> str:
    diff = (today - value).days
    if diff == 0:
        return "今天"
    if diff == 1:
        return "昨天"
    if diff == 2:
        return "前天"
    if 3 <= diff <= 6:
        return WEEKDAY_CN[value.weekday()]
    return md_label(value)


def labels_for_date(value: date, today: date) -> list[str]:
    labels = [douyin_date_label(value, today), md_label(value), m_d_label(value)]
    result: list[str] = []
    for label in labels:
        if label and label not in result:
            result.append(label)
    return result


def is_target_gif_response(response: Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "image/gif" not in content_type:
        return False
    lowered = response.url.lower()
    return "cover" not in lowered and "sc=cover" not in lowered and "biz_tag=pcweb_cover" not in lowered


def find_or_create_page(contexts, chat_url: str) -> Page:
    pages = [page for context in contexts for page in context.pages]
    for page in pages:
        if page.url.startswith(chat_url):
            return page
    if pages:
        page = pages[0]
        page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        return page
    context = contexts[0]
    page = context.new_page()
    page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    return page


def click_friend(page: Page, friend_name: str) -> dict:
    try:
        page.get_by_text(friend_name, exact=True).first.click(timeout=8000)
        page.wait_for_timeout(2000)
        return {"clicked": True, "method": "get_by_text"}
    except Exception:
        pass

    result = page.evaluate(
        """(name) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                const text = node.nodeValue || "";
                if (!text.includes(name)) continue;
                let el = node.parentElement;
                for (let i = 0; el && i < 10; i += 1, el = el.parentElement) {
                    const rect = el.getBoundingClientRect();
                    if (
                        rect.width > 50 &&
                        rect.height > 30 &&
                        rect.left < window.innerWidth * 0.55 &&
                        rect.top >= 0 &&
                        rect.bottom <= window.innerHeight
                    ) {
                        el.scrollIntoView({ block: "center", inline: "center" });
                        el.click();
                        return {
                            clicked: true,
                            method: "tree_walker",
                            targetClassName: String(el.className || ""),
                            targetText: (el.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 160),
                        };
                    }
                }
            }
            return {
                clicked: false,
                reason: "friend text not found",
                bodyText: (document.body.innerText || "").slice(0, 1000),
            };
        }""",
        friend_name,
    )
    page.wait_for_timeout(2000)
    if not result.get("clicked"):
        raise RuntimeError(f"未找到或无法点击好友：{friend_name}\n{result}")
    return result


def init_scroller(page: Page) -> dict:
    result = page.evaluate(
        """() => {
            const el = document.querySelector(".messageMessageListlist");
            if (!el) return null;
            window.__bqbMessageScroller = el;
            const rect = el.getBoundingClientRect();
            return {
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
                scrollTop: el.scrollTop || 0,
                scrollHeight: el.scrollHeight || 0,
                clientHeight: el.clientHeight || 0,
                className: String(el.className || ""),
            };
        }"""
    )
    if result is None:
        raise RuntimeError("未找到 messageMessageListlist。请确认已经进入目标好友聊天页。")
    return result


def reset_to_latest(page: Page) -> dict:
    result = page.evaluate(
        """() => {
            const el = window.__bqbMessageScroller;
            if (!el) return null;
            el.scrollTop = 0;
            el.dispatchEvent(new Event("scroll", { bubbles: true }));
            return {
                scrollTop: el.scrollTop || 0,
                scrollHeight: el.scrollHeight || 0,
                clientHeight: el.clientHeight || 0,
            };
        }"""
    )
    if result is None:
        raise RuntimeError("聊天滚动容器丢失")
    return result


def scroller_snapshot(page: Page, target_labels: list[str], stop_labels: list[str]) -> dict:
    return page.evaluate(
        """({ targetLabels, stopLabels }) => {
            const el = window.__bqbMessageScroller;
            if (!el) return null;
            const rootRect = el.getBoundingClientRect();
            const markerRows = [...document.querySelectorAll(".MessageBoxTimetimeLayout")]
                .map((node) => {
                    const rect = node.getBoundingClientRect();
                    const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
                    return {
                        text,
                        visible: rect.bottom >= rootRect.top && rect.top <= rootRect.bottom,
                        top: rect.top,
                    };
                })
                .filter((row) => row.text);
            const visibleMarkers = markerRows.filter((row) => row.visible);
            const hasAny = (text, labels) => labels.some((label) => text.includes(label));
            const targetText = visibleMarkers.find((row) => hasAny(row.text, targetLabels))?.text || null;
            const stopText = visibleMarkers.find((row) => hasAny(row.text, stopLabels))?.text || null;
            return {
                scrollTop: el.scrollTop || 0,
                scrollHeight: el.scrollHeight || 0,
                clientHeight: el.clientHeight || 0,
                targetVisible: Boolean(targetText),
                targetText,
                stopVisible: Boolean(stopText),
                stopText,
                visibleDateTexts: visibleMarkers.map((row) => row.text),
                allDateTexts: markerRows.map((row) => row.text),
            };
        }""",
        {"targetLabels": target_labels, "stopLabels": stop_labels},
    )


def wheel_and_poll(
    page: Page,
    x: float,
    y: float,
    config: Config,
    target_labels: list[str],
    stop_labels: list[str],
    stop_when_target_visible: bool = False,
    stop_when_boundary_visible: bool = True,
) -> dict:
    page.mouse.move(x, y)
    page.mouse.wheel(0, config.wheel_delta_y)
    deadline = time.monotonic() + config.wait_ms / 1000
    last_state = scroller_snapshot(page, target_labels, stop_labels)
    while time.monotonic() < deadline:
        page.wait_for_timeout(config.poll_ms)
        last_state = scroller_snapshot(page, target_labels, stop_labels)
        if stop_when_boundary_visible and last_state["stopVisible"]:
            return last_state
        if stop_when_target_visible and last_state["targetVisible"]:
            return last_state
    return last_state


def url_tail(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1] or "gif"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "gif"
    if not name.lower().endswith(".gif"):
        name += ".gif"
    return name


def download_once(url: str, output_path: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://www.douyin.com",
            "User-Agent": USER_AGENT,
            "Accept": "image/gif,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        output_path.write_bytes(response.read())


def download_with_retry(url: str, output_path: Path) -> str | None:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            download_once(url, output_path)
            return None
        except Exception as exc:
            last_error = exc
            if attempt == 1:
                print(f"下载失败，重试一次：{output_path.name} | {exc}", flush=True)
                time.sleep(1)
    return str(last_error)


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Capture Douyin chat GIFs for a target date.")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--chat-url", default=DEFAULT_CHAT_URL)
    parser.add_argument("--friend-name", help="目标好友昵称；如果当前已经在聊天页，可不填。")
    parser.add_argument("--target-date", required=True, help="目标日期，格式 YYYY-MM-DD。")
    parser.add_argument("--today", default=date.today().isoformat(), help="今天日期，格式 YYYY-MM-DD。")
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--wheel-delta-y", type=int, default=DEFAULT_WHEEL_DELTA_Y)
    parser.add_argument("--wait-ms", type=int, default=DEFAULT_WAIT_MS)
    parser.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    parser.add_argument("--preheat-max-rounds", type=int, default=DEFAULT_PREHEAT_MAX_ROUNDS)
    parser.add_argument("--capture-max-rounds", type=int, default=DEFAULT_CAPTURE_MAX_ROUNDS)
    parser.add_argument("--no-download", action="store_true", help="只写URL列表，不下载GIF。")
    args = parser.parse_args()
    return Config(
        cdp_url=args.cdp_url,
        chat_url=args.chat_url,
        friend_name=args.friend_name,
        target_date=parse_date(args.target_date),
        today=parse_date(args.today),
        download_dir=args.download_dir,
        log_dir=args.log_dir,
        wheel_delta_y=args.wheel_delta_y,
        wait_ms=args.wait_ms,
        poll_ms=args.poll_ms,
        preheat_max_rounds=args.preheat_max_rounds,
        capture_max_rounds=args.capture_max_rounds,
        no_download=args.no_download,
    )


def main() -> int:
    config = parse_args()
    boundary_date = config.target_date - timedelta(days=1)
    target_labels = labels_for_date(config.target_date, config.today)
    stop_labels = labels_for_date(boundary_date, config.today)
    gif_hits: dict[str, GifHit] = {}

    def handle_response(response: Response) -> None:
        try:
            if is_target_gif_response(response):
                gif_hits[response.url] = GifHit(url=response.url, ts=time.time() * 1000)
        except Exception as exc:
            print(f"响应监听异常，已跳过：{exc}", flush=True)

    print(
        json.dumps(
            {
                "targetDate": config.target_date.isoformat(),
                "today": config.today.isoformat(),
                "targetLabels": target_labels,
                "stopBoundaryDate": boundary_date.isoformat(),
                "stopLabels": stop_labels,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(config.cdp_url)
        page = find_or_create_page(browser.contexts, config.chat_url)
        page.bring_to_front()
        page.set_extra_http_headers({"Cache-Control": "no-cache", "Pragma": "no-cache"})

        client = page.context.new_cdp_session(page)
        client.send("Network.enable")
        client.send("Network.setCacheDisabled", {"cacheDisabled": True})
        page.on("response", handle_response)

        if config.friend_name:
            click_result = click_friend(page, config.friend_name)
            print(f"[好友] {click_result}", flush=True)

        scroller = init_scroller(page)
        print(f"[容器] {scroller}", flush=True)
        reset_state = reset_to_latest(page)
        print(f"[起点] 已设为最新位置：{reset_state}", flush=True)

        target_state = scroller_snapshot(page, target_labels, stop_labels)
        for round_num in range(1, config.preheat_max_rounds + 1):
            if target_state["targetVisible"]:
                break
            target_state = wheel_and_poll(
                page,
                scroller["x"],
                scroller["y"],
                config,
                target_labels,
                stop_labels,
                stop_when_target_visible=True,
                stop_when_boundary_visible=False,
            )
            if round_num % 10 == 0 or target_state["targetVisible"]:
                print(
                    f"[预热] 第{round_num}轮 | scrollTop={target_state['scrollTop']:.0f} | "
                    f"scrollHeight={target_state['scrollHeight']:.0f} | "
                    f"目标文本={target_state.get('targetText')} | 可见日期={target_state['visibleDateTexts']}",
                    flush=True,
                )
        if not target_state["targetVisible"]:
            raise RuntimeError(f"预热未找到目标日期标记：{target_labels}")

        gif_hits.clear()
        print("[正式] 已到目标日期，gif_hits 已清空，开始抓取；遇到停止边界即停止。", flush=True)

        final_state = target_state
        for round_num in range(1, config.capture_max_rounds + 1):
            final_state = wheel_and_poll(
                page,
                scroller["x"],
                scroller["y"],
                config,
                target_labels,
                stop_labels,
                stop_when_target_visible=False,
                stop_when_boundary_visible=True,
            )
            if round_num % 10 == 0 or final_state["stopVisible"]:
                print(
                    f"[正式] 第{round_num}轮 | GIF={len(gif_hits)} | "
                    f"scrollTop={final_state['scrollTop']:.0f} | "
                    f"scrollHeight={final_state['scrollHeight']:.0f} | "
                    f"停止文本={final_state.get('stopText')} | 可见日期={final_state['visibleDateTexts']}",
                    flush=True,
                )
            if final_state["stopVisible"]:
                print(f"[正式] 发现日期边界 {final_state.get('stopText')}，第{round_num}轮停止。", flush=True)
                break

        urls = [hit.url for hit in gif_hits.values()]
        final_state = scroller_snapshot(page, target_labels, stop_labels)

        config.log_dir.mkdir(parents=True, exist_ok=True)
        url_list_file = config.log_dir / "wheel_gif_urls.txt"
        failed_file = config.log_dir / "failed.txt"
        summary_file = config.log_dir / "summary.json"
        url_list_file.write_text("\n".join(urls), encoding="utf-8")

        success_count = 0
        failed: list[str] = []
        if not config.no_download:
            config.download_dir.mkdir(parents=True, exist_ok=True)
            for index, url in enumerate(urls, start=1):
                output_path = config.download_dir / f"{index:03d}_{url_tail(url)}"
                print(f"下载进度：{index}/{len(urls)} -> {output_path.name}", flush=True)
                error = download_with_retry(url, output_path)
                if error is None:
                    success_count += 1
                else:
                    failed.append(f"{index}\t{output_path.name}\t{url}\t{error}")
        failed_file.write_text("\n".join(failed), encoding="utf-8")

        summary = {
            "gifTotal": len(urls),
            "downloadSuccess": success_count,
            "downloadFailed": len(failed),
            "targetDate": config.target_date.isoformat(),
            "targetLabels": target_labels,
            "stopBoundaryDate": boundary_date.isoformat(),
            "stopLabels": stop_labels,
            "urlListFile": str(url_list_file),
            "failedFile": str(failed_file),
            "finalState": final_state,
        }
        summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
