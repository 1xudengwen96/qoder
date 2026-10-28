# 开发进展与验证记录

主仓库：`https://github.com/1xudengwen96/qoder`；主分支：`main`。日期按开发环境本地日期记录。没有真实验证证据的能力不能标记为完成。

## 2026-10-27：启动开发

已确定的产品要求：

- 云端统一维护上游账号池，客户端不获取上游账号 Token。
- 客户安装 Windows App、输入激活码，获得固定额度与有效期。
- App 初始化本机独立 CA 与代理，客户手动设置 HTTP 代理。
- 客户界面采用参考截图的黑金风格，包含卡片、余额、明细与证书管理。
- 客户确认已具备 Windows Qoder 环境及有可用额度的测试账号。

已完成：

- 完整实施方案、项目分工、阶段依赖、工作量估算与验收标准归档。
- 对比 Switch、Qoder2API-Hub、Autopilot 源码，并记录固定评估提交。
- 新仓库检查为空；原项目完整历史保留，`origin` 改为用户指定仓库，原仓库保留为 `upstream`。
- 主分支命名为 `main`；后续按可验证的改动分批提交，不强制覆盖远端历史。

当前阶段：P0 协议验证。

启动时计划（完成情况见后续记录）：

- 脱敏的原生协议用量采样、SSE 信封解析与缺失用量识别。
- 自动化覆盖：真实零额度与字段缺失、重复累计用量、流拆包、嵌套信封错误、数据脱敏。
- 持续集成：前端构建与无账号依赖的协议测试。

尚未验证：

- 干净 IDE 在没有客户自备上游账号时的初始化。
- 目标 Windows IDE 是否信任自动安装的当前用户 CA。
- 云端重签后的原生推理、工具调用、会话和取消链路。
- 真实原生请求的单次 Credits 归属及计费边界。

环境情况：

- 当前工作环境为 Linux，已安装 Node.js 22，前端依赖已安装。
- 初始前端 `npm run build` 通过；浏览器预览已启动过。
- 当前 PATH 中没有 Rust 工具链；Windows Tauri 编译与实机验证须通过 Windows CI / 测试环境执行。
- Docker 服务可用；后续用于隔离验证 PostgreSQL 与云端服务，不把测试数据放进生产账号库。

发布约定：每次有明确阶段结果就提交代码、同步本记录并推送主仓库。提交不等于商业版本可用；P0 四项验收通过之前，不标记云端闭环完成。

## 2026-10-27：首批实现与 Windows 验证环境

- 文档提交 `2894c68` 已推送到新仓库 `origin/main`。
- 完成 P0 离线用量规范化、原生信封 / SSE 解析与白名单采样 CLI；25 项自动化测试通过。
- 人工构造的原生 SSE 夹具输出 0.125 Credits，报告始终标为诊断数据。
- 加入 GitHub Actions 前端构建、协议测试和人工样本验证。
- 固定 pnpm 10.11.0，修复 workspace 中无效的构建白名单占位值；冻结锁文件配置校验通过。
- 只读确认本机 Windows 已安装 Qoder 0.4.8；当时未运行 Qoder 进程。
- Windows 已有 Rust stable 1.96.0，但缺失 core 标准库元数据，初次依赖编译失败。安装了独立命名的 `1.96.0` 测试工具链，没有更改用户默认工具链。
- 证书初始化修复已在 Windows 下编译，3 项证书单元测试通过；没有调用安装证书接口、没有修改 IDE 配置。该修复将单独提交。

真实原生响应尚未采集。下一步是修复实时流式透传、并发请求上下文与默认日志隐私，再进入 Windows 账号和证书信任实测。

## 2026-10-27：P0-A 代理基础改造完成

实现：

- `ensure_ca()` 统一负责首次生成证书；安装证书不再要求先启动代理。重复或并发初始化保留同一 CA 身份；证书 / 私钥不完整时明确失败，不自动覆盖。
- Hudsucker 请求 / 响应关联改为每个 handler 实例独立保存，修复共享“最后请求”状态造成的并发串日志。
- 响应不再为日志调用 `body.collect()`；流式数据、响应头与 trailers 原样返回，不等待整个回复结束。
- 无关请求不预读完整正文；只有配置目标域名的 CONNECT 才解密。非目标域名的 401/403 不触发 Qoder 换号。
- 读取目标请求正文失败时返回本机 502，不能吞掉错误后转发成空内容。认证头须全部解析成功后才替换。
- 默认日志不保存请求 / 响应正文；认证、Cookie、账号标识和未知头值脱敏。磁盘请求日志使用实际关联 ID。
- 新增 Windows CI，使用独立的固定版本 Rust 工具链运行全部本机库测试。

验证：

- Windows `cargo +1.96.0 test --locked --no-default-features --lib`：11 项通过；包括 4 项证书、6 项代理和 1 项日志隐私测试。
- 代理测试证明永不结束的响应不被预读、data / headers / trailers 保留、并发响应各自归属、非目标正文不被消费、CONNECT 域名匹配和正文错误不会被吞掉。
- 本阶段不安装 Windows 信任证书，不启动 / 修改真实 IDE，不调用或扣费任何上游账号。
- 协议工具提交 `2d2aea2` 已推送到 `origin/main`；本次代理改造将继续提交同一分支。
- GitHub Actions 配置已纳入仓库；未将远端 CI 状态写成已通过，实际以对应运行结果为准。

剩余工作已完整拆分到 [交付清单](remaining-work.md)。下一阶段是 P0-B Windows 真实协议、证书信任和单次 Credits 验证；本阶段完成不代表云端号池或客户计费已经上线。

本阶段前端构建通过，构建 57 个模块；详细命令与证据边界归档到 [P0-A 验证记录](verification/2026-10-27-p0-a.md)。代码、测试、CI、验收记录与全部后续计划一并同步 `main`。

## 2026-10-27：进度复核与 P0-B 离线准备

进度复核：

- 当前 HEAD 为 `1b7141b`，与 P0-A 代理修复记录一致；`server/` 目前只有离线解析工具，未实现云端服务、账号凭据加密、客户授权、激活码或 PostgreSQL 账本。继承的本地账号管理界面不等于云端客户 App。
- 只读核对本机安装清单：Qoder IDE 1.37.0 与另一桌面 App 0.4.11 均存在。旧记录中的 0.4.8 不能代表当前 IDE 版本；首发实际产品和区域仍需确定。
- 历史日志包含 qodercli 流量，api2 / api3 / repo2 作为候选入口记录在 [协议验收手册](protocol-validation.md)，未判定 IDE 已兼容。撤回本轮临时自动扩域名 / 迁移配置改动，默认 TLS 解密范围与用户配置保持原状。
- 当前 Windows shell PATH 的 Python 指向 WindowsApps 占位入口，但专用文件搜索找到了已有 uv 管理的 Python 3.13.3，通过绝对路径执行；未安装或更改系统工具链。

本批实现：

- 显式 `--content-encoding identity/gzip/deflate`。单成员 gzip、带 zlib 包装的 deflate 增量解码，原始输入与解压输出各限制 64 MiB；拒绝校验和错误、截断、多成员 / 尾随数据和未知编码。
- 报告 v2 新增编码、输入 / 解压字节数、流结束判定与待核对原因。完整 usage 事件后无 `[DONE]` 的 SSE 不再退出 0；不完整尾部、用量快照回退同样退出 2，已有快照仅为观测值。
- 增加 25 项人工数据回归；协议测试共 50 项通过，含压缩拆包、大小边界、压缩炸弹、结束判定、文件 / stdin CLI 和脱敏。原 SSE 夹具仍为 0.125 Credits（125000 微积分），`diagnostic_only: true`。
- CI 配置扩展为 Windows / Ubuntu × Python 3.10 / 3.13。尚未核验远端运行状态，不把配置完成写成远端测试通过。

本机可复现命令（仓库根目录，使用已核验的 Python 路径）：

```powershell
& "$env:APPDATA\uv\python\cpython-3.13.3-windows-x86_64-none\python.exe" -m unittest discover -s server/tests -v
& "$env:APPDATA\uv\python\cpython-3.13.3-windows-x86_64-none\python.exe" -m server.qoder_cloud.probe --input server/tests/fixtures/synthetic-native.sse
```

边界：未安装信任证书、未修改真实 IDE / 账号配置、未调用上游对话或核对官方扣费；本批未修改 Rust / 前端，未重跑其构建。P0-B 实机验收仍未通过，P1–P5 尚未交付。本批改动留在工作区，未提交或推送。

## 2026-10-28：恢复任务与本机 P0-B 证据复核

- Git HEAD 仍为 `1b7141b`；P0-B 离线解析、CI 和文档修改尚未提交，另有未跟踪的 `server/qoder_cloud/windows_p0.py`。不将工作区改动当成已发布版本。
- 上轮未归档的 `.local/windows-p0/certificate-status.json` 已记录独立测试 CA 安装。此次只读按 SHA-256 精确指纹在 Windows ROOT 中重新确认其存在，没有再次安装、删除或替换证书。
- 使用真实安装的 `E:\Qoder IDE\Qoder IDE.exe`，重跑 `tls-check`。15888 → 15889 的 loopback CONNECT 和 Windows 默认 CA TLS 通过；IDE 的 Node 默认报 `UNABLE_TO_VERIFY_LEAF_SIGNATURE`，仅在独立子进程设置 `NODE_USE_SYSTEM_CA=1` 时授权通过。未修改现用 IDE 环境或配置。
- 上述是 Python loopback 辅助代理与 Electron Node TLS 栈测试，不是 Rust 代理到 IDE 的正常对话，也不能证明 Chromium、原生 Qoder 进程或工具调用链均信任该 CA。P0-01 / P0-02 仍未完成端到端验收。
- 只读账号盘点找到加密存储，但没有验证出可用测试凭据；`candidate_count: 0`、`read_only_checks: []`。未刷新 Token、未导入账号、未请求上游推理，不能将容器存在当成可用账号。
- 人工新增诊断用例复现：`decode_ide_secret()` 的无 BOM UTF-16LE 判断切片取得 4 个字节，却与 3 个零字节比较，因此该分支不成立。这不解释 DPAPI 解密本身失败；两类问题需要分别验证。
- Windows 工具改进的文件编辑停在 IDE 保存确认阶段，尚未获得落盘确认；未重复覆盖该待保存文件。依赖该改进的临时测试已撤回，原有 50 项协议测试重新通过，`git diff --check` 通过。新增诊断回归未交付。

恢复后的顺序：先确认原编辑操作与所有保存已停止，再由用户执行 `Chat: Recover Pending File Edit` 恢复编辑确认；随后核对实际文件并完成编码识别、隔离账号来源、退出状态与连接清理的回归。再在独立 IDE 测试目录通过本机授权准备可用测试账号，执行最小对话、原生响应采样和官方扣费核对。不得修改当前会话或把 Token 发到聊天中。

本轮只完成上述复核与记录，未提交 / 推送，未重跑 Rust / 前端构建，P1–P5 仍未开始。

## 2026-10-28：用户不在场时继续独立离线开发

执行约定：缺账号、样本、设备或人工操作的测试本轮跳过，其他可独立工作继续执行，不等待常规方案选择。跳过不等于通过；已观察到的 IDE Node 默认信任失败仍保留，P0 真实链路是上线门槛。

本批实际落盘：

- `protocol.py` 拒绝重复 JSON 键和转义同名键；JSON 结构在解析前限 64 层，避免依赖不同 Python 版本的递归限制。极端 Decimal 指数统一为安全 `ProtocolError`，调用者关闭 Decimal trap 时也拒绝非有限解析结果。
- SSE 按真实 CR / LF / CRLF 与 UTF-8 字节计入事件限额；补裸 data 字段、结束后的空事件、EOF 注释 / 未知字段和信封结束标记深度判定。
- 非 2xx 原生信封不会被视为成功。显式 null 用量清除旧快照；累计高水位检测跨部分 / 缺失字段，覆盖 Credits、输入、输出、总、缓存与推理 Token。较早的无效用量与回退不会因后来有效快照而消失。
- `usage.py` 实现精确整数微积分核对，`probe.py` 提供 `--official-credits` 与 `--summary`。不访问官方 API、不读取账号、不修改余额；人工数字一致仍标记 `manual_unverified`、`request_correlation_verified: false`、`diagnostic_only: true`。
- 不一致、缺失或未确认样本退出 2；非法数值和解析错误退出 1，不输出私人字段、路径或 traceback。stdout 保持单份 JSON，摘要仅在 stderr，已有输出拒绝覆盖。
- 新增 45 项自动回归，协议测试累计 95 项；包含固定种子的 90 种编码 / 换行 / 拆包组合。CI 增加人工 SSE 的离线官方数字核对步骤，未声称远端运行通过。
- 更新工具说明、任务清单和 [人工测试登记](protocol-validation.md)。没有以空测试或 synthetic 数据代替真实验收；Windows 工具待保存编辑本轮跳过，不再次读取或覆盖该文件。

验证命令（仓库根目录）：

```powershell
& ".local\p0-venv\Scripts\python.exe" -m unittest discover -s server/tests -v
& ".local\p0-venv\Scripts\python.exe" -m server.qoder_cloud.probe --input server/tests/fixtures/synthetic-native.sse --official-credits 0.125 --summary
& "$env:APPDATA\uv\python\cpython-3.13.3-windows-x86_64-none\python.exe" -S -W error -m unittest discover -s server/tests -q
```

验证结果：Windows Python 3.13.3 下 95 项自动测试全部通过；再次禁用 site 自动加载并将警告视为错误运行，仍为 95 项通过。`git diff --check` 无错误。编辑器语言服务未就绪，未把静态诊断记为通过；远端 CI 与其他 Python / OS 组合仍以实际运行结果为准。

人工夹具核对返回 `matched`、差额 0、125000 微积分，仍仅诊断。真实对话、原生路由、官方逐笔消耗、无账号初始化与跨客户隔离留待有资源时验证；所需条件与步骤已集中登记。没有触碰当前会话、账号、代理或证书，未提交 / 推送。本批不修改 Rust / 前端，也不把 P1–P5 标成完成。
