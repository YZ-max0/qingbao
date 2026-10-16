"""合规抓取基类：统一 UA、robots 检查、随机延迟、重试。"""
from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from .util import DELAY_RANGE, USER_AGENT, polite_sleep, setup_logging

log = setup_logging("fetcher")

_robots_cache: dict[str, RobotFileParser | None] = {}


def robots_allows(url: str) -> bool:
    """检查 robots.txt。

    注意：站点常返回 200/404 的 HTML 错误页冒充 robots.txt，
    RobotFileParser 会把这类 HTML 内容误读成规则（实测 ccgp 就因此
    被误判为"禁止抓取"）。因此必须校验响应的 Content-Type 与正文，
    只有真正的文本型 robots.txt 才交给解析器判断。
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots_cache:
        _robots_cache[origin] = _load_robots(origin)
    rp = _robots_cache[origin]
    if rp is None:
        return True  # 无有效 robots.txt：公开页面按可抓处理
    return rp.can_fetch(USER_AGENT, url)


def _load_robots(origin: str) -> RobotFileParser | None:
    try:
        # 与主请求一致：不走系统代理（否则 robots 探测也会因代理失败，
        # 进而把"读取失败"误判成可抓，掩盖真实问题）
        sess = requests.Session()
        sess.trust_env = False
        try:
            resp = sess.get(
                f"{origin}/robots.txt",
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
        finally:
            sess.close()
        if resp.status_code != 200:
            log.info("%s 无 robots.txt (HTTP %d)，按可抓处理", origin, resp.status_code)
            return None
        ctype = (resp.headers.get("Content-Type") or "").lower()
        body = resp.text or ""
        # 错误页伪装成 robots.txt：内容类型是 HTML，或正文以 HTML 标签开头
        if "html" in ctype or body.lstrip()[:1] == "<":
            log.info("%s robots.txt 返回 HTML 错误页，按无 robots.txt 处理", origin)
            return None
        if not body.strip():
            return None

        rp = RobotFileParser()
        rp.parse(body.splitlines())
        return rp
    except Exception as exc:  # noqa: BLE001
        log.warning("robots.txt 读取失败，按可抓处理: %s (%s)", origin, exc)
        return None


class Fetcher:
    """带礼貌延迟的 HTTP 客户端。

    默认**忽略系统代理**（trust_env=False）。原因（实测 2026-10-16）：
        本机 Windows 系统代理指向 127.0.0.1:7897，该代理对部分国内站点
        会连接中断（中国机器人网、今天国际官网、工信部均出现过），
        且**时好时坏** —— 同一站点半小时前失败、之后又成功。
        结果是每日定时任务随机丢失几个源的数据，且不易察觉。

        实测 13 个采集源：走代理 3 个失败，直连 0 个失败。
        采集目标全是国内公开站点，直连更快也更稳，故默认直连。

    需要走代理的服务（如 Tavily 等国外 API）在自己的调用处显式设置，
    不要依赖这里。
    """

    def __init__(
        self,
        delay_range: tuple[float, float] = DELAY_RANGE,
        timeout: int = 20,
        max_retries: int = 2,
        respect_robots: bool = True,
        use_proxy: bool = False,
    ) -> None:
        self.delay_range = delay_range
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.session = requests.Session()
        # False = 不读系统代理设置（HTTP_PROXY / 注册表代理）
        self.session.trust_env = use_proxy
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    def get(self, url: str, *, params: dict | None = None) -> requests.Response | None:
        if self.respect_robots and not robots_allows(url):
            log.warning("robots.txt 禁止抓取，跳过: %s", url)
            return None

        for attempt in range(self.max_retries + 1):
            try:
                polite_sleep(self.delay_range)
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries:
                    log.error("抓取失败(%d次): %s | %s", attempt + 1, url, exc)
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def stream(self, url: str) -> bytes | None:
        resp = self.get(url)
        return resp.content if resp is not None else None
