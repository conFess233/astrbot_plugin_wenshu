"""仅保存有界执行摘要，原子替换保护旧文件。"""

import asyncio
import json
import os
from pathlib import Path

from astrbot.api import logger


class Records:
    def __init__(self, directory: Path):
        self.path = directory / "runs.json"
        self.lock = asyncio.Lock()
        self.items = []

    async def load(self):
        if self.path.exists():
            try:
                data = await asyncio.to_thread(
                    self.path.read_text, encoding="utf-8-sig"
                )
                rows = json.loads(data)
                if not isinstance(rows, list):
                    raise TypeError("invalid records")
                self.items = rows[:1000]
            except (OSError, ValueError, TypeError):
                logger.warning("文枢：摘要读取失败，保留原文件，改用恢复记录文件")
                self.path = self.path.with_name("runs-recovered.json")

    def _write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump(items, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    async def add(self, item, limit):
        async with self.lock:
            items = ([item] + self.items)[:limit] if limit else []
            try:
                await asyncio.to_thread(self._write, items)
                self.items = items
            except OSError:
                logger.warning("文枢：摘要保存失败，旧记录保留")
