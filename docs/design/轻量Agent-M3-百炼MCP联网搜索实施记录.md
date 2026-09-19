# 轻量 Agent M3：百炼 MCP 联网搜索实施记录

日期：2026-09-18。用户确认使用百炼 MCP 广场的联网搜索端点 `https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp`。本次完成离线实现和模拟验收；没有读取或修改本地 `.env` 中的密钥，没有向百炼或网页地址发起真实请求，也没有执行 Git push。

## 实现范围

- 新增受控的 `BailianMcpClient`：使用官方 MCP Streamable HTTP SDK，认证只从既有 `DASHSCOPE_API_KEY` 读取；调用前发现工具并验证搜索参数，工具名不唯一时要求通过 `BAILIAN_MCP_WEB_SEARCH_TOOL` 明确指定。
- 在通用工具平台登记 `web_search`（`mcp` 适配器）和 `read_webpage`（`http` 适配器）。模型不能连接任意 MCP Server、指定任意请求头或绕过平台注册表。
- `web_search` 只发送经 Schema 与敏感关键词策略筛出的公开查询；不发送历史、内部检索片段、会话标识或知识库权限。最多保留五条 URL 线索，标记为 `lead`，不作为事实依据。
- `read_webpage` 只接受本次搜索签发且绑定会话的句柄，或本轮用户消息中逐字出现的链接。地址只允许无凭据的 HTTP(S) 标准端口，DNS 拒绝回环、私网和非公网 IP；HTTPS 连接固定到已校验 IP 且用原域名做 TLS 校验。每次跳转重新校验，最多三跳。
- 网页读取限制 HTML/纯文本、超时、压缩后大小（2 MB）和正文长度；拒绝下载、脚本执行、非支持编码和登录/动态页面。网页正文是外部不可信数据。成功读取的正文才标为 `supported` 并进入工具来源；搜索摘要不会被当作引用。
- 前端引入 `dompurify`，对 Markdown 输出进行白名单净化；外部链接仅允许 HTTP(S)，使用 `noopener noreferrer nofollow`。

## 配置与启用

新增配置均默认关闭或保守限制：

- `WEB_SEARCH_ENABLED=false`
- `WEB_READ_ENABLED=false`
- `BAILIAN_MCP_WEB_SEARCH_URL`（默认上述百炼端点）
- `BAILIAN_MCP_WEB_SEARCH_TOOL`（默认空，自动发现仅在唯一匹配时使用）
- MCP/网页超时、跳转次数、响应字节数、正文字符数上限

启用前需在百炼控制台确认联网搜索服务、API Key 权限与额度；再设置 `WEB_SEARCH_ENABLED=true`。若要让模型读取搜索命中的正文，还需设置 `WEB_READ_ENABLED=true`。保持两项为 `false` 时，Agent 目录不会暴露这两个工具，原有行为不变。

## 依赖与验证

- 后端新增 `mcp` 与已有 `httpx` 依赖；安装 MCP SDK 时，首次受限环境无法连接包源，后经用户授权的依赖安装权限完成。该失败没有影响代码或配置。
- 前端新增 `dompurify`。首次受限安装未在时限内完成；经用户授权的依赖安装权限成功完成。npm 提示另有三个既存依赖的安装脚本尚待批准，本次没有批准或执行它们。
- 执行 `backend` 下的 M1/M2/M3 离线测试：`59 passed, 3 warnings`。M3 覆盖默认隐藏、私网 URL 拒绝、MCP 工具发现与认证头、搜索线索会话句柄、用户链接限制和敏感查询阻断。测试使用模拟 MCP 和网页读取器，不验证百炼账号可用性、线上工具名、真实搜索质量或外网连通性。

## 已知边界

- 当前正文读取不解析 PDF、附件、需要登录的页面或 JavaScript 渲染内容；这些情况应明确返回不能读取，而非臆测内容。
- 网页正文“已读取”不等于自动判定为官方或最新通知。涉及报名截止、届次等问题仍需要模型根据正文、日期和来源给出限定；下一阶段可为权威来源与发布日期提取增加更严格的核验规则。
