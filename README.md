# 多地区菜单治理服务

面向连锁饮品跨市场（如新加坡 SG、马来西亚 MY、印尼 ID）同步上市的配方治理后端。
解决"配方、营养等级、原料来源散落在不同聊天记录、门店误用旧版本导致停售"的问题：

- **配方维护者**按市场提交本地化版本（配方、营养等级、原料来源缺一不可）；
- **营养审核人员**决定每版的批准/驳回、**生效市场范围、生效时间、紧急回滚点**，
  并对**逐项原料登记许可依据**；
- **门店只能领取所属市场当前放行指针指向的版本**——审核中、被驳回、未到生效
  时间的内容一律不可见；
- **紧急回滚后新领取立即回到指定版本**，此前排定的定时发布不会复活；
- **重复发布不会产生第二份记录**（request_id 幂等 + 内容指纹去重）；
- **跨区域管理人员**只能查询获授权市场，待审资料对其脱敏，并可沿历史版本
  追溯每项原料在当时被允许使用的原因与审批人。

## 架构

事件溯源（event sourcing）：所有状态变更以不可变事件落库，当前状态由重放得到。

```
app/contracts.py   请求/结果约定、角色常量、受信身份目录 Directory
app/storage.py     EventStore 协议、InMemoryEventStore、SqliteEventStore（唯一约束幂等）
app/service.py     MenuRegistryService：状态机、权限、放行指针、审计追溯
app/api.py         stdin/stdout 的本地调用入口
tests/test_service.py  25 个行为测试
```

事件类型：`recipe_submitted` → `recipe_reviewed`（approved/rejected）→ `release_rolled_back`。
审核决策以快照形式不可变地保存在版本上，包含审核员、生效范围/时间、逐原料 rationale，
因此"为什么当时允许"是一次查询，而不是从聊天记录里考古。

### 放行指针

每个 `(配方, 领取市场)` 维护一条按决策时间排列的指令链：

- `release(源市场, 版本, 生效时间)`：**到点才**推进指针，支持定时同步上市；
- `rollback(源市场, 版本)`：**立即**推进指针，排在链尾所以压过此前一切，
  包括回滚之后才到点的定时发布。

一条批准可以把 SG 提交的版本放行进多个市场；跨市场回滚时用 `source_market`
指明回到哪个市场的版本线。

## 角色与动作

| 动作 | 配方维护者 | 营养审核员 | 门店 | 跨区经理 |
|---|---|---|---|---|
| `submit_recipe` | ✅（限授权市场） | – | – | – |
| `review_recipe` | – | ✅（限授权市场） | – | – |
| `rollback` | – | ✅（限授权市场） | – | – |
| `fetch_menu` | – | – | ✅（仅本市场） | – |
| `review_queue` | – | ✅（限授权市场） | – | – |
| `my_submissions` | ✅（仅本人） | – | – | – |
| `recipe_history` | – | ✅ | – | ✅（限授权市场） |
| `trace_ingredient` | ✅（仅本人提交过的配方） | ✅ | – | ✅（限授权市场） |

版本状态：`pending_review` → `released` / `rejected`。待审版本在门店、经理
视图中均脱敏（仅维护者本人和有管辖权的审核员能看到内容）。

### 关键校验

- 提交：`name`、`nutrition_grade`、每项原料的 `id/name/origin`、`preparation` 必填；
  有版本在审时不能再提；同内容指纹不生成新版本。
- 批准：`release_markets` 非空且必须包含版本所属市场、不得超出审核员授权；
  `effective_at` 不得早于当前；每种原料必须有 `approved=true` 与 `rationale`；
  可预置 `rollback_version`（默认取当前生效版本）。
- 回滚：目标必须是**曾放行进该市场**的版本；当前已在目标版本则拒绝。
- 幂等：每个写请求必须带 `request_id`；存储层对其加唯一约束，重试返回首次结果。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall app
```

覆盖：提交去重、request_id 幂等、待审保密、市场隔离、定时生效、紧急回滚、
跨市场放行/回滚、权限矩阵、逐原料追溯、SQLite 持久化与重放。

## 使用

身份目录由部署方提供（不能由请求自带，否则门店可冒充审核员）：

```json
{"actors": [
  {"id": "chef-sg",   "role": "recipe_maintainer", "markets": ["SG", "MY"]},
  {"id": "reviewer-sea", "role": "nutrition_reviewer", "markets": ["SG", "MY"]},
  {"id": "store-sg-1", "role": "store", "markets": ["SG"]},
  {"id": "mgr-sea",   "role": "regional_manager", "markets": ["SG", "MY"]}
]}
```

每个请求是一个 JSON 对象（`actor` 必须已在目录登记）：

```bash
export MENU_DIRECTORY=directory.json
export MENU_DB=menu.db            # 不设则用内存库（进程结束即丢失）

# 1) 维护者提交本地化版本
echo '{"actor":"chef-sg","action":"submit_recipe","request_id":"req-1","payload":{
  "recipe_id":"pandan-latte","market":"SG",
  "content":{"name":"斑斓椰椰","nutrition_grade":"B",
    "ingredients":[{"id":"pandan","name":"斑斓汁","origin":"SG-本地农场"},
                   {"id":"milk","name":"椰奶","origin":"MY-供应商A"}],
    "preparation":"雪克15秒","changelog":"首版"}}}' | python3 -m app.api

# 2) 审核员决定生效范围/时间，并逐项登记原料依据
echo '{"actor":"reviewer-sea","action":"review_recipe","request_id":"req-2","payload":{
  "recipe_id":"pandan-latte","market":"SG","version_no":1,
  "decision":"approved","release_markets":["SG","MY"],
  "effective_at":"2026-10-03T10:00:00",
  "ingredients":[{"id":"pandan","approved":true,"rationale":"农场证照齐全，冷链可溯"},
                 {"id":"milk","approved":true,"rationale":"HALAL 与进口检疫证明齐备"}]}}' \
  | python3 -m app.api

# 3) 门店领取（只返回本市场已到生效时间的版本）
echo '{"actor":"store-sg-1","action":"fetch_menu","request_id":"req-3","payload":{}}' \
  | python3 -m app.api

# 4) 紧急回滚：新领取立即回到 v1
echo '{"actor":"reviewer-sea","action":"rollback","request_id":"req-4","payload":{
  "recipe_id":"pandan-latte","market":"SG","target_version":1,
  "reason":"椰奶批次污染"}}' | python3 -m app.api

# 5) 跨区经理：历史版本与原料许可依据追溯（只能查授权市场）
echo '{"actor":"mgr-sea","action":"trace_ingredient","request_id":"req-5","payload":{
  "recipe_id":"pandan-latte","market":"SG","ingredient_id":"milk"}}' | python3 -m app.api
```

退出码：`0` 请求被接受（查询或写入成功），`1` 被业务规则拒绝，`2` 输入为空。
所有时间使用 ISO 8601（UTC 朴素时间）。
