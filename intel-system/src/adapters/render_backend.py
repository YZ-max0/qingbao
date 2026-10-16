"""可选浏览器渲染后端 —— 解决 JS 动态渲染的政府/企业站点。

仅在需要时导入；未安装 playwright 时 policy_adapter 会给出明确提示。
"""
from __future__ import annotations

from ..common.util import setup_logging

log = setup_logging("render")


def render_html(url: str, wait_selector: str | None = None, timeout: int = 30000) -> str:
    """用 Chromium 渲染页面并返回 HTML。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page.goto(url, timeout=timeout, wait_until="networkidle")
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout)
            html = page.content()
            log.info("渲染完成: %s (%d bytes)", url, len(html))
            return html
        finally:
            browser.close()
