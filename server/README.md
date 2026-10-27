# 云端代码与协议验证工具

当前交付是 P0 离线协议采样工具，还没有开放推理服务、激活服务或客户计费接口。`diagnostic_only: true` 的报告只能用于协议验证，不能作为真实账单，也不代表 IDE 到云端链路已经通过。

## 运行测试

需要 Python 3.10+，本阶段不需要安装第三方依赖：

```bash
python3 -m unittest discover -s server/tests -v
```

## 采样本机响应

先用仓库内人工构造的样本确认工具能运行：

```bash
python3 -m server.qoder_cloud.probe --input server/tests/fixtures/synthetic-native.sse
```

真实响应留在被 Git 忽略的 `.local/protocol/` 目录。只从本地文件或标准输入读取，不请求上游、不读取浏览器或 IDE 账号库：

```bash
python3 -m server.qoder_cloud.probe \
  --input .local/protocol/raw-response.sse \
  --format sse \
  --output .local/protocol/reports/usage-001.json

python3 -m server.qoder_cloud.probe \
  --input .local/protocol/raw-response.json \
  --format json
```

输出仅含白名单中的数字、状态、用量和统计。不包含请求头、账号、模型回复、工具参数或错误正文。输出文件只允许新建，不覆盖输入或已有报告。

| 字段 | 含义 |
|---|---|
| `usage_state` | `credits_observed`、`credits_missing`、`usage_absent`、`invalid_usage`、`upstream_error` |
| `credits_micro` | 精确整数，1 Credit = 1,000,000 单位；缺失为 null，不能按 0 结算 |
| `usage_snapshots` | 观测到的 usage 快照数；累计快照不相加 |
| `snapshot_regressions` | 后一个快照比前一个更小的次数；提示需要核实该协议是否为累计值 |
| `done_seen` | 是否观察到 `[DONE]` 标记；不单独证明业务成功 |
| `incomplete_event` | EOF 时是否残留不完整 SSE 事件；该事件被丢弃，不作为用量 |
| `diagnostic_only` | 始终为 true，真实结算还需要服务器关联、幂等与完成状态 |

退出码：0 表示观测到 Credits 且没有不完整尾事件；2 表示未形成这种观测结果；1 表示解析、编码、大小或文件操作失败。0 不表示真实账号、计费或整个流已验证通过。

## 支持范围

支持 JSON 响应里的 `usage`，以及含 `statusCodeValue` 和 `body` 的原生 JSON / SSE 信封；`body` 可以是 JSON 对象或 JSON 字符串。SSE 支持 UTF-8 拆包、BOM、CR / LF / CRLF、多个 data 行和注释。

只解开明确识别的信封，不递归搜索对话正文中的同名字段。默认总输入上限 64 MiB、单事件上限 2 MiB。压缩包、protobuf、其他二进制协议不在当前解析范围；不能因此声称所有 Qoder IDE 流量都兼容。

只有 `server/tests/fixtures/` 下明确标注为 synthetic 的人工夹具可以提交。真实账号凭据、原始响应和对话不得进入仓库。下一步验收见 [协议验证操作](../docs/protocol-validation.md)。
