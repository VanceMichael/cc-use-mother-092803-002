import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.contracts import Request
from app.service import MenuGovernanceService

T0 = datetime(2026, 1, 1, 8, 0, 0)
WINDOW = {"effective_from": "2026-01-01T00:00:00", "effective_to": "2026-12-31T23:59:59"}

CONTENT_V1 = {
    "name": "海盐椰子冰茶",
    "nutrition_grade": "B",
    "ingredients": [
        {"name": "椰子水", "source": "马来西亚供应商A"},
        {"name": "海盐", "source": "本地供应商B"},
    ],
}
CONTENT_V2 = {
    "name": "海盐椰子冰茶",
    "nutrition_grade": "C",
    "ingredients": [
        {"name": "椰子水", "source": "马来西亚供应商A"},
        {"name": "糖浆", "source": "新供应商C"},
    ],
}


def make_service(db_path=":memory:"):
    return MenuGovernanceService(
        db_path,
        actors={
            "admin-1": {"role": "admin"},
            "maintainer-1": {"role": "maintainer"},
            "reviewer-1": {"role": "reviewer"},
            "store-sg-01": {"role": "store", "markets": ["SG"]},
            "store-my-01": {"role": "store", "markets": ["MY"]},
            "manager-sg": {"role": "regional_manager", "markets": ["SG"]},
            "manager-apac": {"role": "regional_manager", "markets": ["SG", "MY"]},
        },
    )


class Flow:
    """测试辅助：统一发请求并自动分配幂等键。"""

    def __init__(self, service):
        self.service = service
        self.seq = 0

    def call(self, actor, action, payload, at=T0):
        self.seq += 1
        return self.service.handle(Request(actor, action, payload, f"req-{self.seq}", at))

    def submit(self, recipe="drink-1", market="SG", content=CONTENT_V1):
        return self.call("maintainer-1", "submit_version",
                         {"recipe_id": recipe, "market": market, "content": content})

    def approve(self, version_id, markets=("SG",), **window):
        payload = {"version_id": version_id, "decision": "approve",
                   "markets": list(markets), "reason": "营养等级与原料来源符合当地法规"}
        payload.update(window or WINDOW)
        return self.call("reviewer-1", "review_version", payload)

    def publish(self, version_id, recipe="drink-1", market="SG"):
        return self.call("reviewer-1", "publish",
                         {"recipe_id": recipe, "market": market, "version_id": version_id})

    def release_one(self, recipe="drink-1", market="SG", content=CONTENT_V1):
        submitted = self.submit(recipe, market, content)
        version_id = submitted.data["version_id"]
        self.approve(version_id, (market,))
        self.publish(version_id, recipe, market)
        return version_id


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.flow = Flow(self.service)

    # 1. 全流程：提交 -> 审核 -> 发布 -> 门店领取
    def test_full_flow(self):
        version_id = self.flow.release_one()
        result = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
        self.assertTrue(result.accepted)
        self.assertEqual(result.data["version_id"], version_id)
        self.assertEqual(result.data["content"], CONTENT_V1)

    # 2. 审核中的资料不得泄露给门店
    def test_pending_content_not_claimable(self):
        self.flow.submit()
        result = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "unreleased")

    # 3. 已审核未发布的版本门店仍领取不到
    def test_approved_but_unpublished_not_claimable(self):
        submitted = self.flow.submit()
        self.flow.approve(submitted.data["version_id"])
        result = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
        self.assertFalse(result.accepted)

    # 4. 门店只能领取所属市场
    def test_store_cannot_claim_other_market(self):
        self.flow.release_one(market="MY")
        result = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "MY"})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "forbidden")

    # 5. 越权动作被拒绝
    def test_unauthorized_actions(self):
        submitted = self.flow.submit()
        version_id = submitted.data["version_id"]
        cases = [
            ("store-sg-01", "publish", {"recipe_id": "drink-1", "market": "SG", "version_id": version_id}),
            ("maintainer-1", "review_version", {"version_id": version_id, "decision": "approve",
                                                "markets": ["SG"], "reason": "x", **WINDOW}),
            ("store-sg-01", "submit_version", {"recipe_id": "d", "market": "SG", "content": CONTENT_V1}),
            ("nobody", "query", {}),
        ]
        for actor, action, payload in cases:
            with self.subTest(actor=actor, action=action):
                result = self.flow.call(actor, action, payload)
                self.assertFalse(result.accepted)
                self.assertEqual(result.state, "forbidden")

    # 6. 同一 request_id 重放返回首次结果，不产生重复数据
    def test_idempotent_replay(self):
        request = Request("maintainer-1", "submit_version",
                          {"recipe_id": "drink-1", "market": "SG", "content": CONTENT_V1}, "req-once", T0)
        first = self.service.handle(request)
        second = self.service.handle(request)
        self.assertEqual(first, second)
        versions = self.flow.call("reviewer-1", "query", {"markets": ["SG"]}).data["markets"]["SG"]["versions"]
        self.assertEqual(len(versions), 1)

    # 7. 重复发布同一版本不产生第二份记录
    def test_duplicate_publish_no_second_record(self):
        version_id = self.flow.release_one()
        again = self.flow.publish(version_id)
        self.assertTrue(again.accepted)
        self.assertIn("未产生重复记录", again.message)
        trace = self.flow.call("reviewer-1", "trace", {"recipe_id": "drink-1", "market": "SG"})
        self.assertEqual(len(trace.data["history"]), 1)

    # 8. 紧急回滚后新领取回到指定版本
    def test_rollback_pins_version(self):
        v1 = self.flow.release_one(content=CONTENT_V1)
        v2 = self.flow.release_one(content=CONTENT_V2)  # 同配方第二版并发布
        claim = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
        self.assertEqual(claim.data["version_id"], v2)

        rollback = self.flow.call("reviewer-1", "rollback",
                                  {"recipe_id": "drink-1", "market": "SG",
                                   "to_version_id": v1, "reason": "新供应商C资质存疑，紧急回滚"})
        self.assertTrue(rollback.accepted)
        claim = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
        self.assertEqual(claim.data["version_id"], v1)
        self.assertEqual(claim.data["content"], CONTENT_V1)

    # 9. 回滚到生效窗口已过的旧版本，领取仍以回滚指令为准
    def test_rollback_overrides_expired_window(self):
        submitted = self.flow.submit(content=CONTENT_V1)
        v1 = submitted.data["version_id"]
        self.flow.approve(v1, ("SG",), effective_from="2026-01-01T00:00:00",
                          effective_to="2026-01-31T23:59:59")
        self.flow.publish(v1)
        v2 = self.flow.submit(content=CONTENT_V2).data["version_id"]
        self.flow.approve(v2, ("SG",), effective_from="2026-02-01T00:00:00",
                          effective_to="2026-12-31T23:59:59")
        self.flow.publish(v2)
        later = T0 + timedelta(days=90)  # v1 窗口已过，v2 在窗口内
        claim = self.flow.call("store-sg-01", "claim",
                               {"recipe_id": "drink-1", "market": "SG"}, at=later)
        self.assertEqual(claim.data["version_id"], v2)
        self.flow.call("reviewer-1", "rollback",
                       {"recipe_id": "drink-1", "market": "SG", "to_version_id": v1,
                        "reason": "v2 原料投诉，紧急回滚"}, at=later)
        claim = self.flow.call("store-sg-01", "claim",
                               {"recipe_id": "drink-1", "market": "SG"}, at=later)
        self.assertTrue(claim.accepted)
        self.assertEqual(claim.data["version_id"], v1)

    # 10. 生效时间窗口约束领取
    def test_effective_window_gates_claim(self):
        submitted = self.flow.submit()
        v1 = submitted.data["version_id"]
        self.flow.approve(v1, ("SG",), effective_from="2026-02-01T00:00:00",
                          effective_to="2026-12-31T23:59:59")
        self.flow.publish(v1)
        early = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
        self.assertEqual(early.state, "pending")
        later = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"},
                               at=datetime(2026, 3, 1))
        self.assertTrue(later.accepted)
        expired = self.flow.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"},
                                 at=datetime(2027, 1, 1))
        self.assertEqual(expired.state, "expired")

    # 11. 跨区域管理人员只能看到获授权市场
    def test_manager_sees_only_authorized_markets(self):
        self.flow.release_one(market="SG")
        self.flow.release_one(market="MY")
        result = self.flow.call("manager-sg", "query", {})
        self.assertEqual(set(result.data["markets"].keys()), {"SG"})
        # 即使显式请求未授权市场也被过滤
        result = self.flow.call("manager-sg", "query", {"markets": ["SG", "MY", "TH"]})
        self.assertEqual(set(result.data["markets"].keys()), {"SG"})
        both = self.flow.call("manager-apac", "query", {})
        self.assertEqual(set(both.data["markets"].keys()), {"SG", "MY"})

    # 12. 审核中的版本不向管理人员泄露
    def test_pending_version_hidden_from_manager(self):
        self.flow.release_one(market="SG", content=CONTENT_V1)
        self.flow.submit(market="SG", content=CONTENT_V2)  # 待审核的第二版
        versions = self.flow.call("manager-sg", "query", {}).data["markets"]["SG"]["versions"]
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["status"], "approved")
        # 审核人员可以看到待审核版本
        reviewer_versions = self.flow.call("reviewer-1", "query", {}).data["markets"]["SG"]["versions"]
        self.assertEqual(len(reviewer_versions), 2)

    # 13. 管理人员不能追溯未授权市场
    def test_manager_trace_scope(self):
        self.flow.release_one(market="MY")
        denied = self.flow.call("manager-sg", "trace", {"recipe_id": "drink-1", "market": "MY"})
        self.assertFalse(denied.accepted)
        allowed = self.flow.call("manager-apac", "trace", {"recipe_id": "drink-1", "market": "MY"})
        self.assertTrue(allowed.accepted)

    # 14. 沿历史版本追溯每项原料被允许使用的原因
    def test_trace_history_with_approval_reasons(self):
        v1 = self.flow.release_one(content=CONTENT_V1)
        v2 = self.flow.release_one(content=CONTENT_V2)
        self.flow.call("reviewer-1", "rollback",
                       {"recipe_id": "drink-1", "market": "SG", "to_version_id": v1,
                        "reason": "新供应商C资质存疑"})
        trace = self.flow.call("manager-sg", "trace", {"recipe_id": "drink-1", "market": "SG"})
        history = trace.data["history"]
        self.assertEqual([h["event"] for h in history], ["publish", "publish", "rollback"])
        self.assertEqual([h["version_id"] for h in history], [v1, v2, v1])
        for entry in history:
            self.assertIsNotNone(entry["approval"])
            self.assertEqual(entry["approval"]["reviewer"], "reviewer-1")
            self.assertIn("符合当地法规", entry["approval"]["reason"])
            self.assertTrue(all(i["source"] for i in entry["ingredients"]))
        # 回滚事件记录了原因
        self.assertIn("资质存疑", history[-1]["reason"])

    # 15. 数据保存在调用方提供的 SQLite 文件中，可跨实例恢复
    def test_persistence_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gov.db")
            flow = Flow(make_service(path))
            version_id = flow.release_one()
            # 新实例打开同一文件
            reopened = Flow(make_service(path))
            claim = reopened.call("store-sg-01", "claim", {"recipe_id": "drink-1", "market": "SG"})
            self.assertTrue(claim.accepted)
            self.assertEqual(claim.data["version_id"], version_id)

    # 16. 未通过审核的版本不能发布；未获该市场放行的版本不能发布
    def test_publish_guards(self):
        submitted = self.flow.submit()
        v1 = submitted.data["version_id"]
        unreviewed = self.flow.publish(v1)
        self.assertFalse(unreviewed.accepted)
        self.flow.approve(v1, ("SG",))
        wrong_market = self.flow.publish(v1, market="MY")
        self.assertFalse(wrong_market.accepted)
        self.assertEqual(wrong_market.state, "unapproved")

    # 17. 不能回滚到未曾在该市场获放行的版本
    def test_rollback_guard(self):
        v1 = self.flow.release_one(market="SG")
        other = self.flow.submit(market="MY", content=CONTENT_V2)
        v2 = other.data["version_id"]
        self.flow.approve(v2, ("MY",))
        result = self.flow.call("reviewer-1", "rollback",
                                {"recipe_id": "drink-1", "market": "SG",
                                 "to_version_id": v2, "reason": "尝试回滚"})
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "unapproved")

    # 18. 配方内容校验
    def test_content_validation(self):
        bad = self.flow.submit(content={"name": "x", "nutrition_grade": "A",
                                        "ingredients": [{"name": "水"}]})
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.state, "invalid")


if __name__ == "__main__":
    unittest.main()
