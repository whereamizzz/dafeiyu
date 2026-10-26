"""分享卡片正文读取。

微信里的公众号文章/网页分享，数据库里只有一段 XML（标题 + 摘要 + 链接）。
这里负责把链接那头的正文尽量抠出来，给模型当上下文用。

原则：
- 只用 requests 抓一次，超时/失败一律返回空串，绝不抛异常；
- 结果按 URL 缓存 30 分钟，避免同一个链接反复抓；
- 只做纯文本，留 1200 字够模型看懂新闻大意了。
"""

from __future__ import annotations

import html
import re
import time

import requests

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 1800.0
_CACHE_MAX = 64
_TIMEOUT = 8.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI WindowsWechat"
)


def _decode(resp: requests.Response) -> str:
    head = (resp.content or b"")[:4096]
    match = re.search(rb'charset=["\']?([A-Za-z0-9_\-]+)', head)
    encodings = []
    if match:
        encodings.append(match.group(1).decode("ascii", "ignore"))
    encodings.extend(["utf-8", "gbk"])
    for enc in encodings:
        try:
            return (resp.content or b"").decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return (resp.content or b"").decode("utf-8", "replace")


def _clean(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<style.*?</style>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</(p|div|section|li|h\d)>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    fragment = fragment.replace("\u200b", "").replace("\xa0", " ")
    fragment = re.sub(r"[ \t\r\f\v]+", " ", fragment)
    fragment = re.sub(r"\n\s*\n+", "\n", fragment)
    return fragment.strip()


def _extract_body(raw: str) -> str:
    # 公众号正文在 <div id="js_content"> 里，后面紧跟脚本区。
    match = re.search(
        r'<div[^>]*id="js_content"[^>]*>(.*?)(?:<script|<div[^>]*id="js_pc_qr_code")',
        raw,
        re.S | re.I,
    )
    if not match:
        match = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', raw, re.S | re.I)
    if match:
        text = _clean(match.group(1))
        if len(text) >= 60:
            return text
    # 抓不到正文就退到摘要。
    match = re.search(
        r'<meta[^>]+(?:property|name)="(?:og:description|description)"[^>]+content="([^"]*)"',
        raw,
        re.I,
    )
    if match:
        return html.unescape(match.group(1)).strip()
    return ""


def read_share(url: str, max_chars: int = 1200) -> str:
    """抓取分享链接正文；失败返回空串。"""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    key = url
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1][:max_chars]

    try:
        resp = requests.get(
            url,
            timeout=_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
    except Exception:  # noqa: BLE001
        return ""
    if resp.status_code != 200:
        return ""
    try:
        text = _extract_body(_decode(resp))[:max_chars]
    except Exception:  # noqa: BLE001
        return ""
    if not text:
        return ""
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now, text)
    return text
