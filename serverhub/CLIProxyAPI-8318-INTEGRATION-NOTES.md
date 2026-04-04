# CLIProxyAPI 8318 × ServerHub 集成经验总结

> 目的：记录这次排障和修复的关键经验，避免下次改 `CLIProxyAPI 8318` 或 `serverhub` 时重复踩坑。
>
> 适用项目：
> - CLIProxyAPI 部署目录：`/opt/CLIProxyAPI-8318`
> - ServerHub 项目目录：`/root/.openclaw/workspace/serverhub`

---

## 1. 这次问题的真实根因

本次表象是：
- `serverhub` 里读取 8318 的凭证额度时，`limit` 能显示
- 但大量 `active` 账号没有具体额度百分比，表现为未知 / 空值

最终确认，问题分成 **两层**：

### 第一层：management 接口鉴权头不兼容
CLIProxyAPI 更新后，management 接口严格要求 management key。

`serverhub` 原先的请求头只有：
- `Authorization: Bearer <token>`

修复后改成同时发送：
- `x-management-key: <token>`
- `Authorization: Bearer <token>`

这样可以兼容新版 CLIProxyAPI。

### 第二层：Codex active 配额探测请求头不对
真正导致 **active 百分比拿不到** 的，是 `serverhub` 的 Codex quota 探测实现和 8318 自带 quota 页不一致。

`serverhub` 原先请求：
- `GET https://chatgpt.com/backend-api/wham/usage`
- `User-Agent: Mozilla/5.0`

这会导致很多 active 账号拿不到可用的 quota JSON。

而 8318 自带管理页实际使用的是：
- `GET https://chatgpt.com/backend-api/wham/usage`
- `User-Agent: codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal`
- `Chatgpt-Account-Id: <account_id>`

**结论：不是要换 endpoint，而是要对齐 8318 自带 quota 页的请求方式。**

---

## 2. 非常关键：不要误判 `/quota` 页面来源

这次一个很容易误判的点是：

用户说：
- 8318 的 `/quota` 页面还能正常看到 active 剩余额度

实际排查发现：
- CLIProxyAPI 暴露出来的不是简单的 `/quota` HTTP 路由
- 它的管理前端是 `management.html`
- 里面包含 `QuotaPage` / `AuthFiles quota card` 的前端逻辑
- 这个页面内部自己调用 management API + 专用 quota 探测逻辑

**所以以后遇到“页面能看，别的项目看不到”的问题，优先去抠管理前端的实现，而不是凭印象猜 API。**

---

## 3. 本次真正有效的排障方法

### 方法 A：先确认是“后端没拿到”还是“前端渲染错了”
本次先查了 `serverhub` 返回的数据，发现：
- 后端已经能区分 `active / limit / error`
- 不是全部 unknown

这一步帮助区分：
- 状态识别问题
- 百分比缺失问题
- 前端文案误导问题

### 方法 B：对比 CLIProxyAPI 自带管理前端实现
这一步是最关键的。

做法：
1. 从容器里导出管理页：
   - `docker cp cli-proxy-api-8318:/CLIProxyAPI/static/management.html /tmp/cliproxyapi-management.html`
2. 搜关键字：
   - `codex_quota`
   - `remainingFraction`
   - `quotaInfo`
   - `loadQuota`
3. 从压缩后的前端代码里抠出真实实现

最后定位到：
- `vee = "https://chatgpt.com/backend-api/wham/usage"`
- `xee = { Authorization: "Bearer $TOKEN$", Content-Type: "application/json", User-Agent: "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal" }`
- 以及 `Chatgpt-Account-Id` 的提取方式

### 方法 C：不要只盯着 endpoint，header 同样重要
一开始容易误以为：
- `wham/usage` 不行
- 要换 `/codex/status`、`/codex/usage` 等 endpoint

实际上：
- 错不在 endpoint 本身
- 而在 **User-Agent 和 account_id 提取方式**

这次直接实测后证明：
- 同一个 `wham/usage`
- 用 `Mozilla/5.0` → 拿不到有效结果
- 用 `codex_cli_rs/...WindowsTerminal` → 能返回完整 quota JSON

---

## 4. ServerHub 里最终需要保留的改动

### 4.1 management 请求头兼容
文件：`serverhub/app.py`

函数：`mgmt_headers(token, include_json=False)`

要求：至少同时发：
- `x-management-key`
- `Authorization: Bearer ...`

目的：兼容新版 CLIProxyAPI management 接口。

---

### 4.2 Codex quota 探测逻辑
文件：`serverhub/app.py`

关键点：
1. 需要 robust 地提取 `chatgpt_account_id`
2. 需要使用和 8318 管理页一致的 `User-Agent`
3. 继续走：
   - `POST /v0/management/api-call`
   - 目标 URL：`https://chatgpt.com/backend-api/wham/usage`
4. 解析：
   - `rate_limit.primary_window.used_percent`
   - `reset_at`
   - `reset_after_seconds`
   - `plan_type`

### 4.3 `chatgpt_account_id` 的提取来源
不要只从：
- `item.id_token.chatgpt_account_id`

还要兼容：
- JWT 字符串形式的 `id_token`
- `metadata.id_token`
- `attributes.id_token`

也就是说，要允许：
- dict 直接取字段
- JWT base64 decode 后取 `chatgpt_account_id`

---

## 5. 这次修复后的验收结果

修复后重新刷新 8318 对应 CPA，结果：
- `active_with_ratio = 344`
- `active_without_ratio = 2`
- `limit = 46`

汇总恢复：
- `summary_remaining_ratio = 76.66`
- `summary_used_ratio = 23.34`

说明：
- 大多数 active 账号已经恢复具体百分比
- 剩余少量 active 账号没有百分比，属于个别凭证问题，不是全局实现问题

---

## 6. 下次改这个项目时的操作顺序建议

以后再改 `serverhub` 或 `CLIProxyAPI 8318`，建议严格按这个顺序来：

### Step 1：先确认是哪一层坏了
先分清楚：
- management 接口进不去？
- auth-files 返回结构变了？
- active 百分比缺失？
- 只是前端文案误导？

### Step 2：不要猜 API，先对齐 8318 自带管理页实现
优先做：
- 导出 `management.html`
- 搜 provider 对应 quota 页面实现
- 确认它真实调用的接口、header、解析逻辑

### Step 3：只改 `serverhub`，不要先动 8318
如果目标只是恢复 `serverhub` 显示：
- 优先改 `serverhub`
- 不要先动 8318 私有逻辑
- 除非确认是 8318 自己坏了

### Step 4：改完必须重启并刷新真实目标
这次真正有效的验收方式是：
1. 重启 `serverhub-8321`
2. 调：
   - `POST /api/cpas/<id>/refresh`
3. 再看：
   - `/api/overview?range=24h`
   - active 是否有 `remaining_ratio`

不要只看静态页面，不然容易被缓存误导。

---

## 7. 直接可复用的排障命令

### 查看 8318 本地仓库
```bash
cd /opt/CLIProxyAPI-8318
```

### 查看 8318 管理前端 asset
```bash
docker cp cli-proxy-api-8318:/CLIProxyAPI/static/management.html /tmp/cliproxyapi-management.html
```

### 搜 Codex quota 实现
```bash
grep -n "codex_quota\|remainingFraction\|quotaInfo\|loadQuota" /tmp/cliproxyapi-management.html
```

### 刷新 ServerHub 的目标 CPA
```bash
curl -X POST http://127.0.0.1:8321/api/cpas/d6d82679b9a9/refresh
```

### 看刷新后的汇总
```bash
curl http://127.0.0.1:8321/api/overview?range=24h
```

---

## 8. 经验结论（最重要的几句）

1. **不要把“页面还能看”理解成“公开 API 路由没问题”。**
2. **遇到管理后台相关问题，优先抠管理前端实现。**
3. **同一个 endpoint，header 不同，结果可能完全不同。**
4. **这次 active 百分比恢复，关键不是换接口，而是对齐 8318 quota 页的 UA 和 account_id 提取逻辑。**
5. **以后再动这块，先看这个文件，再动代码。**

---

## 9. 当前状态快照

当前这套修复之后：
- `serverhub` 已兼容新版 CLIProxyAPI management key 头
- `serverhub` 已恢复绝大多数 active 的具体额度百分比
- 8318 的私有改动未被动到
- 自动更新脚本已补好

如果下次这里又出问题，先看：
- management key 是否正常
- `probe_cpa_quota()` 是否还是对齐 quota 页实现
- `management.html` 中 codex quota 实现是否发生变化
