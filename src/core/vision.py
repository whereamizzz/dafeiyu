"""识图：把微信图片交给视觉模型描述成一句话。

支持三家（都走 OpenAI 兼容格式）：
- zhipu：智谱 glm-4v-flash，免费
- qwen：阿里 qwen-vl-plus，便宜且准
- custom：自填 base_url 和模型名

设计取舍：只让它「描述」，不让它参与判定；
描述结果当普通文本进流程，和语音转写一个位置。
"""

from __future__ import annotations

import base64
import io

import httpx
from loguru import logger

PROVIDERS = {
    "zhipu": {
        "label": "智谱 glm-4v-flash（免费）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4v-flash",
    },
    "qwen": {
        "label": "通义 qwen-vl-plus（便宜）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-vl-plus",
    },
    "custom": {"label": "自定义", "base_url": "", "model": ""},
}

DEFAULT_PROMPT = (
    "用一句中文说清这张图里有什么：如果是表情包，说明它表达的情绪；"
    "如果是截图，说清是什么界面、关键内容。不要评价，不要展开。"
)


def _prepare(path: str) -> str:
    from PIL import Image

    image = Image.open(path).convert("RGB")
    longest = max(image.width, image.height)
    if longest > 1024:
        scale = 1024 / longest
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82)
    return base64.b64encode(buffer.getvalue()).decode()


def resolve(cfg) -> tuple[str, str, str]:
    provider = str(getattr(cfg, "provider", "zhipu") or "zhipu")
    info = PROVIDERS.get(provider, PROVIDERS["zhipu"])
    base_url = getattr(cfg, "base_url", "") or info["base_url"]
    model = getattr(cfg, "model", "") or info["model"]
    return base_url, model, info["label"]


async def describe(path: str, cfg) -> str:
    if not getattr(cfg, "enabled", False):
        return ""
    base_url, model, label = resolve(cfg)
    key = str(getattr(cfg, "api_key", "") or "")
    if not (base_url and model and key):
        logger.warning(f"识图未配置完整（{label}），跳过")
        return ""
    try:
        data = _prepare(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"图片处理失败: {exc}")
        return ""

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": getattr(cfg, "prompt", "") or DEFAULT_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{data}"},
                    },
                ],
            }
        ],
        "temperature": 0.2,
        "max_tokens": int(getattr(cfg, "max_tokens", 120)),
    }
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=float(getattr(cfg, "timeout", 30))) as client:
            response = await client.post(base_url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        text = str(body["choices"][0]["message"]["content"] or "").strip()
        logger.info(f"识图（{label}）: {text[:40]}")
        return text[: int(getattr(cfg, "max_chars", 80))]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"识图失败（{label}）: {exc}")
        return ""
