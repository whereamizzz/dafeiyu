"""语音转写：微信语音条（SILK）→ PCM → 文本。

两段：
1. `silk_to_wav`：用 silk-python 解码，手写 WAV 头（微信语音是 16k/16bit/单声道）；
2. `transcribe`：faster-whisper 本地模型，默认 base、int8、CPU，首次使用会下载模型。

设计取舍：
- 不用微信自带的「转文字」，那条路要模拟右键菜单，脆弱；
- 模型懒加载，没语音消息就不占内存；
- 转写失败不抛异常，返回空串，由上层决定「没听清」怎么处理。
"""

from __future__ import annotations

import os

# 模型从镜像站下载；禁用 xet 传输（CAS 服务对匿名请求会 401）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import struct
import wave
from pathlib import Path

from loguru import logger

_model = None
_model_key = ""


def _silk_to_wav(silk_path: str, wav_path: str) -> bool:
    try:
        import pysilk

        pcm_path = f"{wav_path}.pcm"
        with open(silk_path, "rb") as source, open(pcm_path, "wb") as sink:
            pysilk.decode(source, sink, 16000)
        raw = Path(pcm_path).read_bytes()
        with wave.open(wav_path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(raw)
        Path(pcm_path).unlink(missing_ok=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"SILK 解码失败: {exc}")
        return False


def _load_model(
    model_name: str,
    device: str,
    compute_type: str,
    endpoint: str = "",
    cpu_threads: int = 4,
):
    if endpoint:
        import os

        os.environ.setdefault("HF_ENDPOINT", endpoint)
    global _model, _model_key
    key = f"{model_name}|{device}|{compute_type}"
    if _model is not None and _model_key == key:
        return _model
    from faster_whisper import WhisperModel

    logger.info(f"加载语音模型 {model_name}（{device}/{compute_type}）")
    _model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=max(1, int(cpu_threads)),
    )
    _model_key = key
    return _model


def transcribe(silk_path: str, cfg) -> str:
    """SILK → 文本；失败返回空串。"""
    if not cfg.enabled:
        return ""
    wav_path = str(Path(silk_path).with_suffix(".wav"))
    if not _silk_to_wav(silk_path, wav_path):
        return ""
    try:
        model = _load_model(
            cfg.model, cfg.device, cfg.compute_type,
            getattr(cfg, "endpoint", ""),
            getattr(cfg, "cpu_threads", 4),
        )
        # 直接喂 float32 数组：绕开 PyAV 的版本兼容问题
        import numpy as np

        with wave.open(wav_path, "rb") as handle:
            frames = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if rate != 16000:
            audio = audio[:: max(1, rate // 16000)]
        segments, info = model.transcribe(
            audio,
            language=cfg.language or None,
            beam_size=int(getattr(cfg, "beam_size", 5)),
            vad_filter=False,
            initial_prompt=getattr(cfg, "initial_prompt", None) or None,
        )
        text = "".join(segment.text for segment in segments).strip()
        logger.info(f"语音转写（{info.language}）: {text[:40]}")
        return text
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"语音转写失败: {exc}")
        return ""
    finally:
        Path(wav_path).unlink(missing_ok=True)
