"""多地区菜单治理 的轻量本地调用入口。

用法：
    python3 -m app.api [db_path]
    echo '{"actor": "...", "action": "...", "payload": {...}, "request_id": "..."}' | python3 -m app.api data.db

标准输入为单个 JSON 对象，可选 created_at（ISO 格式）用于指定业务时间。
"""
import json
import os
import sys
from datetime import datetime
from .contracts import Request
from .service import MenuGovernanceService


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MENU_GOV_DB", "menu_governance.db")
    service = MenuGovernanceService(db_path)
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    item = json.loads(raw)
    created_raw = str(item.get("created_at", "")).strip()
    created_at = datetime.fromisoformat(created_raw) if created_raw else datetime.utcnow()
    request = Request(
        str(item.get("actor", "")),
        str(item.get("action", "")),
        dict(item.get("payload", {})),
        str(item.get("request_id", "")),
        created_at,
    )
    result = service.handle(request)
    print(
        json.dumps(
            {
                "accepted": result.accepted,
                "state": result.state,
                "message": result.message,
                "data": result.data,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
