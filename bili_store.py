"""插件本地持久化。

数据统一放在 ``data/plugin_data/<plugin_name>/`` 下，避免插件更新时被覆盖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class JsonStore:
    """极简 JSON 存储，读失败时回退到默认值，写失败不抛出。"""

    def __init__(self, plugin_name: str) -> None:
        self.dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def load(self, name: str, default: Any) -> Any:
        target = self.path(name)
        if not target.exists():
            return default
        try:
            with target.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        except (OSError, json.JSONDecodeError):
            return default

    def save(self, name: str, data: Any) -> None:
        target = self.path(name)
        tmp = target.with_name(target.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
        except OSError:
            pass

    def append(self, name: str, item: Any, limit: int = 500) -> None:
        data = self.load(name, [])
        if not isinstance(data, list):
            data = []
        data.append(item)
        self.save(name, data[-limit:])
