"""多地区菜单治理的本地调用入口。

请求以单个 JSON 对象从 stdin 读入：
    {"actor": "reviewer-sg", "action": "review_recipe",
     "payload": {...}, "request_id": "req-001"}

运行配置（环境变量）：
- MENU_DIRECTORY：身份目录 JSON 文件路径，格式 {"actors": [{"id","role","markets"}]}
- MENU_DB：SQLite 文件路径；缺省时使用内存库（进程结束即丢失，仅用于试用）
"""
import json
import os
import sys

from .contracts import Directory, Request, utcnow_naive
from .service import MenuRegistryService
from .storage import SqliteEventStore


def build_service() -> MenuRegistryService:
    directory = Directory({})
    directory_path = os.environ.get("MENU_DIRECTORY")
    if directory_path:
        with open(directory_path, "r", encoding="utf-8") as fh:
            directory = Directory.from_dict(json.load(fh))
    store = None
    db_path = os.environ.get("MENU_DB")
    if db_path:
        store = SqliteEventStore(db_path)
    return MenuRegistryService(directory=directory, store=store)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    item = json.loads(raw)
    request = Request(
        str(item.get("actor", "")),
        str(item.get("action", "")),
        dict(item.get("payload", {})),
        str(item.get("request_id", "")),
        utcnow_naive(),
    )
    service = build_service()
    result = service.handle(request)
    print(json.dumps({
        "accepted": result.accepted,
        "state": result.state,
        "message": result.message,
        "data": result.data,
    }, ensure_ascii=False))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
