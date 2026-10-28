# P0 原生协议验证操作与验收

完整范围见 [实施方案](cloud-account-pool-plan.md)。此文档用于实际采样与结果归档，不代表相关链路已经通过。

## 准备

使用两种 Windows 环境：已登录并有额度的官方 IDE，以及不包含客户自备上游账号的干净 IDE。记录 Windows 版本、IDE 版本、产品名称与区域。不要把邮箱、密码、Token 或原始对话提交到仓库。

管理员账号只用于自己的测试。客户无账号场景和已有账号场景分别验收，不能混用登录状态掩盖初始化问题。

## 采样范围

先采集小规模、内容简单的正常请求，再依次验证长响应、工具调用、多轮、取消和并发。需确认的数据包括原生请求域名 / 路由、编码方式、协议、HTTP 状态、SSE 信封、最终用量与上游错误形式。

初始 Switch 会记录认证头和正文，也会收集完整响应后才返回。P0-A 已修复这两点：默认只记脱敏元数据，响应直接流式透传；继承的旧日志仍不得直接上传。当前代理默认不保存响应正文，真实采样需在受控测试环境另外取得本机原始响应，再留在被忽略的 `.local/protocol/` 目录，用白名单工具生成不含内容的用量报告。

## 检查表

| 编号 | 场景 | 证据 | 当前状态 |
|---|---|---|---|
| P0-01 | 本机代理 CONNECT 正常 | 本机诊断和请求成功 | Python loopback 通过；Rust / IDE 实机部分本轮跳过，待人工 |
| P0-02 | 自动 CA 被目标 IDE 实际信任 | 未关闭 TLS 校验，正常 HTTPS 对话 | Windows ROOT 已确认；Node 默认失败、子进程系统 CA 通过；正常对话本轮跳过，待人工 |
| P0-03 | 无上游账号的客户初始化 | 干净用户目录完成目标流程 | 本轮跳过，待人工操作独立 IDE |
| P0-04 | 云端选号与原生重签 | 客户侧没有上游凭据，对话成功 | 待实现 |
| P0-05 | SSE 首字、工具、多轮与取消 | 时序、完成状态、语义验证 | 本轮跳过，待真实账号与样本 |
| P0-06 | 单次 Credits 可信 | 服务端响应字段与官方记录一致 | 离线核对工具完成；真实样本与官方记录本轮跳过 |
| P0-07 | 缺失 / 重复用量 | 95 项离线测试通过，覆盖压缩、流边界、跨缺失快照回退、畸形 JSON 和数字核对 | 离线通过，实机样本部分本轮跳过 |
| P0-08 | 同客户与跨客户会话隔离 | 会话和账单各自归属 | 待云端实现 |

## 样本与结果的约定

真实样本不提交 Git。脱敏报告只记录可核对的协议字段：是否出现 usage、Credits 是否存在、输入 / 输出 / 缓存 Token、终止信号、上游状态码、用量快照个数与最终判定。没有 usage 不能被转换成 0；若 Credits 精度超过账本约定、为负数或不是有限数字，应报告无效用量。

当前解析器只覆盖声明的 JSON / SSE 信封，以及显式指定的单成员 gzip / zlib 包装 deflate 正文。未知编码、二进制或信封保持“未验证 / 用量未知”，不得据此宣称目标 IDE 已兼容。输入应是去掉 HTTP 头与传输分块后的正文；抓包工具已解压时使用 `identity`，不重复解压。

## 下一步实机操作

1. 确认具体 Qoder 产品、版本、区域，以及能否在 Windows 测试环境执行工具。
2. 使用现有已登录账号验证一个最小对话，保留原始响应在本机目录。
3. 运行采样工具，只查看脱敏报告中的 Credits 与 Token 是否完整；报告不是平台账单。
4. 核对官方显示的实际消耗，再尝试取消与长响应，确认是否会丢最终 usage。
5. 清空客户侧的账号登录状态，在隔离用户目录验证初始化；保留可复现步骤。
6. 样本形成路由清单后，实现云端桥接并重跑以上用例。

每项结果要记录日期、兼容版本、步骤、预期、实际、脱敏证据和是否通过。未通过项保留失败原因，不改成演示数据来推进验收。

## 本轮跳过与回来后的验证清单（2026-10-28）

用户不在场时，缺外部资源或人工操作的测试标记跳过，其他开发与离线回归继续。这里的跳过登记不是 `unittest` 空占位用例，不计入自动测试通过数，也不会清除已观察到的失败或上线验收门槛。

| 项目 | 本轮状态 / 缺少条件 | 用户回来后的操作与验收 |
|---|---|---|
| P0-01 / P0-02 正常 IDE 链路 | 跳过；需隔离 IDE 的实际网络操作与授权 | 在独立用户目录用当前 Rust 代理做最小对话，保持 TLS 校验开启；记录实际请求栈、CA 信任与 CONNECT 成功证据 |
| P0-03 无账号初始化 | 跳过；需人工观察干净 IDE | 使用另一个从未登录的独立用户目录，仅设置测试代理，验证是否能初始化并正常对话；无法完成时记录真实限制 |
| 路由 / 编码清单、P0-05 | 跳过；缺可用测试账号与受控原生响应 | 完成短回复、长回复、工具、多轮和取消，分别保留时序、HTTP 状态、编码、终止与 usage 元数据 |
| P0-06 / P0-07 真实用量 | 跳过；缺单笔响应及可靠官方消耗记录 | 给同笔请求关联官方实际消耗；禁止其他并发消耗干扰；离线核对，人工确认请求归属与官方展示精度 |
| P0-04 / P0-08 | 待实现，不能用跳过替代开发 | 原生路由与身份适配验证后实现云端桥接，之后验证选号重签、客户无上游凭据及跨会话隔离 |
| Windows 工具编辑与诊断回归 | 本轮跳过；原编辑尚无保存确认 | 确认原编辑与所有保存已停止后执行 `Chat: Recover Pending File Edit`，恢复后核查工具并完成 UTF-16LE、账号来源、退出码与清理回归 |

测试账号只在独立本机目录授权，不把 Token、密码、私人数据库、聊天正文或原始响应发到聊天或仓库，不退出或更改现用 IDE 的登录。真实响应留在 `.local/protocol/`。

拿到同笔官方消耗后，用 [离线核对工具](../server/README.md#离线核对同笔官方消耗) 运行 `--official-credits` 与 `--summary`。`matched` 仅证明整数微积分相等；`request_correlation_verified` 始终 false，关联与业务完成仍由人工验证。没有同笔官方记录时不要填演示数值或将账号余额代入。

自动回归、人工样本核对与上线验收分开记录；本轮跳过项均保持未勾选。

## 2026-10-27：离线工具验证结果

协议工具已实现于 `server/qoder_cloud/`。执行 `python3 -m unittest discover -s server/tests -v`：25 项通过。已验证人工原生 SSE 夹具输出 125000 微积分（0.125 Credits），明确标记 `diagnostic_only: true`。

冻结依赖安装配置已使用 `pnpm@10.11.0 install --frozen-lockfile --lockfile-only --ignore-scripts` 检查通过。前端工作区中的构建脚本白名单已移除原来的无效占位值。

这些结果仅证明解析工具和人工夹具行为，不证明真实 IDE 协议已经兼容。CLI 用法、输出字段与退出码见 [工具说明](../server/README.md)。

P0-A 的 Windows 编译、11 项 Rust 回归和前端构建结果见 [验证记录](verification/2026-10-27-p0-a.md)。证书安装与 IDE 信任仍为实机待验证，不能用生成证书的单元测试替代。

## 2026-10-27：P0-B 离线准备与安装清单核查

当前本机安装资源确认存在两类产品：`E:\Qoder IDE` 的 Qoder IDE 1.37.0（构建提交 `b371586dbb38a08acc6699d1a78ead98cfe3c452`），以及 `%LOCALAPPDATA%\Programs\Qoder` 的桌面 App 0.4.11（构建提交 `d9996760e3d4f61dedf1589a76f87d7001618ebf`）。这是安装信息，不证明实际使用区域、用户选择的首发产品或代理兼容性；旧记录 0.4.8 不作为当前验收版本。

历史日志还包括 qodercli 流量，发现以下候选入口。仅记录公开主机与固定路径，不保留查询串、身份或原始日志；不把它们当成已验证的 IDE 路由，也不据此扩大默认 TLS 解密范围。

| 候选入口 | 历史日志路径 | 仍需验证 |
|---|---|---|
| `api3.qoder.sh` | `/algo/api/v2/service/pro/sse/agent_chat_generation` | 是否由所选 IDE 使用、方法、认证、编码与单次用量 |
| `api3.qoder.sh` | `/algo/api/v2/service/business/finish`、`/api/v2/model/list` | 业务含义、是否计费、用量是否可关联 |
| `api2.qoder.sh` | `/api/v2/model/list`、`/algo/api/v1/ping` | 与实际区域及 IDE 的关系 |
| `repo2.qoder.sh` | `/algo/api/v2/service/codebase/embedding` | 代码库功能、身份范围及是否产生单次用量 |

离线准备已实现并在 Windows Python 3.13.3 下验证：

- `python -m unittest discover -s server/tests -v`：50 项通过，全部人工数据。
- 人工 SSE 夹具报告 v2 仍输出 125000 微积分，`stream_state: sse_done`、`diagnostic_ready: true`、`diagnostic_only: true`。
- `--content-encoding gzip/deflate` 支持有界增量解码，压缩输入与解压正文各限 64 MiB。截断、校验错误、尾随数据与解压超额失败，不输出部分报告。
- SSE 在完整事件边界结束但缺少 `[DONE]` 时为 `sse_end_unconfirmed`；尾事件残留为 `sse_incomplete_event`。即使已有有效 Credits，CLI 仍退出 2；用量快照回退同样待核对。
- 已新增 Windows / Ubuntu × Python 3.10 / 3.13 的 CI 配置，尚未获取远端运行结果。当前批次未修改 Rust / 前端，未重跑其构建。

本次没有安装系统证书、修改用户 IDE / 账号配置、发起真实上游对话或验证官方消费记录。P0-01 至 P0-06、P0-08 均保持待验收；后续需先确定隔离测试环境与实际产品，再取得受控响应样本。

## 2026-10-28：本机辅助链路复测

独立目录 `.local/windows-p0/` 留有上轮准备、证书安装及 TLS 测试报告。本次只读精确指纹确认测试 CA 仍在 Windows ROOT，不重新安装证书。实际 Electron 可执行文件为 `E:\Qoder IDE\Qoder IDE.exe`，不是 `E:\Qoder IDE\Qoder.exe`。

```powershell
& ".local\p0-venv\Scripts\python.exe" -m server.qoder_cloud.windows_p0 tls-check --electron "E:\Qoder IDE\Qoder IDE.exe"
```

复测结果：Python loopback CONNECT 与 Windows 默认 CA TLS 通过；`ide_node_default_ca.authorized: false`，错误为 `UNABLE_TO_VERIFY_LEAF_SIGNATURE`；独立子进程的 `ide_node_system_ca.authorized: true`。只改变测试子进程环境，未关闭 TLS 校验、未启动真实对话、未改当前会话的环境或代理。

此测试的 `scope` 是 `loopback_helper_not_rust_proxy`。它没有覆盖 IDE Chromium / 原生请求栈、Rust 代理、云端签名或工具调用。当前工具即使存在默认 Node 信任失败也可能退出 0，必须检查报告字段，不能用命令成功退出替代 P0-02 验收。

账号检查当前未找到可用凭据，未调用官方计划 / 配额接口或消耗额度。已复现的无 BOM UTF-16LE 识别条件错误及其他诊断完善仍待修复；工具编辑停在 IDE 保存确认阶段，临时新增测试已撤回，原有 50 项离线协议测试通过。恢复编辑后先补齐工具回归，再通过隔离测试账号获取真实响应和官方扣费证据。

## 2026-10-28：不依赖实机资源的后续实现

- 协议回归从 50 项扩展为 95 项：严格 JSON 重复键与 64 层深度、极端数字指数安全失败、SSE 实际 CRLF / UTF-8 字节边界、裸 data 字段和尾部事件判定、非 2xx 成功判定、跨 null / 部分快照的累计回退、缓存 / 推理 Token 回退与信封结束标记深度。
- 固定种子拆包测试覆盖 identity / gzip / deflate × CR / LF / CRLF × 10 种分块，报告与整段输入一致；全部样本为人工数据。
- 新增离线 `--official-credits` 和 `--summary`，仅核对手工数字；未知、不完整、上游异常或不一致仍退出 2，非法数值安全退出 1，不访问账号。CI 添加人工 SSE 的离线数字核对步骤，远端运行未核验。
- 上述实现不读取待保存的 Windows 工具、不影响现用 IDE / 代理 / 证书，不代表实机或平台账本交付。人工测试按上表留待用户回来后验证。

## 2026-10-28：Windows 辅助工具回归补齐

`decode_ide_secret()` 的无 BOM UTF-16LE 判据缺陷已修复（原实现对 4 字节切片与 3 字节 NUL 做定长比较，条件恒为 false），改为按奇偶字节的 NUL 分布判断，并把所用编码写入 `safe_decode_metadata.decoded_encoding`。同时修复只读数据库句柄泄漏：`with sqlite3.connect(...)` 只提交事务、不关闭连接，`account_inventory` / `account_check` 改为 `contextlib.closing`，不再持续占用 `state.vscdb`。

`account-check.json` 的 `secret_decode_state` 收敛为四个固定值：`unsupported_container`、`decoded_content_not_supported`、`dpapi_failed`、`decrypted_secret_not_text`；`HTTPError` 显式 `close()`，避免 `-W error` 下的 ResourceWarning。

新增 `server/tests/test_windows_p0.py` 33 项纯离线回归：DPAPI、证书构建、socket 与报告层全部替换为本地假实现，只使用临时目录，不访问真实证书库、账号库或网络。覆盖容器解析（含无 BOM UTF-16LE 回归）、token 提取边界、端点白名单、CONNECT 目标校验、盘点不泄漏取值、多候选不自动验证、CA 身份不完整 / 不匹配 / 过期拒绝，以及 CLI 路径约束与固定失败文案。全量 `python -m unittest discover -s server/tests -q` 为 128 项通过（协议 95 项 + 工具 33 项）；以旧判据做变异校验时新测试 2/2 失败，可确认回归有效。

本机 Python 3.14.8 未安装 `cryptography`，因此 `prepare` / `install` / `tls-check` 的证书与 DPAPI 路径**未在本机重跑**。上表 P0-01 / P0-02 的验收状态不变；无 BOM UTF-16LE 修复后仍需用隔离测试账号的真实容器复测 `candidate_count` 与 `usable_credential_verified`。

## 2026-10-28：本机 Windows 22H2 辅助链路实测

环境：`.local/p0-venv` + `cryptography` 50.0.2；本机无 `E:\Qoder IDE`，实际 Electron 可执行文件为 `%LOCALAPPDATA%\Programs\Qoder\Qoder.exe`（桌面 App，含 `app.asar`）；预检确认 15888 / 15889 空闲、`.local/windows-p0/` 原本为空（不存在遗留测试 CA）。

- `prepare`：生成独立 EC P-256 测试 CA，SHA-256 `f7c5e110…`、指纹 `4F00FE2327E632CBD97DE2A7244860AF5B6EA612`，有效期至 2026-11-27，CA 私钥为当前用户 DPAPI 密文。
- `install`：写入当前用户 ROOT 后按 SHA-256 精确指纹复核为 true。
- `tls-check`：loopback CONNECT 与 Windows 默认 CA TLS 通过；`ide_node_default_ca.authorized: false`（`UNABLE_TO_VERIFY_LEAF_SIGNATURE`），仅独立子进程 `NODE_USE_SYSTEM_CA=1` 时为 true。与上轮记录一致，说明该 Electron 默认不读 Windows 用户根库，后续代理方案必须显式注入 `NODE_EXTRA_CA_CERTS` / `NODE_USE_SYSTEM_CA`，否则 IDE 会被自身 TLS 校验拦住。
- `account-inventory`：`storage.json`（3319 B）无可识别顶层字段；`auth.v1.dat`（554 B）非 JSON；`proxy-tokens.json` 不存在；`state.vscdb` 有 5 条 `secret://`，其中 1 条属 `aicoding.` 且为 `{type: Buffer, data}` 容器，另 2 条 auth 类记录值不是 JSON。用户选择仅盘点，**未跑 `account-check`**，`usable_credential_verified` 保持 false。
- 全部报告在 `.local/windows-p0/`（已忽略），未发起推理请求、未刷新凭据、未改现用 IDE 配置。
- 测试 CA 仍在当前用户 ROOT；清理命令：`certutil -user -delstore Root 4F00FE2327E632CBD97DE2A7244860AF5B6EA612`。P0-01 / P0-02 仍需隔离账号的真实对话与官方逐笔消耗才能验收。

## 2026-10-28：桌面 App 凭据容器接入只读验证

上一节“仅盘点”的限制已解除：`account-check` 在本机跑通，并暴露出三处真实缺陷，均已修复并补回归。

- **非目标容器污染状态**：`state.vscdb` 里存在无法解析的 `secret://` 描述和非 `aicoding.` 扩展的密钥行，旧实现让它们参与状态判定，把真正的 Qoder 结果覆盖成 `decoded_content_not_supported_json`。现在描述解析失败或扩展名不符时直接跳过，只有目标行影响 `secret_decode_state`。
- **`v10` 容器算法错误**：IDE 内那条 Qoder 密钥是 `v10` + AES-256-GCM，旧实现拿纯 DPAPI 去解必然失败（表现为 `dpapi_failed`）。现实现先试纯 DPAPI，失败再按 Chromium 规则用 `Local State` 的 `os_crypt.encrypted_key`（DPAPI 包装的 32 字节密钥）解密。该容器解出 20 字节，内容只有 `{"platform": …}`，**不含 token**。
- **桌面 App 容器**：`%APPDATA%\com.qoder.app.stable\auth.v1.dat`（554 B，`v10`）同样走 AES-256-GCM，但密钥必须来自**同级目录**的 `Local State`，用 IDE 目录的密钥解会失败。解出 523 字节 JSON，字段为 `authenticationContext / expiresAt / refreshToken / refreshTokenExpiresAt / schemaVersion / token / user`，`token_extractable: true`。

候选选择改为按身份去重：`candidate_count` 是能取出 token 的容器数，`distinct_identity_count` 是不同 token 数；同一 token 只验证一次并优先字段更完整的快照，不同 token 视为不同身份、自动验证被阻塞为 `multiple_distinct_identities`，必须用 `--source` 显式指定。本机两个容器是**两个不同身份**（`com.qoder.app.stable` 有效期至 2026-11-18，`com.qodercn.app.stable` 至 2026-10-29），因此显式指定了有效期更长的前者。

```powershell
& ".local\p0-venv\Scripts\python.exe" -m server.qoder_cloud.windows_p0 account-check --source com.qoder.app.stable
```

实测结果：`user_plan` 与 `quota_usage` 均 `http_status: 200` / `valid_json: true`，`upstream_remaining_micro: 743000000`（即 743.0 Credits 剩余），`verified_source: com.qoder.app.stable`，`usable_credential_verified: true`。独立快照写入 `account-snapshot.dpapi`（812 B 的 DPAPI 密文，非明文），解密后的 token 指纹与 `com.qoder.app.stable` 一致、与 CN 容器不同。同一轮 `existing_login_modified: false`、`token_refreshed: false`、`upstream_inference_requests: 0`，未改现用 IDE 配置、未走推理接口。

回归从 128 项扩到 144 项（协议 95 + 工具 49），新增覆盖 `v10` 降级解密、真实 AES-GCM 往返（缺 `cryptography` 时跳过）、桌面容器只读验证、行过滤、同身份去重、身份冲突阻塞、显式 `--source` 与未知来源拒绝；对去重、显式选择、字段优先、来源优先四处逻辑做变异校验均被测试杀死。

本次只证明“只读取凭据 + 官方只读配额查询”可用，并提供了 743.0 Credits 这一官方基线数字。P0-01 / P0-02 仍未验收：还需要隔离账号的一次真实对话，并用同一工具对比请求前后的 `upstream_remaining_micro`，才能证明代理链路上的逐笔消耗与官方口径一致。

## 2026-10-28：实际产品版本、数据目录与端点清单

之前记录的“Qoder IDE 1.37.0”与本机不符，现按安装清单与 `product.json` 重录。

| 产品 | 版本 | 位置 | 其他事实 |
|---|---|---|---|
| Qoder IDE | **1.106.3**（quality stable，commit `b98cae7`，2026-10-16） | `D:\Qoder IDE\` | `updateUrl: https://center.qoder.sh/algo`，`dataFolderName: .qoder`，`cliName: qoder` |
| Qoder 桌面 App | 安装项 **0.4.9**；`.qoder-versions` 已下载 **0.4.11** | `%LOCALAPPDATA%\Programs\Qoder` | `appId: com.qoder.app`、`dataDirectoryName: com.qoder.app.stable`、channel `stable`，更新下载基址 `https://download.qoder.com.cn/qoder-app/releases` |
| 本仓库旧构建 | Qoder Proxy 1.1.0 | `%LOCALAPPDATA%\Qoder Proxy` | 与本次实机验证无关，未启动 |

两个桌面数据目录的活跃度可以直接量出来，不需要猜：

- `%APPDATA%\com.qoder.app.stable`：**在用**。1698 个文件，`auth.v1.dat` 最后写入 2026-10-27，`main.sqlite` 10.3 MB。
- `%APPDATA%\com.qodercn.app.stable`：**旧客户端残留**。2636 个文件，最后一次写入 2026-10-12，`main.sqlite` 48 MB，另有 `auth.machine-id`、`chat-session-turn-payload-buffer.sqlite`、`sessionMigration.sqlite` 等只有较新构建才有的文件。

因此上一节显式选 `com.qoder.app.stable` 既符合“有效期更长 / 字段更全”，也符合“目录仍在写入”。工具现在把这条依据固化进排序：字段完整度 → 容器写入时间 → 固定来源顺序，并在报告里输出 `container_modified_at` 与 `data_directory_files` 两个可复查字段。

端点清单来自 IDE 1.106.3 自带文件，不靠抓包：

- `out/vs/code/electron-utility/sharedProcess/sharedProcessMain.js` 的角色常量：`DEFAULT_LOGIN_BASE_URL=https://qoder.com/device/selectAccounts`、`DEFAULT_OPENAPI_BASE_URL=https://openapi.qoder.sh`、`DEFAULT_CENTER_ENDPOINT=https://center.qoder.sh`（大模型）、`DEFAULT_QUEST_SERVER_ENDPOINT=https://qts2.qoder.sh`、`DEFAULT_DYNAMIC_ENDPOINT=https://api2.qoder.sh`。
- 同一文件里的区域机制：`/algo/api/v2/service/region/endpoints`，缓存文件 `User/region-config-cache.json`，刷新间隔 1800 s，且 `REGION_CACHE_OVERRIDES_STATIC_ENDPOINTS` 允许缓存覆盖上述静态默认值。
- 本机 `User/region-config-cache.json`（315 B，`fetchTime` = 2026-10-26T05:07:06Z）实际内容：`preferredInferenceNode = https://api3.qoder.sh`、`preferredNesNode = https://api5.qoder.com.cn`、`preferredCodebaseNode = https://api2.qoder.sh`。该缓存已超过刷新周期未更新，说明该次读取时 IDE 近期未联网刷新。

这条证据直接推翻了一个假设：产品默认的 `target_domains`（`center.qoder.sh` + `openapi.qoder.sh`）**并未覆盖真正的推理与补全节点**，所以按旧默认值运行，代理看不到计费流量，P0 逐笔核对无从谈起。已改为四个：`center.qoder.sh`、`openapi.qoder.sh`、`api3.qoder.sh`、`api5.qoder.com.cn`；代码库节点 `api2.qoder.sh` 不计费且请求体会带上整段代码，有意不默认解密。改动在 `src-tauri/src/config.rs`（`DEFAULT_TARGET_DOMAINS` + `default_target_domains()`，`state.rs` 复用同一份）并新增 3 项单元测试。

**该改动已在本机编译验证（2026-10-28）**：1.96.0 工具链原先的 `rust-lib` 只剩元数据桩，`rustc` 连 `fn main` 都过不去；已用镜像源重装该工具链（`rustup toolchain uninstall 1.96.0-x86_64-pc-windows-msvc`，再以 `RUSTUP_DIST_SERVER=https://rsproxy.cn` 执行 `rustup toolchain install 1.96.0-x86_64-pc-windows-msvc --profile minimal --no-self-update`，仅对单次命令生效，未写用户级环境变量）。重装后 `rustc +1.96.0 --version` 与 hello world 均正常，`cargo +1.96.0 test --locked --manifest-path src-tauri/Cargo.toml --no-default-features --lib` 输出 **22 passed; 0 failed**（历史 11 项 + 区域配置 8 项 + 热更新 3 项）。历史记录里的“11 项 Rust 单元测试”仍只能作为旧证据，当前数值以 22 项为准。

`cargo check --all-targets` 会停在 `tauri::generate_context!()`：`frontendDist` 指向的 `../dist` 不存在（本仓库未包含前端构建产物），与本次改动无关；CI 的 `windows-native` job 同样只跑 `--lib`，前端构建由另一个 job 负责。

区域配置可被服务端随时改写，固定四个域名仍会在下一次区域变更时失效。现已改为运行时读取：`config.rs` 新增 `region_cache_path()` / `region_billing_domains()` / `effective_target_domains()`，`state.rs` 的 `AppState::new` 在启动时读缓存并把推理 / 补全节点并入拦截范围。

合并规则有意保留用户意图：名单为空或仍等于产品默认时才并入区域节点；自定义过名单则原样使用，不悄悄加回用户删掉的域名。缓存缺失、不是合法 JSON、端点不是 https、主机名形状不合法时一律返回空表并回退到常量，不猜也不报错。对应 5 项新单测（区域节点并入、定制名单不变、重复去重、只读计费节点、坏缓存返回空），并对“合并条件、去重、https 校验、节点范围”四处做过变异校验，结果 4/4 均被杀死。

运行中也会跟随：`state.rs` 的 `spawn_region_refresh()` 按 1800 秒（与 IDE 自身刷新周期一致）重读缓存，名单变了就热更新 `RequestLogger`，不需要重启代理；缓存消失或读不出计费节点时保持现有名单，不会把拦截范围清空。这项对长时间开着的客户端有意义——否则 IDE 一换节点，代理就静默失效。另加 3 项 `#[tokio::test]`，对“空缓存不覆盖名单”与“不看用户配置”两处变异校验 2/2 被杀死。

新增的 `windows_p0 endpoint-check` 就是这件事的离线部分：它只读同一份缓存，输出每个节点的角色、是否计费、是否已在默认范围内，并给出 `missing_billing_domains` / `unbilled_domains_in_scope` / `suggested_target_domains` 与缓存年龄。2026-10-28 本机实测结果：三个节点分别命中 `api3.qoder.sh`（推理，计费）、`api5.qoder.com.cn`（补全，计费）、`api2.qoder.sh`（代码库，不计费），`within_default_scope: true`、`missing_billing_domains: []`、`unbilled_domains_in_scope: []`；`region_cache_fetched_at` 为 2026-10-26T05:07:06Z，`region_cache_age_seconds: 174607`（约 48.5 小时），因此 `region_cache_stale: true`。也就是说“默认范围覆盖了计费节点”这个结论成立，但依据是两天前的缓存，不能当作当前路由。

该子命令只报告不修改：Python 侧名单与 `src-tauri/src/config.rs` 的 `DEFAULT_TARGET_DOMAINS` 由跨语言一致性测试（读取 `config.rs` 源码比对四个域名）守住，名单漂移会先让测试失败，而不是让线上代理静默少解密。新增 9 项离线回归，全量 159 项通过；新逻辑的四处变异（漂移置空、把代码库计入计费、忽略过期判定、取消 https 校验）均为 4/4 被杀死。
