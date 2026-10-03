"""多地区菜单治理：配方版本、本地审核、市场放行、紧急回滚与历史追溯。

角色与权限：
- maintainer        配方维护者，提交本地化版本（submit_version）
- reviewer          营养审核人员，审核并决定生效市场与时间（review_version），
                    执行发布（publish）与紧急回滚（rollback）
- store             门店，只能领取所属市场已放行内容（claim）
- regional_manager  跨区域管理人员，只能查询/追溯获授权市场（query / trace）
- admin             登记操作者（register_actor）

防泄露约定：审核中（submitted）与已驳回（rejected）的版本内容只对审核人员和
提交者本人可见；门店领取只经过 releases 放行指针，永远读不到未放行内容。
"""
from __future__ import annotations

import json
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Optional

from .contracts import Request, Result, validate_request
from .db import connect

ROLE_MAINTAINER = "maintainer"
ROLE_REVIEWER = "reviewer"
ROLE_STORE = "store"
ROLE_MANAGER = "regional_manager"
ROLE_ADMIN = "admin"
ROLES = {ROLE_MAINTAINER, ROLE_REVIEWER, ROLE_STORE, ROLE_MANAGER, ROLE_ADMIN}

STATUS_SUBMITTED = "submitted"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

EVENT_PUBLISH = "publish"
EVENT_ROLLBACK = "rollback"


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _validate_content(content: Any) -> dict:
    """校验配方内容：名称、营养等级、原料清单（每项含名称与来源）。"""
    if not isinstance(content, dict):
        raise ValueError("配方内容必须是对象")
    if not str(content.get("name", "")).strip():
        raise ValueError("配方缺少名称")
    if not str(content.get("nutrition_grade", "")).strip():
        raise ValueError("配方缺少营养等级")
    ingredients = content.get("ingredients")
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("配方缺少原料清单")
    for item in ingredients:
        if (
            not isinstance(item, dict)
            or not str(item.get("name", "")).strip()
            or not str(item.get("source", "")).strip()
        ):
            raise ValueError("每项原料需包含名称与来源")
    return content


class MenuGovernanceService:
    """多地区菜单治理服务，数据保存在调用方提供的 SQLite 文件中。"""

    def __init__(
        self,
        db_path: str = ":memory:",
        actors: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        self._conn = connect(db_path)
        self._lock = RLock()
        self._actions: dict[str, Callable[[Request], Result]] = {
            "register_actor": self._register_actor,
            "submit_version": self._submit_version,
            "review_version": self._review_version,
            "publish": self._publish,
            "claim": self._claim,
            "rollback": self._rollback,
            "query": self._query,
            "trace": self._trace,
        }
        for name, info in (actors or {}).items():
            self._conn.execute(
                "INSERT OR REPLACE INTO actors (actor, role, markets) VALUES (?, ?, ?)",
                (name, str(info["role"]), json.dumps(list(info.get("markets", [])))),
            )
        self._conn.commit()

    # ---- 入口与幂等 ----

    def handle(self, request: Request) -> Result:
        validate_request(request)
        with self._lock:
            cached = self._conn.execute(
                "SELECT response FROM requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if cached is not None:
                return self._decode_result(cached["response"])
            handler = self._actions.get(request.action)
            if handler is None:
                result = Result(False, "unknown", f"未知动作: {request.action}")
            else:
                try:
                    result = handler(request)
                except ValueError as exc:
                    self._conn.rollback()
                    result = Result(False, "invalid", str(exc))
            self._conn.execute(
                "INSERT OR IGNORE INTO requests (request_id, response) VALUES (?, ?)",
                (request.request_id, self._encode_result(result)),
            )
            self._conn.commit()
            return result

    @staticmethod
    def _encode_result(result: Result) -> str:
        return json.dumps(
            {
                "accepted": result.accepted,
                "state": result.state,
                "message": result.message,
                "data": result.data,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _decode_result(raw: str) -> Result:
        item = json.loads(raw)
        return Result(item["accepted"], item["state"], item["message"], item["data"])

    # ---- 通用助手 ----

    def _actor(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT actor, role, markets FROM actors WHERE actor = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return {"actor": row["actor"], "role": row["role"], "markets": json.loads(row["markets"])}

    def _require(self, request: Request, roles: set[str]) -> tuple[Optional[dict], Optional[Result]]:
        actor = self._actor(request.actor)
        if actor is None:
            return None, Result(False, "forbidden", "未登记的操作者")
        if actor["role"] not in roles:
            return None, Result(
                False, "forbidden", f"角色 {actor['role']} 无权执行 {request.action}"
            )
        return actor, None

    def _version(self, version_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _approval(self, version_id: str, market: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE version_id = ? AND market = ?",
            (version_id, market),
        ).fetchone()
        return dict(row) if row is not None else None

    def _release(self, recipe_id: str, market: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM releases WHERE recipe_id = ? AND market = ?",
            (recipe_id, market),
        ).fetchone()
        return dict(row) if row is not None else None

    def _latest_event_kind(self, recipe_id: str, market: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT kind FROM release_events WHERE recipe_id = ? AND market = ? "
            "ORDER BY id DESC LIMIT 1",
            (recipe_id, market),
        ).fetchone()
        return row["kind"] if row is not None else None

    # ---- 动作 ----

    def _register_actor(self, request: Request) -> Result:
        _, err = self._require(request, {ROLE_ADMIN})
        if err:
            return err
        name = str(request.payload.get("actor", "")).strip()
        role = str(request.payload.get("role", "")).strip()
        markets = request.payload.get("markets", [])
        if not name:
            raise ValueError("缺少操作者名称")
        if role not in ROLES:
            raise ValueError(f"未知角色: {role}")
        if not isinstance(markets, list):
            raise ValueError("markets 必须是数组")
        self._conn.execute(
            "INSERT OR REPLACE INTO actors (actor, role, markets) VALUES (?, ?, ?)",
            (name, role, json.dumps([str(m) for m in markets])),
        )
        return Result(True, "registered", "操作者已登记", {"actor": name, "role": role})

    def _submit_version(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_MAINTAINER})
        if err:
            return err
        recipe_id = str(request.payload.get("recipe_id", "")).strip()
        market = str(request.payload.get("market", "")).strip()
        if not recipe_id or not market:
            raise ValueError("缺少配方标识或目标市场")
        content = _validate_content(request.payload.get("content"))
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS n FROM versions WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        version_no = row["n"] + 1
        version_id = f"{recipe_id}@v{version_no}"
        self._conn.execute(
            "INSERT INTO versions (version_id, recipe_id, version_no, market, content, "
            "status, submitted_by, submitted_at, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                recipe_id,
                version_no,
                market,
                json.dumps(content, ensure_ascii=False),
                STATUS_SUBMITTED,
                actor["actor"],
                _iso(request.created_at),
                request.request_id,
            ),
        )
        return Result(
            True,
            STATUS_SUBMITTED,
            "版本已提交，等待营养审核",
            {
                "recipe_id": recipe_id,
                "version_id": version_id,
                "version_no": version_no,
                "market": market,
            },
        )

    def _review_version(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_REVIEWER})
        if err:
            return err
        version_id = str(request.payload.get("version_id", "")).strip()
        decision = str(request.payload.get("decision", "")).strip()
        reason = str(request.payload.get("reason", "")).strip()
        version = self._version(version_id)
        if version is None:
            return Result(False, "missing", "版本不存在")
        if not reason:
            raise ValueError("审核必须填写理由，供后续追溯")
        if decision == "reject":
            if version["status"] == STATUS_APPROVED:
                return Result(False, version["status"], "已审核通过的版本不能驳回")
            self._conn.execute(
                "UPDATE versions SET status = ? WHERE version_id = ?",
                (STATUS_REJECTED, version_id),
            )
            return Result(True, STATUS_REJECTED, "版本已驳回", {"version_id": version_id})
        if decision != "approve":
            raise ValueError("decision 必须是 approve 或 reject")
        markets = [str(m).strip() for m in request.payload.get("markets", []) if str(m).strip()]
        if not markets:
            raise ValueError("需指定生效市场范围")
        effective_from_raw = str(request.payload.get("effective_from", "")).strip()
        if not effective_from_raw:
            raise ValueError("需指定生效开始时间")
        effective_from = _parse(effective_from_raw)
        effective_to_raw = str(request.payload.get("effective_to", "") or "").strip()
        effective_to = _parse(effective_to_raw) if effective_to_raw else None
        if effective_to is not None and effective_to <= effective_from:
            raise ValueError("生效结束时间必须晚于开始时间")
        approved, skipped = [], []
        for market in markets:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO approvals (version_id, market, reviewer, reason, "
                "effective_from, effective_to, created_at, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    market,
                    actor["actor"],
                    reason,
                    _iso(effective_from),
                    _iso(effective_to) if effective_to else None,
                    _iso(request.created_at),
                    request.request_id,
                ),
            )
            (approved if cursor.rowcount else skipped).append(market)
        self._conn.execute(
            "UPDATE versions SET status = ? WHERE version_id = ?",
            (STATUS_APPROVED, version_id),
        )
        return Result(
            True,
            STATUS_APPROVED,
            "版本已审核通过",
            {
                "version_id": version_id,
                "approved_markets": approved,
                "already_approved_markets": skipped,
                "effective_from": _iso(effective_from),
                "effective_to": _iso(effective_to) if effective_to else None,
            },
        )

    def _publish(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_REVIEWER})
        if err:
            return err
        recipe_id = str(request.payload.get("recipe_id", "")).strip()
        market = str(request.payload.get("market", "")).strip()
        version_id = str(request.payload.get("version_id", "")).strip()
        version = self._version(version_id)
        if version is None or version["recipe_id"] != recipe_id:
            return Result(False, "missing", "版本不存在")
        if version["status"] != STATUS_APPROVED:
            return Result(False, version["status"], "版本未通过审核，不能发布")
        if self._approval(version_id, market) is None:
            return Result(False, "unapproved", "该版本未获此市场放行")
        current = self._release(recipe_id, market)
        if current is not None and current["version_id"] == version_id:
            return Result(
                True,
                "released",
                "该版本已在此市场放行，未产生重复记录",
                {"recipe_id": recipe_id, "market": market, "version_id": version_id},
            )
        self._conn.execute(
            "INSERT INTO releases (recipe_id, market, version_id, released_by, released_at, "
            "request_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (recipe_id, market) DO UPDATE SET version_id = excluded.version_id, "
            "released_by = excluded.released_by, released_at = excluded.released_at, "
            "request_id = excluded.request_id",
            (recipe_id, market, version_id, actor["actor"], _iso(request.created_at), request.request_id),
        )
        self._conn.execute(
            "INSERT INTO release_events (recipe_id, market, version_id, kind, actor, reason, "
            "request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recipe_id,
                market,
                version_id,
                EVENT_PUBLISH,
                actor["actor"],
                str(request.payload.get("reason", "")),
                request.request_id,
                _iso(request.created_at),
            ),
        )
        return Result(
            True,
            "released",
            "版本已放行到市场",
            {"recipe_id": recipe_id, "market": market, "version_id": version_id},
        )

    def _claim(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_STORE})
        if err:
            return err
        recipe_id = str(request.payload.get("recipe_id", "")).strip()
        market = str(request.payload.get("market", "")).strip()
        if not market and len(actor["markets"]) == 1:
            market = actor["markets"][0]
        if not recipe_id or not market:
            raise ValueError("缺少配方标识或市场")
        if market not in actor["markets"]:
            return Result(False, "forbidden", "门店无权领取其他市场的内容")
        release = self._release(recipe_id, market)
        if release is None:
            return Result(False, "unreleased", "该市场尚未放行此配方")
        version = self._version(release["version_id"])
        approval = self._approval(release["version_id"], market)
        if version is None or approval is None:
            return Result(False, "unreleased", "该市场尚未放行此配方")
        # 回滚产生的指针以回滚指令为准；正常发布的内容必须在审核生效窗口内。
        if self._latest_event_kind(recipe_id, market) != EVENT_ROLLBACK:
            now = request.created_at
            if now < _parse(approval["effective_from"]):
                return Result(False, "pending", "版本尚未到生效时间")
            if approval["effective_to"] and now > _parse(approval["effective_to"]):
                return Result(False, "expired", "版本已过生效期")
        content = json.loads(version["content"])
        return Result(
            True,
            "released",
            "领取成功",
            {
                "recipe_id": recipe_id,
                "market": market,
                "version_id": version["version_id"],
                "version_no": version["version_no"],
                "content": content,
            },
        )

    def _rollback(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_REVIEWER})
        if err:
            return err
        recipe_id = str(request.payload.get("recipe_id", "")).strip()
        market = str(request.payload.get("market", "")).strip()
        to_version_id = str(request.payload.get("to_version_id", "")).strip()
        reason = str(request.payload.get("reason", "")).strip()
        if not reason:
            raise ValueError("紧急回滚必须填写原因")
        version = self._version(to_version_id)
        if version is None or version["recipe_id"] != recipe_id:
            return Result(False, "missing", "指定版本不存在")
        if self._approval(to_version_id, market) is None:
            return Result(False, "unapproved", "指定版本未曾在此市场获放行，不能回滚到它")
        current = self._release(recipe_id, market)
        if current is None:
            return Result(False, "unreleased", "该市场尚无放行记录，无法回滚")
        if current["version_id"] == to_version_id:
            return Result(
                True,
                "released",
                "市场已处于指定版本，未产生重复记录",
                {"recipe_id": recipe_id, "market": market, "version_id": to_version_id},
            )
        self._conn.execute(
            "UPDATE releases SET version_id = ?, released_by = ?, released_at = ?, "
            "request_id = ? WHERE recipe_id = ? AND market = ?",
            (to_version_id, actor["actor"], _iso(request.created_at), request.request_id, recipe_id, market),
        )
        self._conn.execute(
            "INSERT INTO release_events (recipe_id, market, version_id, kind, actor, reason, "
            "request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (recipe_id, market, to_version_id, EVENT_ROLLBACK, actor["actor"], reason, request.request_id, _iso(request.created_at)),
        )
        return Result(
            True,
            "rolled_back",
            "已紧急回滚，新领取将回到指定版本",
            {"recipe_id": recipe_id, "market": market, "version_id": to_version_id},
        )

    def _query(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_REVIEWER, ROLE_MANAGER, ROLE_MAINTAINER})
        if err:
            return err
        requested = [str(m).strip() for m in request.payload.get("markets", []) if str(m).strip()]
        if actor["role"] == ROLE_MANAGER:
            allowed = set(actor["markets"])
            markets = [m for m in (requested or sorted(allowed)) if m in allowed]
        elif requested:
            markets = requested
        else:
            markets = [
                r["market"]
                for r in self._conn.execute("SELECT DISTINCT market FROM versions ORDER BY market")
            ]
        result_markets: dict[str, Any] = {}
        for market in markets:
            versions = []
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE market = ? ORDER BY recipe_id, version_no",
                (market,),
            ).fetchall()
            for row in rows:
                if actor["role"] == ROLE_REVIEWER:
                    visible, with_content = True, True
                elif actor["role"] == ROLE_MANAGER:
                    # 审核中的资料不向管理人员泄露，只能看到已通过的版本
                    visible = row["status"] == STATUS_APPROVED
                    with_content = visible
                else:  # maintainer 只能看到自己提交的版本
                    visible = row["submitted_by"] == actor["actor"]
                    with_content = visible
                if not visible:
                    continue
                item = {
                    "version_id": row["version_id"],
                    "recipe_id": row["recipe_id"],
                    "version_no": row["version_no"],
                    "status": row["status"],
                    "submitted_by": row["submitted_by"],
                    "submitted_at": row["submitted_at"],
                }
                if with_content:
                    item["content"] = json.loads(row["content"])
                versions.append(item)
            releases = []
            for row in self._conn.execute(
                "SELECT * FROM releases WHERE market = ? ORDER BY recipe_id", (market,)
            ).fetchall():
                releases.append(
                    {
                        "recipe_id": row["recipe_id"],
                        "version_id": row["version_id"],
                        "released_by": row["released_by"],
                        "released_at": row["released_at"],
                        "last_event": self._latest_event_kind(row["recipe_id"], market),
                    }
                )
            result_markets[market] = {"versions": versions, "releases": releases}
        return Result(True, "ok", "查询完成", {"markets": result_markets})

    def _trace(self, request: Request) -> Result:
        actor, err = self._require(request, {ROLE_REVIEWER, ROLE_MANAGER})
        if err:
            return err
        recipe_id = str(request.payload.get("recipe_id", "")).strip()
        market = str(request.payload.get("market", "")).strip()
        if not recipe_id or not market:
            raise ValueError("缺少配方标识或市场")
        if actor["role"] == ROLE_MANAGER and market not in actor["markets"]:
            return Result(False, "forbidden", "无权追溯未获授权的市场")
        events = self._conn.execute(
            "SELECT * FROM release_events WHERE recipe_id = ? AND market = ? ORDER BY id",
            (recipe_id, market),
        ).fetchall()
        history = []
        for event in events:
            version = self._version(event["version_id"])
            approval = self._approval(event["version_id"], market)
            content = json.loads(version["content"]) if version else {}
            history.append(
                {
                    "event": event["kind"],
                    "actor": event["actor"],
                    "at": event["created_at"],
                    "reason": event["reason"],
                    "version_id": event["version_id"],
                    "version_no": version["version_no"] if version else None,
                    "nutrition_grade": content.get("nutrition_grade"),
                    "ingredients": content.get("ingredients", []),
                    "approval": (
                        {
                            "reviewer": approval["reviewer"],
                            "reason": approval["reason"],
                            "effective_from": approval["effective_from"],
                            "effective_to": approval["effective_to"],
                        }
                        if approval
                        else None
                    ),
                }
            )
        return Result(
            True,
            "ok",
            "追溯完成",
            {"recipe_id": recipe_id, "market": market, "history": history},
        )
