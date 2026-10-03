import json
import os
import tempfile
import unittest
from datetime import datetime

from app.contracts import Directory, Request
from app.service import MenuRegistryService, content_hash
from app.storage import SqliteEventStore

BASE = datetime(2026, 10, 3, 9, 0, 0)


def t(minute: int) -> datetime:
    return BASE.replace(minute=minute % 60, hour=9 + minute // 60)


DIRECTORY = Directory.from_dict({"actors": [
    {"id": "chef-sg", "role": "recipe_maintainer", "markets": ["SG", "MY"]},
    {"id": "chef-id", "role": "recipe_maintainer", "markets": ["ID"]},
    {"id": "reviewer-sea", "role": "nutrition_reviewer", "markets": ["SG", "MY"]},
    {"id": "reviewer-id", "role": "nutrition_reviewer", "markets": ["ID"]},
    {"id": "store-sg-1", "role": "store", "markets": ["SG"]},
    {"id": "store-my-1", "role": "store", "markets": ["MY"]},
    {"id": "store-id-1", "role": "store", "markets": ["ID"]},
    {"id": "manager-sg-my", "role": "regional_manager", "markets": ["SG", "MY"]},
    {"id": "manager-id", "role": "regional_manager", "markets": ["ID"]},
]})


def content_v1():
    return {
        "name": "斑斓椰椰",
        "nutrition_grade": "B",
        "ingredients": [
            {"id": "pandan", "name": "斑斓汁", "origin": "SG-本地农场", "notes": "冷链"},
            {"id": "milk", "name": "椰奶", "origin": "MY-合规供应商A"},
        ],
        "preparation": "雪克 15 秒，加冰至刻度线",
        "changelog": "首版",
    }


def content_v2():
    body = content_v1()
    body = json.loads(json.dumps(body, ensure_ascii=False))
    body["nutrition_grade"] = "A"
    body["ingredients"][0]["origin"] = "MY-南部合作社"
    body["changelog"] = "减糖换源"
    return body


def content_v3_rejected():
    body = content_v2()
    body = json.loads(json.dumps(body, ensure_ascii=False))
    body["nutrition_grade"] = "C"
    body["changelog"] = "试做高糖版"
    return body


def rationales(grade_notes="符合 ASEAN 糖含量指引"):
    return [
        {"id": "pandan", "approved": True,
         "rationale": f"斑斓汁来源可溯，{grade_notes}"},
        {"id": "milk", "approved": True,
         "rationale": "椰奶供应商具备 HALAL 与进口检疫证明"},
    ]


def req(actor, action, payload, rid, at=None):
    return Request(actor, action, payload, rid, at or BASE)


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.svc = MenuRegistryService(directory=DIRECTORY)

    def approve(self, version_no, minute, markets=("SG", "MY"), at_effective=None,
                reviewer="reviewer-sea", ingredients=None, rid=None):
        at = t(minute)
        return self.svc.handle(req(reviewer, "review_recipe", {
            "recipe_id": "pandan-latte", "market": "SG",
            "version_no": version_no, "decision": "approved",
            "release_markets": list(markets),
            "effective_at": (at_effective or at).isoformat(),
            "ingredients": ingredients or rationales(),
            "review_notes": "材料齐备",
        }, rid or f"req-approve-{version_no}-{minute}", at))

    def release_v1(self):
        r1 = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "req-sub-v1", t(0)))
        self.assertTrue(r1.accepted, r1.message)
        a1 = self.approve(1, 10)
        self.assertTrue(a1.accepted, a1.message)
        return r1, a1

    # ------------------------------------------------------------------ 提交与去重

    def test_submit_creates_pending_version(self):
        r = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "req-sub-v1", t(0)))
        self.assertEqual(r.state, "pending_review")
        self.assertEqual(r.data["version_no"], 1)
        self.assertIsNone(r.data["parent_version"])

    def test_same_request_id_never_creates_second_record(self):
        first = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "req-dup", t(0)))
        second = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG",
            "content": {"tampered": True},
        }, "req-dup", t(0)))
        self.assertEqual(first, second)
        versions = self.svc._state.versions[("pandan-latte", "SG")]
        self.assertEqual(len(versions), 1)

    def test_identical_content_does_not_mint_new_version(self):
        self.release_v1()
        dup = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "req-sub-dup-content", t(20)))
        self.assertFalse(dup.accepted)
        self.assertEqual(dup.data["version_no"], 1)
        versions = self.svc._state.versions[("pandan-latte", "SG")]
        self.assertEqual(len(versions), 1)

    def test_pending_blocks_resubmission_but_decision_unblocks(self):
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "x", "market": "SG", "content": content_v1(),
        }, "s1", t(0)))
        blocked = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "x", "market": "SG", "content": content_v2(),
        }, "s2", t(1)))
        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.state, "pending_review")
        self.assertEqual(blocked.data["blocking_version"], 1)

        self.svc.handle(req("reviewer-sea", "review_recipe", {
            "recipe_id": "x", "market": "SG", "version_no": 1,
            "decision": "rejected", "review_notes": "糖标缺失",
        }, "r-rej", t(2)))
        again = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "x", "market": "SG", "content": content_v2(),
        }, "s3", t(3)))
        self.assertTrue(again.accepted)
        self.assertEqual(again.data["version_no"], 2)

    def test_content_hash_independent_of_key_order(self):
        ordered = json.dumps(content_v1(), ensure_ascii=False)
        rebuilt = {k: v for k, v in reversed(list(json.loads(ordered).items()))}
        self.assertEqual(content_hash(json.loads(ordered)), content_hash(rebuilt))

    def test_submit_requires_complete_ingredient_provenance(self):
        bad = content_v1()
        del bad["ingredients"][0]["origin"]
        r = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "x", "market": "SG", "content": bad,
        }, "bad-origin", t(0)))
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "invalid_request")

    # ------------------------------------------------------------------ 审核把关

    def test_approval_requires_rationale_for_every_ingredient(self):
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "sub", t(0)))
        missing = self.svc.handle(req("reviewer-sea", "review_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "version_no": 1,
            "decision": "approved", "release_markets": ["SG"],
            "effective_at": t(5).isoformat(),
            "ingredients": [{"id": "pandan", "approved": True, "rationale": "有来源"}],
        }, "app1", t(4)))
        self.assertFalse(missing.accepted)
        self.assertIn("milk", missing.message)

        disallowed = self.svc.handle(req("reviewer-sea", "review_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "version_no": 1,
            "decision": "approved", "release_markets": ["SG"],
            "effective_at": t(5).isoformat(),
            "ingredients": [
                {"id": "pandan", "approved": False, "rationale": "来源农场证照过期"},
                {"id": "milk", "approved": True, "rationale": "证照齐全"},
            ],
        }, "app2", t(4)))
        self.assertFalse(disallowed.accepted)
        self.assertIn("pandan", disallowed.message)
        # 未批准的版本不得泄露给门店
        menu = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "fetch", t(6)))
        self.assertEqual(menu.data["items"], [])

    def test_reviewer_cannot_release_beyond_authorized_markets(self):
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "sub", t(0)))
        r = self.svc.handle(req("reviewer-sea", "review_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "version_no": 1,
            "decision": "approved", "release_markets": ["SG", "ID"],
            "effective_at": t(5).isoformat(), "ingredients": rationales(),
        }, "overreach", t(4)))
        self.assertFalse(r.accepted)
        # ID 门店确认没有被越权放行
        menu = self.svc.handle(req("store-id-1", "fetch_menu", {}, "fetch-id", t(10)))
        self.assertEqual(menu.data["items"], [])

    def test_decision_is_one_shot(self):
        self.release_v1()
        again = self.approve(1, 30, rid="double-approve")
        self.assertFalse(again.accepted)
        self.assertEqual(again.state, "released")

    def test_rejected_version_invisible_and_reason_returned_to_maintainer(self):
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v3_rejected(),
        }, "sub-v3", t(40)))
        rej = self.svc.handle(req("reviewer-sea", "review_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "version_no": 1,
            "decision": "rejected", "review_notes": "糖含量超出新加坡等级要求",
        }, "rej-v3", t(41)))
        self.assertEqual(rej.state, "rejected")
        menu = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "m", t(42)))
        self.assertEqual(menu.data["items"], [])
        mine = self.svc.handle(req("chef-sg", "my_submissions", {}, "my", t(42)))
        row = next(i for i in mine.data["items"] if i["version_no"] == 1)
        self.assertEqual(row["status"], "rejected")
        self.assertIn("糖含量", row["review_notes"])

    # ------------------------------------------------------------------ 门店领取：保密、排期、隔离

    def test_pending_material_never_leaks_to_stores_or_other_markets(self):
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
        }, "sub", t(0)))
        for actor in ("store-sg-1", "store-my-1", "store-id-1"):
            menu = self.svc.handle(req(actor, "fetch_menu", {}, f"m-{actor}", t(5)))
            self.assertEqual(menu.data["items"], [])

    def test_scheduled_release_only_visible_after_effective_time(self):
        self.release_v1()  # v1 09:10 生效
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v2(),
        }, "sub-v2", t(20)))
        self.approve(2, 25, at_effective=t(60))  # 10:00 生效
        before = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "before", t(55)))
        self.assertEqual(before.data["items"][0]["version_no"], 1)
        after = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "after", t(65)))
        self.assertEqual(after.data["items"][0]["version_no"], 2)

    def test_store_sees_only_own_market_including_cross_market_release(self):
        self.release_v1()
        sg = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "sg", t(15)))
        my = self.svc.handle(req("store-my-1", "fetch_menu", {}, "my", t(15)))
        ident = self.svc.handle(req("store-id-1", "fetch_menu", {}, "id", t(15)))
        self.assertEqual(sg.data["items"][0]["version_no"], 1)
        self.assertEqual(sg.data["items"][0]["source_market"], "SG")
        self.assertEqual(my.data["items"][0]["source_market"], "SG")  # 跨市场领取
        self.assertEqual(ident.data["items"], [])
        # 门店响应里不应带审核备注等内部字段
        self.assertNotIn("review_notes", sg.data["items"][0])

    # ------------------------------------------------------------------ 紧急回滚

    def test_emergency_rollback_pins_new_fetches_to_named_version(self):
        self.release_v1()
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v2(),
        }, "sub-v2", t(20)))
        self.approve(2, 25, at_effective=t(30))
        live = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "live", t(31)))
        self.assertEqual(live.data["items"][0]["version_no"], 2)

        rb = self.svc.handle(req("reviewer-sea", "rollback", {
            "recipe_id": "pandan-latte", "market": "SG",
            "target_version": 1, "reason": "v2 椰奶批次污染",
        }, "rollback", t(32)))
        self.assertTrue(rb.accepted, rb.message)
        pinned = self.svc.handle(req("store-sg-1", "fetch_menu", {}, "pinned", t(33)))
        self.assertEqual(pinned.data["items"][0]["version_no"], 1)
        self.assertEqual(pinned.data["items"][0]["served_via"], "rollback")

    def test_rollback_to_never_released_version_rejected(self):
        self.release_v1()
        r = self.svc.handle(req("reviewer-sea", "rollback", {
            "recipe_id": "pandan-latte", "market": "SG", "target_version": 9,
        }, "rb-bad", t(40)))
        self.assertFalse(r.accepted)
        self.assertEqual(r.state, "invalid_rollback_target")

    def test_cross_market_rollback_uses_source_market_pointer(self):
        self.release_v1()
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v2(),
        }, "sub-v2", t(20)))
        self.approve(2, 25, at_effective=t(30))
        # MY 也随 v2 生效，随后跨市场回滚到 SG 的 v1
        self.assertEqual(
            self.svc.handle(req("store-my-1", "fetch_menu", {}, "my-v2", t(31)))
                .data["items"][0]["version_no"], 2)
        rb = self.svc.handle(req("reviewer-sea", "rollback", {
            "recipe_id": "pandan-latte", "market": "MY",
            "source_market": "SG", "target_version": 1, "reason": "同步停售",
        }, "rb-my", t(32)))
        self.assertTrue(rb.accepted, rb.message)
        pinned = self.svc.handle(req("store-my-1", "fetch_menu", {}, "my-pinned", t(33)))
        self.assertEqual(pinned.data["items"][0]["version_no"], 1)
        self.assertEqual(pinned.data["items"][0]["source_market"], "SG")

    # ------------------------------------------------------------------ 权限矩阵

    def test_permission_matrix(self):
        cases = [
            ("store-sg-1", "submit_recipe", {"recipe_id": "x", "market": "SG",
                                             "content": content_v1()}),
            ("chef-sg", "review_recipe", {"recipe_id": "x", "market": "SG",
                                          "version_no": 1}),
            ("chef-sg", "rollback", {"recipe_id": "x", "market": "SG",
                                     "target_version": 1}),
            ("manager-sg-my", "fetch_menu", {}),
            ("store-sg-1", "review_queue", {}),
            ("manager-sg-my", "my_submissions", {}),
        ]
        for actor, action, payload in cases:
            r = self.svc.handle(req(actor, action, payload, f"perm-{actor}-{action}", t(0)))
            self.assertEqual(r.state, "forbidden", (actor, action, r))

    def test_unregistered_actor_and_unknown_action(self):
        self.assertEqual(self.svc.handle(
            req("ghost", "fetch_menu", {}, "x", t(0))).state, "unauthorized")
        self.assertEqual(self.svc.handle(
            req("store-sg-1", "teleport", {}, "y", t(0))).state, "unknown_action")

    def test_maintainer_cannot_cross_market_boundary(self):
        r = self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "x", "market": "ID", "content": content_v1(),
        }, "cross", t(0)))
        self.assertEqual(r.state, "forbidden")

    def test_review_queue_scoped_to_reviewer_markets(self):
        self.svc.handle(req("chef-id", "submit_recipe", {
            "recipe_id": "x", "market": "ID", "content": content_v1(),
        }, "id-sub", t(0)))
        sea_queue = self.svc.handle(req("reviewer-sea", "review_queue", {}, "q-sea", t(1)))
        id_queue = self.svc.handle(req("reviewer-id", "review_queue", {}, "q-id", t(1)))
        self.assertEqual(sea_queue.data["items"], [])
        self.assertEqual(id_queue.data["items"][0]["market"], "ID")
        # 审核员队列内可见完整待审资料（保密范围之外）
        self.assertIn("content", id_queue.data["items"][0])

    # ------------------------------------------------------------------ 跨区追溯

    def _history_setup(self):
        self.release_v1()
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG", "content": content_v2(),
        }, "sub-v2", t(20)))
        self.approve(2, 25, at_effective=t(30))
        self.svc.handle(req("reviewer-sea", "rollback", {
            "recipe_id": "pandan-latte", "market": "SG",
            "target_version": 1, "reason": "原料风险",
        }, "rb", t(32)))

    def test_manager_history_scoped_and_pending_redacted(self):
        self._history_setup()
        # 新的待审版本必须对经理脱敏
        self.svc.handle(req("chef-sg", "submit_recipe", {
            "recipe_id": "pandan-latte", "market": "SG",
            "content": {**content_v2(), "changelog": "再调整"},
        }, "sub-v4", t(40)))

        history = self.svc.handle(req("manager-sg-my", "recipe_history", {
            "recipe_id": "pandan-latte", "markets": ["SG", "ID"],
        }, "hist", t(45)))
        self.assertTrue(history.accepted)
        sg = next(m for m in history.data["markets"] if m["market"] == "SG")
        # ID 未授权，直接从结果里消失
        self.assertEqual([m["market"] for m in history.data["markets"]], ["SG"])
        self.assertEqual(sg["served_version"], ["SG", 1])
        kinds = [c["kind"] for c in sg["channel"]]
        self.assertEqual(kinds, ["release", "release", "rollback"])
        pending = next(v for v in sg["submissions"] if v["status"] == "pending_review")
        self.assertNotIn("content", pending)
        self.assertEqual(pending["note"], "审核中，资料保密")
        released = next(v for v in sg["submissions"] if v["version_no"] == 1)
        self.assertIn("content", released)  # 已放行的历史可回看

    def test_manager_cannot_query_unauthorized_market_at_all(self):
        self._history_setup()
        r = self.svc.handle(req("manager-id", "recipe_history", {
            "recipe_id": "pandan-latte", "markets": ["SG"],
        }, "trespass", t(45)))
        self.assertEqual(r.state, "forbidden")
        r2 = self.svc.handle(req("manager-id", "trace_ingredient", {
            "recipe_id": "pandan-latte", "market": "SG", "ingredient_id": "milk",
        }, "trespass2", t(45)))
        self.assertEqual(r2.state, "forbidden")

    def test_ingredient_trace_shows_why_allowed_at_each_release(self):
        self._history_setup()
        trace = self.svc.handle(req("manager-sg-my", "trace_ingredient", {
            "recipe_id": "pandan-latte", "market": "SG", "ingredient_id": "milk",
        }, "trace", t(50)))
        trail = trace.data["trail"]
        self.assertEqual([e["version_no"] for e in trail], [1, 2])
        self.assertTrue(all(e["allowed"] for e in trail))
        self.assertTrue(all(e["rationale"] for e in trail))
        self.assertTrue(all(e["allowed_by"] == "reviewer-sea" for e in trail))
        self.assertEqual(trail[0]["ingredient"]["origin"], "MY-合规供应商A")

    def test_maintainer_trace_limited_to_own_recipes(self):
        self._history_setup()
        ok = self.svc.handle(req("chef-sg", "trace_ingredient", {
            "recipe_id": "pandan-latte", "market": "SG", "ingredient_id": "pandan",
        }, "own", t(50)))
        self.assertTrue(ok.accepted)
        other = self.svc.handle(req("chef-id", "trace_ingredient", {
            "recipe_id": "pandan-latte", "market": "SG", "ingredient_id": "pandan",
        }, "not-own", t(50)))
        self.assertEqual(other.state, "forbidden")

    # ------------------------------------------------------------------ SQLite 持久化

    def test_sqlite_persistence_replays_state_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "menu.db")
            store = SqliteEventStore(db)
            svc = MenuRegistryService(directory=DIRECTORY, store=store)
            submit = req("chef-sg", "submit_recipe", {
                "recipe_id": "pandan-latte", "market": "SG", "content": content_v1(),
            }, "persist-sub", t(0))
            self.assertTrue(svc.handle(submit).accepted)
            approve = req("reviewer-sea", "review_recipe", {
                "recipe_id": "pandan-latte", "market": "SG", "version_no": 1,
                "decision": "approved", "release_markets": ["SG"],
                "effective_at": t(5).isoformat(), "ingredients": rationales(),
            }, "persist-app", t(4))
            self.assertTrue(svc.handle(approve).accepted)
            store.close()

            # 重新启动：事件重放恢复全部状态
            svc2 = MenuRegistryService(directory=DIRECTORY,
                                       store=SqliteEventStore(db))
            menu = svc2.handle(req("store-sg-1", "fetch_menu", {}, "m2", t(10)))
            self.assertEqual(menu.data["items"][0]["version_no"], 1)
            # 旧 request_id 重放不产生第二份记录
            replay = svc2.handle(submit)
            self.assertTrue(replay.accepted)
            self.assertEqual(replay.data["version_no"], 1)
            versions = svc2._state.versions[("pandan-latte", "SG")]
            self.assertEqual(len(versions), 1)
            svc2.store.close()


if __name__ == "__main__":
    unittest.main()
