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

## 2026-10-28：Windows 辅助工具缺陷修复与离线回归

- 修复 `decode_ide_secret()` 的无 BOM UTF-16LE 识别缺陷：原判据对 `clear[1:8:2]` 的 4 字节切片与 3 字节 `b"\x00\x00\x00"` 做定长比较，条件恒为 false，导致该容器落回 UTF-8 解码。现改为按奇偶字节的 NUL 分布判断，并抽出 `secret_text()` 返回所用编码，写入 `safe_decode_metadata.decoded_encoding`。
- 修复只读数据库句柄泄漏：`with sqlite3.connect(...)` 只提交事务、不关闭连接，`account_inventory` / `account_check` 改用 `contextlib.closing`。原实现在 Windows 下会持续占用 `state.vscdb`，使该文件无法被移动或删除。
- 收敛错误分类：非 JSON 容器统一为 `ValidationError`；`account-check.json` 的 `secret_decode_state` 只在 `unsupported_container` / `decoded_content_not_supported` / `dpapi_failed` / `decrypted_secret_not_text` 四值中取值；`HTTPError` 显式 `close()`，避免 `-W error` 下的 ResourceWarning。
- 新增 `server/tests/test_windows_p0.py`（33 项）：DPAPI、证书构建、socket 与报告层全部替换为本地假实现，只使用临时目录，不触碰真实证书库、账号库与网络。覆盖容器解析与无 BOM UTF-16LE 回归、token 提取边界、端点白名单、CONNECT 目标校验、盘点不泄漏取值、多候选不自动验证、CA 身份不完整 / 不匹配 / 过期拒绝，以及 CLI 路径约束与固定失败文案。
- 验证：`python -m unittest discover -s server/tests -q` 为 128 项通过（原 95 项 + 新增 33 项）；`python -W error -m unittest server.tests.test_windows_p0` 为 33 项通过。另以旧判据做变异校验，新测试 2/2 失败，确认回归能捕获该缺陷。本机 Python 3.14.8，未安装 `cryptography`：证书生成与真实 DPAPI 未在本机执行，实机结论仍以 `docs/protocol-validation.md` 的记录为准。未安装或删除证书、未读取现用账号、未请求上游，未提交 / 推送。

## 2026-10-28：`v10` 容器解密、桌面 App 凭据与候选身份去重

- 修复非目标行污染状态：`state.vscdb` 中无法解析的 `secret://` 描述、非 `aicoding.` 扩展的密钥行不再参与状态判定，只 `continue` 跳过，避免一行畸形记录把真正的 Qoder 结果覆盖成 `decoded_content_not_supported_json`。
- 修正 `v10` 容器算法：原实现拿纯 DPAPI 解 AES-GCM 密文，必然失败。现拆出 `os_crypt_key()` / `os_crypt_decrypt()`，先试纯 DPAPI，失败再按 Chromium 规则用 `Local State` 里 DPAPI 包装的 32 字节密钥做 AES-256-GCM 解密，并把方案写入 `safe_decode_metadata.container_scheme`（`dpapi` / `os_crypt_aes_gcm`）。
- 接入桌面 App 凭据：`desktop_auth_containers()` / `decode_desktop_auth()` 只读解密 `%APPDATA%\com.qoder.app.stable` 与 `com.qodercn.app.stable` 下的 `auth.v1.dat`；密钥必须取同级目录的 `Local State`。`account_check()` 不再因 IDE 数据库缺失而提前返回，新增 `desktop_auth_containers`、`decoded_container_type`、`recognized_account_fields`、`token_extractable`。
- 候选选择改为按身份去重：新增 `select_candidate()` / `candidate_rank()` / `verify_candidate()`，`candidate_count` 为可取出 token 的容器数，`distinct_identity_count` 为不同 token 数；同一 token 只验证一次并优先字段更完整、来源更靠前的快照（`SOURCE_PRIORITY`）；不同 token 视为不同身份，自动验证阻塞为 `multiple_distinct_identities`，需 `--source <label>` 显式指定，来源不存在时为 `requested_source_not_available`。
- 清理与回归：把只读校验与留档从 `account_check()` 拆到 `verify_candidate()`；测试增至 144 项（协议 95 + 工具 49），新增 `v10` 降级解密、真实 AES-GCM 往返（缺 `cryptography` 时跳过）、桌面容器只读验证、行过滤、同身份去重、身份冲突阻塞、字段完整度优先、显式 `--source` 与未知来源拒绝。
- 验证：venv（含 `cryptography` 50.0.2）与系统 Python 均 `144` 项通过（系统 Python 跳过 1 项 AES-GCM 用例）；对去重、显式选择、字段优先、来源优先四处逻辑做变异校验，4/4 均被测试杀死。实机 `account-check --source com.qoder.app.stable` 返回 `user_plan` / `quota_usage` 双 200，`upstream_remaining_micro: 743000000`，快照以 DPAPI 密文写入 `.local/windows-p0/account-snapshot.dpapi`，实测细节见 [协议验证记录](protocol-validation.md)。未刷新凭据、未改现用 IDE 配置、未发推理请求，未提交 / 推送。

## 2026-10-28：产品版本核实、候选排序可量化、补全默认解密范围

- 核实实际安装：Qoder IDE **1.106.3**（`D:\Qoder IDE`，commit `b98cae7`，2026-10-16）与桌面 App 安装项 **0.4.9**（`.qoder-versions` 已就位 0.4.11）。旧记录中的 1.37.0 是错的，已在文档更正；未沿用旧版本号。
- 候选排序不再依赖硬编码名单：`candidate_rank()` 改为字段完整度 → 容器写入时间 → 固定来源顺序，`desktop_auth_containers` 逐项输出 `container_modified_at` 与 `data_directory_files`。本机量得 `com.qoder.app.stable` 在用（1698 文件，2026-10-27 写入），`com.qodercn.app.stable` 为残留（2026-10-12 后未写入）。
- 新增 `modification_time()` / `iso_mtime()` / `count_files()`，都只读元数据、不读内容；取不到时按最旧处理，不阻断盘点。
- 从 IDE 自带文件整理端点与角色：`sharedProcessMain.js` 的 `DEFAULT_CENTER_ENDPOINT` / `DEFAULT_OPENAPI_BASE_URL` / `DEFAULT_QUEST_SERVER_ENDPOINT` / `DEFAULT_DYNAMIC_ENDPOINT` / `DEFAULT_LOGIN_BASE_URL`，以及区域机制 `/algo/api/v2/service/region/endpoints` + `User/region-config-cache.json`（刷新 1800 s，可覆盖静态默认值）。本机缓存实为 inference `api3.qoder.sh`、NES `api5.qoder.com.cn`、codebase `api2.qoder.sh`。
- 据此修正产品默认范围：`src-tauri/src/config.rs` 新增 `DEFAULT_TARGET_DOMAINS` / `default_target_domains()`（center + openapi + api3 + api5），`state.rs` 复用同一份，避免两处名单漂移；`api2.qoder.sh` 有意排除（不计费且带整段代码）。新增 3 项单元测试。
- 验证：Python 回归 `150` 项通过（协议 95 + 工具 55），新排序键三处变异（字段优先 / 时间优先 / 来源顺序）均被杀死；实机重跑 `account-check --source com.qoder.app.stable` 仍为双 200、基线 `743000000`。**Rust 改动未编译验证**：本机 rustup 1.96.0 的 rust-lib 只剩元数据桩，`rustc` 连 `fn main` 都编译不了，`rustup component remove/add rust-std` 也无法修复（需重装工具链），已在待办中单列。未提交 / 推送。

## 2026-10-28：`endpoint-check` 离线漂移检测

- 新增 `endpoint-check` 子命令：只读 `%APPDATA%\Qoder\User\region-config-cache.json`，输出每个节点的 `role` / `billed` / `in_default_scope`，以及 `missing_billing_domains`、`unbilled_domains_in_scope`、`suggested_target_domains`、缓存年龄，写入 `.local/windows-p0/endpoint-check.json`。只报告漂移，不改配置、不改代理。
- 缓存年龄取自文件里的 `fetchTime`（毫秒）而不是 mtime，与 IDE 的 1800 s 刷新周期比较，给出 `region_cache_stale`；取不到 `fetchTime` 时保持 `null`，不伪造新鲜度。`endpoint_host()` 只接受 https 且形状合法的域名，`http://` 或非 URL 一律当作空主机，不拿去和默认名单比对。
- 名单一致性：Python 侧 `DEFAULT_TARGET_DOMAINS` 与 `src-tauri/src/config.rs` 用跨语言测试守卫（直接解析 Rust 源码里的字符串字面量），任一侧改动遗漏都会让测试失败。
- 验证：新增 9 项离线回归，全量 `159` 项通过（协议 95 + 工具 64，系统 Python 跳过 1 项 AES-GCM）；对漂移置空、把代码库计入计费、忽略过期判定、取消 https 校验四处做变异校验，4/4 均被杀死。本机实测 `within_default_scope: true`、`missing_billing_domains: []`，但 `region_cache_fetched_at` 为 2026-10-26T05:07:06Z、`region_cache_age_seconds: 174607`，`region_cache_stale: true`，所以该结论只能算“两天前的路由”，不能当成当前节点。未提交 / 推送。

## 2026-10-28：修复 Rust 工具链并改为运行时读取区域配置

- 工具链修复：1.96.0 的 `lib\rustlib\...\lib` 为空（只剩清单与元数据桩），`rustc` 连 hello world 都报 `only metadata stub found for rlib dependency core`。彻底卸载后用镜像源重装（`RUSTUP_DIST_SERVER=https://rsproxy.cn` 仅对该条命令生效，`--profile minimal --no-self-update`），重装后 `rustc +1.96.0` 与 `cargo test` 均正常。选用 rsproxy 是因为 `static.rust-lang.org` 直连超时（os error 10060）。
- 拦截范围改为运行时驱动：`config.rs` 新增 `region_cache_path()` / `region_billing_domains()` / `effective_target_domains()`，从 `%APPDATA%\Qoder\User\region-config-cache.json` 读 `preferredInferenceNode` 与 `preferredNesNode`，`state.rs` 的 `AppState::new` 用它们计算实际拦截名单。`DEFAULT_TARGET_DOMAINS` 降为缓存缺失时的兜底。
- 合并规则保留用户意图：名单为空或仍等于产品默认时才并入区域节点；用户定制过名单则原样使用。缓存缺失 / 非法 JSON / 非 https / 主机名含非法字符时返回空表，不猜、不报错。主机名解析先剥凭证与端口，再校验字符集。
- 运行中跟随：`state.rs` 新增 `refresh_from_cache()` / `spawn_region_refresh()`，用 `tauri::async_runtime::spawn` 按 `REGION_REFRESH_SECONDS = 1800` 重读缓存，名单有变化才调 `set_target_domains`；缓存读不出计费节点时直接返回，不清空拦截范围。顺带让原本闲置的 `get_target_domains()` 有了实际用途。
- 验证：`cargo +1.96.0 test --locked --manifest-path src-tauri/Cargo.toml --no-default-features --lib` 输出 **22 passed; 0 failed**（历史 11 项 + 区域配置 8 项 + 热更新 3 项）；对合并条件、去重、https 校验、节点范围四处做变异校验，加上“空缓存不覆盖名单”与“不看用户配置”共 6/6 均被杀死；`cargo check --all-targets` 因仓库没有前端 `dist` 停在 `tauri::generate_context!()`，与本次改动无关（CI 也只用 `--lib`）。未提交 / 推送。
