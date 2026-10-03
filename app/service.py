"""多地区菜单治理领域服务。

治理规则一览：
- 配方维护者按市场提交本地化版本；内容指纹不变不得再占一个版本号。
- 营养审核员一次性决定：批准/驳回、生效市场范围（不得超出授权市场）、生效时间、
  紧急回滚点，以及逐项原料的许可依据（rationale）。
- 门店只能领取本市场"当前指针"指向的已放行版本：待审核/被驳回不可见，
  未到生效时间不可见，紧急回滚后新领取立刻回到指定版本。
- 跨区域管理人员只能查询获授权市场；待审核资料对其脱敏；
  历史版本与逐原料许可依据可完整追溯。
- 所有写操作携带 request_id，存储层唯一约束保证重复发布不会产生第二份记录。

指针解析：每个 (配方, 市场) 维护一条按决策时间排列的指令链（release/rollback）。
release 到点才推进，rollback 立即推进；由于回滚在链上晚于它所撤销的发布，
线性遍历即可保证回滚压过此前一切（包括先于回滚排定、后于回滚到点的定时发布）。
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .contracts import (
    Actor, Directory, Request, Result,
    ROLE_MAINTAINER, ROLE_REVIEWER, ROLE_STORE, ROLE_MANAGER,
)
from .storage import Event, EventStore, IdempotencyConflict, InMemoryEventStore

# 版本生命周期
ST_PENDING = "pending_review"   # 审核中：资料保密，仅提交者与审核员可见
ST_RELEASED = "released"        # 已放行（生效范围与时间见决策快照）
ST_REJECTED = "rejected"        # 已驳回

VERSION_STATES = (ST_PENDING, ST_RELEASED, ST_REJECTED)


def _parse_dt(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{field_name} 必须是 ISO 8601 时间字符串")


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def content_hash(content: dict[str, Any]) -> str:
    """对配方内容做规范哈希：相同内容（与键顺序无关）得到相同指纹。"""
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reject(message: str, state: str = "rejected", data: dict[str, Any] | None = None) -> Result:
    return Result(False, state, message, data or {})


@dataclass
class RecipeVersion:
    recipe_id: str
    market: str                       # 提交（源）市场
    version_no: int
    content: dict[str, Any]
    content_hash: str
    submitted_by: str
    submitted_at: datetime
    parent_version: int | None
    status: str = ST_PENDING
    decision: dict[str, Any] | None = None  # 审核决策快照（不可变，含 reviewed_by）

    def public_content(self) -> dict[str, Any]:
        """门店可见的内容视图。"""
        return {
            "name": self.content.get("name"),
            "nutrition_grade": self.content.get("nutrition_grade"),
            "ingredients": list(self.content.get("ingredients", [])),
            "preparation": self.content.get("preparation"),
        }


@dataclass
class Directive:
    """市场级放行指针上的一条指令，按决策顺序排列。"""
    kind: str                 # "release" | "rollback"
    source_market: str        # 版本所属（提交）市场
    version_no: int
    decided_at: datetime
    effective_at: datetime | None = None   # release 才有：到点才生效
    actor: str = ""
    reason: str = ""

    @property
    def pointer(self) -> tuple[str, int]:
        return self.source_market, self.version_no


@dataclass
class _Projection:
    # 版本按 (配方, 源市场) 归集
    versions: dict[tuple[str, str], list[RecipeVersion]] = field(default_factory=dict)
    # 指令按 (配方, 领取市场) 归集；跨市场放行时源市场与领取市场不同
    directives: dict[tuple[str, str], list[Directive]] = field(default_factory=dict)


class MenuRegistryService:
    def __init__(self, directory: Directory | None = None,
                 store: EventStore | None = None) -> None:
        self.directory = directory or Directory({})
        self.store = store or InMemoryEventStore()
        self._state = _Projection()
        self.store.replay(self._apply)

    # ------------------------------------------------------------------ 事件重放

    def _apply(self, event: Event) -> None:
        p = event.payload
        if event.event_type == "recipe_submitted":
            version = RecipeVersion(
                recipe_id=p["recipe_id"], market=p["market"],
                version_no=p["version_no"], content=p["content"],
                content_hash=p["content_hash"], submitted_by=event.actor,
                submitted_at=datetime.fromisoformat(p["submitted_at"]),
                parent_version=p.get("parent_version"),
            )
            self._state.versions.setdefault((version.recipe_id, version.market), []).append(version)
        elif event.event_type == "recipe_reviewed":
            version = self._find_version(p["recipe_id"], p["market"], p["version_no"])
            version.status = ST_RELEASED if p["decision"] == "approved" else ST_REJECTED
            version.decision = dict(p, reviewed_by=event.actor)
            if version.status == ST_RELEASED:
                for served_market in p["release_markets"]:
                    self._state.directives.setdefault(
                        (p["recipe_id"], served_market), []
                    ).append(Directive(
                        kind="release", source_market=p["market"],
                        version_no=p["version_no"],
                        decided_at=datetime.fromisoformat(p["decided_at"]),
                        effective_at=datetime.fromisoformat(p["effective_at"]),
                        actor=event.actor,
                    ))
        elif event.event_type == "release_rolled_back":
            self._state.directives[(p["recipe_id"], p["market"])].append(Directive(
                kind="rollback", source_market=p["source_market"],
                version_no=p["target_version"],
                decided_at=datetime.fromisoformat(p["rolled_back_at"]),
                effective_at=datetime.fromisoformat(p["rolled_back_at"]),
                actor=event.actor, reason=p.get("reason", ""),
            ))
        else:
            raise ValueError(f"未知事件类型：{event.event_type}")

    # ------------------------------------------------------------------ 入口

    def handle(self, request: Request) -> Result:
        from .contracts import validate_request
        validate_request(request)

        cached = self.store.idem_result(request.request_id)
        if cached is not None:
            return Result(**cached)

        actor = self.directory.get(request.actor)
        if actor is None:
            return _reject("身份未登记", "unauthorized")

        handler = {
            "submit_recipe": self._submit_recipe,
            "review_recipe": self._review_recipe,
            "rollback": self._rollback,
            "fetch_menu": self._fetch_menu,
            "review_queue": self._review_queue,
            "my_submissions": self._my_submissions,
            "recipe_history": self._recipe_history,
            "trace_ingredient": self._trace_ingredient,
        }.get(request.action)
        if handler is None:
            return _reject(f"未知动作：{request.action}", "unknown_action")

        try:
            result = handler(actor, request.payload, request.created_at)
        except (ValueError, TypeError) as exc:
            return _reject(str(exc), "invalid_request")

        event_type = getattr(result, "_event_type", None)
        event_payload = getattr(result, "_event_payload", None)
        if result.accepted and event_type is not None:
            try:
                event = self.store.append(event_type, request.actor, event_payload,
                                          request.request_id, request.created_at)
            except IdempotencyConflict:
                existing = self.store.idem_result(request.request_id)
                if existing is not None:
                    return Result(**existing)
                return _reject("相同请求正在处理中", "in_flight")
            self._apply(event)
            self.store.save_result(request.request_id, {
                "accepted": result.accepted, "state": result.state,
                "message": result.message, "data": result.data,
            })
        return result

    # ------------------------------------------------------------------ 提交

    @staticmethod
    def _validate_content(content: Any) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise ValueError("缺少配方内容 content")
        name = str(content.get("name", "")).strip()
        grade = str(content.get("nutrition_grade", "")).strip()
        if not name:
            raise ValueError("配方名称 name 不能为空")
        if not grade:
            raise ValueError("营养等级 nutrition_grade 不能为空")
        ingredients = content.get("ingredients", [])
        if not isinstance(ingredients, list) or not ingredients:
            raise ValueError("ingredients 必须是非空列表")
        norm_ingredients = []
        seen = set()
        for item in ingredients:
            if not isinstance(item, dict):
                raise ValueError("每项原料必须是对象")
            ing_id = str(item.get("id", "")).strip()
            ing_name = str(item.get("name", "")).strip()
            origin = str(item.get("origin", "")).strip()
            if not ing_id or not ing_name:
                raise ValueError("原料必须包含 id 与 name")
            if not origin:
                raise ValueError(f"原料 {ing_id} 缺少来源 origin")
            if ing_id in seen:
                raise ValueError(f"原料重复：{ing_id}")
            seen.add(ing_id)
            norm_ingredients.append({
                "id": ing_id, "name": ing_name, "origin": origin,
                "notes": str(item.get("notes", "")),
            })
        preparation = str(content.get("preparation", "")).strip()
        if not preparation:
            raise ValueError("调配说明 preparation 不能为空")
        return {
            "name": name, "nutrition_grade": grade,
            "ingredients": norm_ingredients, "preparation": preparation,
            "changelog": str(content.get("changelog", "")),
        }

    def _submit_recipe(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role != ROLE_MAINTAINER:
            return _reject("只有配方维护者可以提交本地化版本", "forbidden")
        recipe_id = str(payload.get("recipe_id", "")).strip()
        market = str(payload.get("market", "")).strip()
        if not recipe_id or not market:
            raise ValueError("必须提供 recipe_id 与 market")
        if actor.markets and market not in actor.markets:
            return _reject(f"无权为市场 {market} 提交配方", "forbidden")
        content = self._validate_content(payload.get("content"))

        key = (recipe_id, market)
        versions = self._state.versions.get(key, [])
        latest = versions[-1] if versions else None
        if latest is not None and latest.status == ST_PENDING:
            return _reject(
                f"版本 v{latest.version_no} 尚在审核，不能重复提交",
                ST_PENDING, {"recipe_id": recipe_id, "market": market,
                             "blocking_version": latest.version_no},
            )
        fingerprint = content_hash(content)
        for previous in versions:
            if previous.content_hash == fingerprint:
                # 重复发布不能制造第二份记录：同内容直接回到既有版本。
                return Result(
                    False, previous.status,
                    f"内容与版本 v{previous.version_no} 完全相同，未生成新版本",
                    {"recipe_id": recipe_id, "market": market,
                     "version_no": previous.version_no, "status": previous.status,
                     "content_hash": fingerprint},
                )

        version_no = (latest.version_no + 1) if latest else 1
        event_payload = {
            "recipe_id": recipe_id, "market": market, "version_no": version_no,
            "content": content, "content_hash": fingerprint,
            "submitted_at": _dt(now),
            "parent_version": latest.version_no if latest else None,
        }
        result = Result(
            True, ST_PENDING,
            f"配方 {recipe_id}@{market} v{version_no} 已提交，等待营养审核",
            {"recipe_id": recipe_id, "market": market, "version_no": version_no,
             "status": ST_PENDING, "content_hash": fingerprint,
             "parent_version": event_payload["parent_version"]},
        )
        result._event_type = "recipe_submitted"  # type: ignore[attr-defined]
        result._event_payload = event_payload    # type: ignore[attr-defined]
        return result

    # ------------------------------------------------------------------ 审核

    def _review_recipe(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role != ROLE_REVIEWER:
            return _reject("只有营养审核员可以作出放行决定", "forbidden")
        recipe_id = str(payload.get("recipe_id", "")).strip()
        market = str(payload.get("market", "")).strip()
        try:
            version_no = int(payload.get("version_no"))
        except (TypeError, ValueError):
            raise ValueError("version_no 必须是整数")
        version = self._find_version_or_none(recipe_id, market, version_no)
        if version is None:
            return _reject("待审核版本不存在", "not_found")
        if market not in actor.markets:
            return _reject(f"无权审核市场 {market} 的版本", "forbidden")
        if version.status != ST_PENDING:
            return _reject(
                f"版本 v{version_no} 已作出过决定（{version.status}），不能重复审核",
                version.status,
                {"recipe_id": recipe_id, "market": market, "version_no": version_no},
            )

        decision = str(payload.get("decision", "")).strip()
        if decision not in ("approved", "rejected"):
            raise ValueError("decision 必须是 approved 或 rejected")
        review_notes = str(payload.get("review_notes", "")).strip()

        if decision == "rejected":
            event_payload = {
                "recipe_id": recipe_id, "market": market, "version_no": version_no,
                "decision": "rejected", "decided_at": _dt(now),
                "review_notes": review_notes, "ingredients": [],
            }
            result = Result(
                True, ST_REJECTED, f"版本 v{version_no} 已驳回",
                {"recipe_id": recipe_id, "market": market,
                 "version_no": version_no, "status": ST_REJECTED},
            )
            result._event_type = "recipe_reviewed"  # type: ignore[attr-defined]
            result._event_payload = event_payload    # type: ignore[attr-defined]
            return result

        # ---- 批准：校验生效范围、时间、逐原料依据 --------------------
        release_markets = self._validated_release_markets(payload, actor, market)
        effective_at = _parse_dt(payload.get("effective_at"), "effective_at")
        if effective_at < now:
            return _reject("生效时间不能早于当前时间", "invalid_schedule")
        ingredient_decisions = self._validated_ingredient_decisions(payload, version)

        # 预置紧急回滚点：必须是源市场上一线已放行版本（未指定则取当前生效版本）。
        rollback_version = payload.get("rollback_version")
        if rollback_version is not None:
            try:
                rollback_version = int(rollback_version)
            except (TypeError, ValueError):
                raise ValueError("rollback_version 必须是整数")
            target = self._find_version_or_none(recipe_id, market, rollback_version)
            if target is None or not self._released_into(recipe_id, market,
                                                         market, rollback_version):
                return _reject(
                    f"回滚点 v{rollback_version} 不是 {market} 已放行版本",
                    "invalid_rollback_target",
                )
        else:
            pointer = self._resolve_pointer(recipe_id, market, now)
            rollback_version = pointer[1] if pointer and pointer[0] == market else None

        event_payload = {
            "recipe_id": recipe_id, "market": market, "version_no": version_no,
            "decision": "approved", "decided_at": _dt(now),
            "release_markets": list(release_markets),
            "effective_at": _dt(effective_at),
            "rollback_version": rollback_version,
            "ingredients": ingredient_decisions,
            "review_notes": review_notes,
        }
        result = Result(
            True, ST_RELEASED,
            f"版本 v{version_no} 已批准，将于 {event_payload['effective_at']} 在 "
            f"{','.join(release_markets)} 生效",
            {"recipe_id": recipe_id, "submitted_for_market": market,
             "version_no": version_no, "status": ST_RELEASED,
             "release_markets": list(release_markets),
             "effective_at": event_payload["effective_at"],
             "rollback_version": rollback_version},
        )
        result._event_type = "recipe_reviewed"  # type: ignore[attr-defined]
        result._event_payload = event_payload    # type: ignore[attr-defined]
        return result

    @staticmethod
    def _validated_release_markets(payload: dict, actor: Actor,
                                   local_market: str) -> tuple[str, ...]:
        raw = payload.get("release_markets")
        if not isinstance(raw, list) or not raw:
            raise ValueError("release_markets 必须是非空市场列表")
        markets = tuple(dict.fromkeys(str(m).strip() for m in raw if str(m).strip()))
        if local_market not in markets:
            raise ValueError("生效范围必须包含版本所属本地市场")
        overreach = [m for m in markets if m not in actor.markets]
        if overreach:
            raise ValueError(f"生效范围超出审核员授权市场：{','.join(overreach)}")
        return markets

    @staticmethod
    def _validated_ingredient_decisions(payload: dict,
                                        version: RecipeVersion) -> list[dict[str, Any]]:
        raw = payload.get("ingredients")
        if not isinstance(raw, list):
            raise ValueError("批准必须附带逐原料许可决定 ingredients")
        decisions: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("原料许可决定必须是对象")
            ing_id = str(item.get("id", "")).strip()
            rationale = str(item.get("rationale", "")).strip()
            approved = bool(item.get("approved"))
            if not ing_id:
                raise ValueError("原料许可决定缺少 id")
            if not rationale:
                raise ValueError(f"原料 {ing_id} 缺少许可依据 rationale")
            decisions[ing_id] = {"id": ing_id, "approved": approved, "rationale": rationale}
        required = {ing["id"] for ing in version.content["ingredients"]}
        missing = required - set(decisions)
        if missing:
            raise ValueError(f"以下原料缺少许可依据：{','.join(sorted(missing))}")
        disallowed = [d["id"] for d in decisions.values()
                      if d["id"] in required and not d["approved"]]
        if disallowed:
            raise ValueError(f"原料未获许可，不能批准版本：{','.join(sorted(disallowed))}")
        return [decisions[ing["id"]] for ing in version.content["ingredients"]]

    # ------------------------------------------------------------------ 回滚

    def _rollback(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role != ROLE_REVIEWER:
            return _reject("只有营养审核员可以执行紧急回滚", "forbidden")
        recipe_id = str(payload.get("recipe_id", "")).strip()
        market = str(payload.get("market", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if not recipe_id or not market:
            raise ValueError("必须提供 recipe_id 与 market")
        if market not in actor.markets:
            return _reject(f"无权回滚市场 {market}", "forbidden")
        try:
            target_no = int(payload.get("target_version"))
        except (TypeError, ValueError):
            raise ValueError("target_version 必须是整数")
        source_market = str(payload.get("source_market", market)).strip() or market
        if not self._released_into(recipe_id, market, source_market, target_no):
            return _reject(
                f"v{target_no}（来源市场 {source_market}）未在 {market} 放行",
                "invalid_rollback_target",
            )
        pointer = self._resolve_pointer(recipe_id, market, now)
        if pointer is None:
            return _reject("该市场尚无生效版本，无需回滚", "nothing_active")
        if pointer == (source_market, target_no):
            return _reject(
                f"当前领取版本已是 v{target_no}", "already_on_target",
                {"recipe_id": recipe_id, "market": market,
                 "served_version": target_no, "source_market": source_market},
            )

        event_payload = {
            "recipe_id": recipe_id, "market": market,
            "source_market": source_market, "target_version": target_no,
            "reason": reason, "rolled_back_at": _dt(now),
        }
        result = Result(
            True, "rolled_back",
            f"{market} 的 {recipe_id} 已紧急回滚至 v{target_no}，新领取立即生效",
            {"recipe_id": recipe_id, "market": market,
             "served_version": target_no, "source_market": source_market,
             "reason": reason},
        )
        result._event_type = "release_rolled_back"  # type: ignore[attr-defined]
        result._event_payload = event_payload       # type: ignore[attr-defined]
        return result

    # ------------------------------------------------------------------ 指针解析

    def _resolve_pointer(self, recipe_id: str, market: str,
                         now: datetime) -> tuple[str, int] | None:
        """线性遍历指令链，返回 (源市场, 版本号)。"""
        current = None
        for d in self._state.directives.get((recipe_id, market), []):
            if d.kind == "rollback":
                current = d.pointer
            elif d.effective_at is not None and d.effective_at <= now:
                current = d.pointer
        return current

    def _resolve_directive(self, recipe_id: str, market: str,
                           now: datetime) -> Directive | None:
        """返回让当前指针落定的那条指令（用于展示生效时间/回滚原因）。"""
        pointer = self._resolve_pointer(recipe_id, market, now)
        if pointer is None:
            return None
        for d in reversed(self._state.directives.get((recipe_id, market), [])):
            if d.pointer != pointer:
                continue
            if d.kind == "rollback":
                return d
            if d.effective_at is not None and d.effective_at <= now:
                return d
        return None

    def _released_into(self, recipe_id: str, served_market: str,
                       source_market: str, version_no: int) -> bool:
        """该版本是否经某条 release 指令放行进指定市场。"""
        for d in self._state.directives.get((recipe_id, served_market), []):
            if d.kind == "release" and d.pointer == (source_market, version_no):
                version = self._find_version_or_none(recipe_id, source_market, version_no)
                if version is not None and version.status == ST_RELEASED:
                    return True
        return False

    # ------------------------------------------------------------------ 门店领取

    def _market_recipes(self, market: str) -> set[str]:
        recipe_ids = {r for (r, m) in self._state.directives if m == market}
        recipe_ids.update(r for (r, m) in self._state.versions if m == market)
        return recipe_ids

    def _fetch_menu(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role != ROLE_STORE:
            return _reject("只有门店身份可以领取菜单", "forbidden")
        market = actor.market
        want_recipe = str(payload.get("recipe_id", "")).strip()
        items = []
        for recipe_id in sorted(self._market_recipes(market)):
            if want_recipe and recipe_id != want_recipe:
                continue
            pointer = self._resolve_pointer(recipe_id, market, now)
            if pointer is None:
                continue   # 待审核、被驳回、未到生效时间：一律不可见
            source_market, served_no = pointer
            version = self._find_version_or_none(recipe_id, source_market, served_no)
            if version is None:
                continue
            directive = self._resolve_directive(recipe_id, market, now)
            items.append({
                "recipe_id": recipe_id, "market": market,
                "source_market": source_market, "version_no": served_no,
                "served_via": directive.kind if directive else "release",
                "effective_at": _dt(directive.effective_at) if directive else None,
                **version.public_content(),
            })
        return Result(True, ST_RELEASED, f"市场 {market} 可领取 {len(items)} 款配方",
                      {"market": market, "items": items})

    # ------------------------------------------------------------------ 审核员视图

    def _review_queue(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role != ROLE_REVIEWER:
            return _reject("只有营养审核员可以查看待审队列", "forbidden")
        queue = []
        for (recipe_id, market), versions in self._state.versions.items():
            if market not in actor.markets:
                continue
            latest = versions[-1]
            if latest.status == ST_PENDING:
                queue.append({
                    "recipe_id": recipe_id, "market": market,
                    "version_no": latest.version_no,
                    "submitted_by": latest.submitted_by,
                    "submitted_at": _dt(latest.submitted_at),
                    "parent_version": latest.parent_version,
                    "content": latest.content,   # 审核员需要看资料才能审
                })
        queue.sort(key=lambda x: (x["market"], x["submitted_at"], x["recipe_id"]))
        return Result(True, ST_PENDING, f"待审核 {len(queue)} 项", {"items": queue})

    # ------------------------------------------------------------------ 维护者视图

    def _my_submissions(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role != ROLE_MAINTAINER:
            return _reject("只有配方维护者可以查看本人提交", "forbidden")
        rows = []
        for (recipe_id, market), versions in self._state.versions.items():
            if actor.markets and market not in actor.markets:
                continue
            for version in versions:
                if version.submitted_by != actor.actor_id:
                    continue
                row = {
                    "recipe_id": recipe_id, "market": market,
                    "version_no": version.version_no, "status": version.status,
                    "submitted_at": _dt(version.submitted_at),
                }
                if version.decision is not None:
                    row["decided_at"] = version.decision["decided_at"]
                    row["decided_by"] = version.decision.get("reviewed_by", "")
                    if version.status == ST_RELEASED:
                        row["release_markets"] = version.decision["release_markets"]
                        row["effective_at"] = version.decision["effective_at"]
                    else:
                        row["review_notes"] = version.decision.get("review_notes", "")
                rows.append(row)
        rows.sort(key=lambda x: (x["submitted_at"], x["recipe_id"], x["version_no"]))
        return Result(True, "ok", f"共提交 {len(rows)} 个版本", {"items": rows})

    # ------------------------------------------------------------------ 跨区追溯

    @staticmethod
    def _authorized_markets(actor: Actor, requested: Any) -> list[str]:
        if not isinstance(requested, list) or not requested:
            raise ValueError("markets 必须是非空市场列表")
        asked = [str(m).strip() for m in requested if str(m).strip()]
        return [m for m in dict.fromkeys(asked) if m in actor.markets]

    def _recipe_history(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role not in (ROLE_MANAGER, ROLE_REVIEWER):
            return _reject("跨区追溯仅对管理人员与审核员开放", "forbidden")
        recipe_id = str(payload.get("recipe_id", "")).strip()
        if not recipe_id:
            raise ValueError("必须提供 recipe_id")
        markets = self._authorized_markets(actor, payload.get("markets"))
        if not markets:
            return _reject("查询市场均不在授权范围内", "forbidden")

        market_views = []
        for market in markets:
            # 本地提交线：待审核版本脱敏
            submissions = []
            for version in self._state.versions.get((recipe_id, market), []):
                entry: dict[str, Any] = {
                    "version_no": version.version_no,
                    "status": version.status,
                    "submitted_by": version.submitted_by,
                    "submitted_at": _dt(version.submitted_at),
                    "parent_version": version.parent_version,
                }
                if version.status == ST_PENDING:
                    entry["note"] = "审核中，资料保密"
                elif version.status == ST_RELEASED:
                    decision = version.decision or {}
                    entry["content"] = version.public_content()
                    entry["release"] = {
                        "release_markets": decision.get("release_markets", []),
                        "effective_at": decision.get("effective_at"),
                        "decided_at": decision.get("decided_at"),
                        "reviewed_by": decision.get("reviewed_by", ""),
                        "review_notes": decision.get("review_notes", ""),
                    }
                else:
                    entry["decided_at"] = (version.decision or {}).get("decided_at")
                    entry["review_notes"] = (version.decision or {}).get("review_notes", "")
                submissions.append(entry)

            # 市场放行指令线：每次门店为何领到这个版本，含跨市场放行来源
            channel = []
            for d in self._state.directives.get((recipe_id, market), []):
                version = self._find_version_or_none(recipe_id, d.source_market, d.version_no)
                item = {
                    "kind": d.kind, "source_market": d.source_market,
                    "version_no": d.version_no, "actor": d.actor,
                    "decided_at": _dt(d.decided_at), "effective_at": _dt(d.effective_at),
                    "reason": d.reason,
                }
                if d.kind == "release" and version is not None:
                    item["nutrition_grade"] = version.content.get("nutrition_grade")
                channel.append(item)

            market_views.append({
                "market": market,
                "served_version": list(self._resolve_pointer(recipe_id, market, now) or []),
                "submissions": submissions,
                "channel": channel,
            })
        return Result(True, "ok", f"配方 {recipe_id} 在 {len(market_views)} 个授权市场的历史",
                      {"recipe_id": recipe_id, "markets": market_views})

    def _trace_ingredient(self, actor: Actor, payload: dict, now: datetime) -> Result:
        if actor.role not in (ROLE_MANAGER, ROLE_REVIEWER, ROLE_MAINTAINER):
            return _reject("该身份不允许原料追溯", "forbidden")
        recipe_id = str(payload.get("recipe_id", "")).strip()
        market = str(payload.get("market", "")).strip()
        ingredient_id = str(payload.get("ingredient_id", "")).strip()
        if not recipe_id or not market or not ingredient_id:
            raise ValueError("必须提供 recipe_id、market 与 ingredient_id")
        if actor.role in (ROLE_MANAGER, ROLE_REVIEWER) and market not in actor.markets:
            return _reject(f"市场 {market} 不在授权范围内", "forbidden")

        owns_any = any(
            v.submitted_by == actor.actor_id
            for v in self._state.versions.get((recipe_id, market), [])
        )
        if actor.role == ROLE_MAINTAINER and not owns_any:
            return _reject("只能追溯本人提交过的配方", "forbidden")

        # 沿该市场的 release 指令链取证：每次放行都是"当时为何允许"的独立证据。
        trail = []
        seen = set()
        for d in self._state.directives.get((recipe_id, market), []):
            if d.kind != "release":
                continue
            version = self._find_version_or_none(recipe_id, d.source_market, d.version_no)
            if version is None or version.status != ST_RELEASED:
                continue
            ingredient = next(
                (ing for ing in version.content["ingredients"] if ing["id"] == ingredient_id),
                None,
            )
            if ingredient is None:
                continue
            approval = next(
                (a for a in version.decision["ingredients"] if a["id"] == ingredient_id), None
            )
            evidence_id = (d.source_market, d.version_no, d.decided_at)
            if evidence_id in seen:
                continue   # 同一批准放行进多市场时，在本市场只产生一条指令，此处防御去重
            seen.add(evidence_id)
            trail.append({
                "market": market, "source_market": d.source_market,
                "version_no": d.version_no,
                "ingredient": ingredient,
                "allowed": True,
                "allowed_by": version.decision.get("reviewed_by", ""),
                "decided_at": version.decision["decided_at"],
                "effective_at": version.decision["effective_at"],
                "rationale": approval["rationale"] if approval else None,
            })
        trail.sort(key=lambda x: (x["decided_at"], x["version_no"]))
        return Result(
            True, "ok",
            f"原料 {ingredient_id} 在 {market} 有 {len(trail)} 段放行依据",
            {"recipe_id": recipe_id, "market": market,
             "ingredient_id": ingredient_id, "trail": trail},
        )

    # ------------------------------------------------------------------ 辅助

    def _find_version(self, recipe_id: str, market: str, version_no: int) -> RecipeVersion:
        found = self._find_version_or_none(recipe_id, market, version_no)
        if found is None:
            raise ValueError(f"版本不存在：{recipe_id}@{market} v{version_no}")
        return found

    def _find_version_or_none(self, recipe_id: str, market: str,
                              version_no: int) -> RecipeVersion | None:
        for version in self._state.versions.get((recipe_id, market), []):
            if version.version_no == version_no:
                return version
        return None
