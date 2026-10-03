# 多地区菜单治理

面向配方版本与本地审核的后端服务。配方维护者提交本地化版本，营养审核人员决定生效市场与时间，门店只能领取所属市场已放行的内容；支持紧急回滚、幂等重放与历史追溯。

## 目录

`app/` 放置领域对象和服务入口，`tests/` 保存行为测试。

## 角色

| 角色 | 说明 |
| --- | --- |
| `maintainer` | 配方维护者，提交本地化版本 |
| `reviewer` | 营养审核人员，审核、发布、回滚 |
| `store` | 门店，只能领取所属市场已放行内容 |
| `regional_manager` | 跨区域管理人员，只能查询/追溯获授权市场 |
| `admin` | 登记操作者 |

## 动作

- `register_actor`（admin）：登记操作者，payload `{actor, role, markets}`
- `submit_version`（maintainer）：提交版本，payload `{recipe_id, market, content}`；content 需含 `name`、`nutrition_grade`、`ingredients[{name, source}]`
- `review_version`（reviewer）：审核，payload `{version_id, decision: approve|reject, markets, effective_from, effective_to?, reason}`
- `publish`（reviewer）：放行到市场，payload `{recipe_id, market, version_id}`；重复发布同一版本不会产生第二份记录
- `claim`（store）：门店领取，payload `{recipe_id, market?}`；只返回所属市场已放行且在生效窗口内的内容
- `rollback`（reviewer）：紧急回滚，payload `{recipe_id, market, to_version_id, reason}`；之后新领取回到指定版本
- `query`（reviewer / regional_manager / maintainer）：按市场查询版本与放行状态，管理人员仅见获授权市场，审核中内容不外泄
- `trace`（reviewer / regional_manager）：沿发布/回滚历史追溯每个版本的原料、营养等级与当时的审核理由

所有请求携带 `request_id` 作为幂等键，重放返回首次结果。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 构建检查

```bash
python3 -m compileall app
```

## 使用

服务以本地 Python 模块运行，数据默认保存在调用方提供的 SQLite 文件中：

```bash
echo '{"actor": "reviewer-1", "action": "query", "payload": {}, "request_id": "r-1"}' \
  | python3 -m app.api data.db
```
