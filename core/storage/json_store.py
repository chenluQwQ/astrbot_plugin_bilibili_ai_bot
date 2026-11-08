"""插件对象上原来那三个 json 读写方法：每次按路径现建 ``JsonFile``。

过渡用。新代码直接拿自己的 ``JsonFile``；还在按路径读写的老调用方经 ``bot.json_store``。
"""
from __future__ import annotations

from .json_file import JsonFile


class JsonStore:
    def _load_json(self, path, default=None):
        return JsonFile(path).read(default)

    def _save_json(self, path, data):
        JsonFile(path).write(data)

    def _append_json_list(self, path, entry, cap=None):
        """读最新数据→追加→写回，整个过程持有该文件的锁，并发任务不会互相覆盖。"""
        with JsonFile(path).locked():
            data = self._load_json(path, [])
            if not isinstance(data, list):
                data = []
            data.append(entry)
            if cap:
                data = data[-cap:]
            self._save_json(path, data)
            return data
