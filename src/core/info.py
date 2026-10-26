"""天气与节日：按群记忆里的地区查天气，节日用本地日历。

- 天气：wttr.in 免费接口，不需要 key；查完即用即弃，不落库；
- 节日：本地静态表（含 2026-2028 年的农历节日），每年重复，不查网。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from loguru import logger

FIXED_HOLIDAYS = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (5, 1): "劳动节",
    (6, 1): "儿童节",
    (10, 1): "国庆节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}

# 农历节日按年固化，避免引入农历库
LUNAR_HOLIDAYS = {
    2026: {"春节": (2, 17), "元宵": (3, 3), "端午": (6, 19), "中秋": (9, 25)},
    2027: {"春节": (2, 6), "元宵": (2, 20), "端午": (6, 9), "中秋": (9, 15)},
    2028: {"春节": (1, 26), "元宵": (2, 9), "端午": (5, 28), "中秋": (10, 3)},
}

WEATHER_CODES = {
    "113": "晴",
    "116": "多云",
    "119": "阴",
    "122": "阴",
    "143": "雾",
    "176": "阵雨",
    "179": "阵雪",
    "182": "雨夹雪",
    "185": "冻雨",
    "200": "雷阵雨",
    "227": "吹雪",
    "230": "暴风雪",
    "248": "雾",
    "260": "冻雾",
    "263": "小雨",
    "266": "小雨",
    "281": "冻雨",
    "284": "冻雨",
    "293": "小雨",
    "296": "小雨",
    "299": "中雨",
    "302": "中雨",
    "305": "大雨",
    "308": "大雨",
    "311": "小雨",
    "314": "中雨",
    "317": "小雨",
    "320": "中雨",
    "323": "小雪",
    "326": "小雪",
    "329": "中雪",
    "332": "中雪",
    "335": "大雪",
    "338": "大雪",
    "350": "冰粒",
    "353": "阵雨",
    "356": "阵雨",
    "359": "暴雨",
    "362": "阵雨",
    "365": "阵雨",
    "368": "阵雪",
    "371": "阵雪",
    "374": "冰粒",
    "377": "冰粒",
    "386": "雷阵雨",
    "389": "雷阵雨",
    "392": "雷雪",
    "395": "雷雪",
}


class Weather:
    """查完即弃：不保存历史，避免过期数据混进上下文。"""

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout

    async def fetch(self, city: str) -> str:
        if not city:
            return ""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"https://wttr.in/{city}",
                    params={"format": "j1", "lang": "zh"},
                )
                response.raise_for_status()
                data = response.json()
            current = data["current_condition"][0]
            desc = WEATHER_CODES.get(str(current.get("weatherCode")), "")
            temp = current.get("temp_C", "")
            feels = current.get("FeelsLikeC", "")
            humidity = current.get("humidity", "")
            text = f"{city}现在 {desc}，{temp} 度（体感 {feels} 度，湿度 {humidity}%）"
            days = data.get("weather") or []
            if days:
                today = days[0]
                low = today.get("mintempC", "")
                high = today.get("maxtempC", "")
                text += f"，今天 {low}-{high} 度"
            return text
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"天气查询失败: {exc}")
            return ""


class Holidays:
    """本地节日日历：固定日期 + 固化农历。"""

    @staticmethod
    def upcoming(within_days: int = 7, today: date | None = None) -> str:
        now = today or date.today()
        found: list[tuple[int, str]] = []
        for year in (now.year, now.year + 1):
            for (month, day), name in FIXED_HOLIDAYS.items():
                try:
                    target = date(year, month, day)
                except ValueError:
                    continue
                delta = (target - now).days
                if 0 <= delta <= within_days:
                    found.append((delta, name))
            for name, (month, day) in LUNAR_HOLIDAYS.get(year, {}).items():
                try:
                    target = date(year, month, day)
                except ValueError:
                    continue
                delta = (target - now).days
                if 0 <= delta <= within_days:
                    found.append((delta, name))
        if not found:
            return ""
        found.sort()
        parts = []
        for delta, name in found[:2]:
            when = "今天" if delta == 0 else f"{delta} 天后"
            parts.append(f"{when}是{name}")
        return "，".join(parts)

    @staticmethod
    def today_is() -> str:
        now = date.today()
        for (month, day), name in FIXED_HOLIDAYS.items():
            if (month, day) == (now.month, now.day):
                return name
        for name, (month, day) in LUNAR_HOLIDAYS.get(now.year, {}).items():
            if (month, day) == (now.month, now.day):
                return name
        return ""
