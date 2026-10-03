"""多地区菜单治理 的输入输出约定与身份目录。

身份目录（:class:`Directory`）是受信任的配置，由部署方在启动服务时注入，
不能由请求 payload 自带——否则任何门店都能在请求里声称自己是审核员。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_naive() -> datetime:
    """UTC 当前时间（朴素表示，与事件中的 ISO 时间串保持同一种时区语义）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# 角色常量
ROLE_MAINTAINER = "recipe_maintainer"   # 配方维护者：提交本地化版本
ROLE_REVIEWER = "nutrition_reviewer"    # 营养审核员：决定生效范围与时间
ROLE_STORE = "store"                    # 门店：只能领取所属市场已放行内容
ROLE_MANAGER = "regional_manager"       # 跨区域管理人员：按授权市场查询追溯
ROLES = (ROLE_MAINTAINER, ROLE_REVIEWER, ROLE_STORE, ROLE_MANAGER)


@dataclass(frozen=True)
class Request:
    actor: str
    action: str
    payload: dict[str, Any]
    request_id: str
    created_at: datetime = field(default_factory=utcnow_naive)


@dataclass
class Result:
    accepted: bool
    state: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


def validate_request(request: Request) -> None:
    if not request.actor or not request.action or not request.request_id:
        raise ValueError("请求缺少身份、动作或幂等键")
    if not isinstance(request.payload, dict):
        raise TypeError("请求数据必须是对象")


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    markets: tuple[str, ...] = ()

    @property
    def market(self) -> str:
        """门店只属于单一市场。"""
        if self.role != ROLE_STORE or len(self.markets) != 1:
            raise AttributeError("仅门店身份拥有唯一市场")
        return self.markets[0]


@dataclass(frozen=True)
class Directory:
    """actor_id -> Actor 的受信目录。"""
    actors: dict[str, Actor]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Directory":
        raw_actors = data.get("actors")
        if not isinstance(raw_actors, list):
            raise ValueError("身份目录必须包含 actors 列表")
        actors: dict[str, Actor] = {}
        for item in raw_actors:
            actor_id = str(item.get("id", "")).strip()
            role = str(item.get("role", "")).strip()
            if not actor_id or role not in ROLES:
                raise ValueError(f"非法身份条目：{item!r}")
            markets = tuple(str(m).strip() for m in item.get("markets", []) if str(m).strip())
            if role == ROLE_STORE and len(markets) != 1:
                raise ValueError(f"门店 {actor_id} 必须且只能归属一个市场")
            if actor_id in actors:
                raise ValueError(f"身份重复：{actor_id}")
            actors[actor_id] = Actor(actor_id, role, markets)
        return cls(actors)

    def get(self, actor_id: str) -> Actor | None:
        return self.actors.get(actor_id)
