# 架构决策

## ADR-001：组合现有项目

状态：采用；日期：2026-10-27。

Switch 承担桌面窗口、托盘、本机 CONNECT 代理、CA 生命周期和安装包。云端账号生命周期、区域与模型路由参考 Qoder2API-Hub。客户授权、权益和账本新建。

Hub 的兼容 API 不等于 IDE 的 HTTP 代理。原生转发必须保留经验证的请求和响应协议，不能无条件把 IDE 请求重写成 OpenAI Chat Completions。

## ADR-002：凭据只在云端

状态：采用。

客户 App 不能领取上游 Access Token / Refresh Token。原 `fetch_pool_account` 只属于继承的本机管理模式，不是云端客户发行版的接入方案。客户使用平台设备会话，由云端裁决额度并重签上游请求。

## ADR-003：先验证，后计费

状态：采用。

P0 未证明原生客户端可启动请求、信任 CA、完成云端推理并返回可归属用量之前，不承诺只设代理即可工作，不把模拟额度作为真实账单。

缺失 Credits 和真实 0 Credits 必须是不同状态；未知用量进入待核对，不自动免费。上游账号余额差与参考价格都不能直接做多客户结算依据。

## ADR-004：平台账本为唯一事实来源

状态：设计已确定，尚未实现。

PostgreSQL 保存权益、原子预留、结算、退款 / 释放与过期事件。Redis 只协调短期租约、限流和缓存，不存客户余额的唯一副本。激活与请求结算分别幂等。

首发客户并发限制为 1；所有上游账号由单一账号池所有者协调。扩容前必须完成分布式租约与凭据刷新互斥，不能共享原版 JSON 文件来伪装集群。

## ADR-005：采样默认最小化

状态：采用。

只提交人工构造的协议夹具。真实采样只输出严格白名单中的用量字段、状态和统计，不输出原始请求、认证头、邮箱、验证码或对话正文。真实原始样本不进入 Git；工具运行失败不回显包含凭据的原始片段。

## ADR-006：主仓库与持续更新

状态：采用。

主远端为 `https://github.com/1xudengwen96/qoder.git`，主分支 `main`。原 Qoder Switch 远端保留为 `upstream`，许可证和历史不删除。按文档、验证工具、代理修复、云端服务等可评审批次提交，不强推、不把凭据提交到仓库。

## ADR-007：解密范围只覆盖计费节点

状态：采用（已改为运行时区域配置驱动，2026-10-28）。

结论：启动时读取 `%APPDATA%\Qoder\User\region-config-cache.json` 的 `preferredInferenceNode` 与 `preferredNesNode`，把主机名并入拦截范围；`DEFAULT_TARGET_DOMAINS`（`center.qoder.sh`、`openapi.qoder.sh`、`api3.qoder.sh`、`api5.qoder.com.cn`）降为兜底默认值。代码库节点 `api2.qoder.sh` 不解密。

合并规则：名单为空或仍等于产品默认时合并区域节点；用户定制过名单则原样使用，不悄悄加回他删掉的域名。区域缓存读不到、不是合法 JSON、或端点不是 https 时，一律回退到常量，不猜。

理由：IDE 1.106.3 的区域配置缓存把推理（`preferredInferenceNode`）指向 `api3.qoder.sh`、补全（`preferredNesNode`）指向 `api5.qoder.com.cn`，两者都计入 Credits；只拦 `center` + `openapi` 会看到登录与配额、却看不到计费流量。代码库节点不产生 Credits，且请求体会携带大段源码，解密收益为零、隐私代价最高。

被否决的替代方案：全量解密 `*.qoder.*`（范围失控、无收益的正文暴露）；只保留旧的两域名（P0 逐笔核对永远拿不到数据）。

适用与失效条件：区域配置每 1800 s 可被服务端改写，所以常量不能作为唯一依据；现已按上述规则在 `AppState::new` 里读取（`region_cache_path()` / `region_billing_domains()` / `effective_target_domains()`），并由 `spawn_region_refresh()` 以同一周期后台重读、热更新 `RequestLogger`，常量只负责缓存缺失时兜底。重读时如果缓存消失了，保持当前名单而不是清空。若该缓存文件路径或字段名在产品新版本中改变，需重新确认；`windows_p0 endpoint-check` 可用于离线发现这种失效。

迁移影响：配置存于 `proxy-config.json`，读取时若文件不存在则用 `Default`。已确认本机 `%APPDATA%\Qoder Proxy` 与 `%LOCALAPPDATA%\Qoder Proxy` 都没有该文件，新默认值直接生效；若将来用户保存过自己的名单，按上述合并规则视为定制、原样使用，不会被区域配置悄悄改写。

已交付的缓解措施：`server.qoder_cloud.windows_p0 endpoint-check` 离线解析同一份缓存，比对常量并输出 `missing_billing_domains` 与 `suggested_target_domains`；Python 名单与 Rust 的 `DEFAULT_TARGET_DOMAINS` 由跨语言一致性测试守卫，代理侧则直接运行时读缓存。
