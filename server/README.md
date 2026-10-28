# 云端代码与协议验证工具

当前交付是 P0 离线协议采样工具，还没有开放推理服务、激活服务或客户计费接口。`diagnostic_only: true` 的报告只能用于协议验证，不能作为真实账单，也不代表 IDE 到云端链路已经通过。

## 运行测试

需要 Python 3.10+，离线协议与数字核对测试不需要第三方依赖。当前 95 项测试全部使用人工数据，不读取 Windows 实测工具或现用账号，也不会安装证书：

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
  --content-encoding identity \
  --output .local/protocol/reports/usage-001.json

python3 -m server.qoder_cloud.probe \
  --input .local/protocol/raw-response.json \
  --format json
```

若文件保留原始 gzip / deflate 响应正文，按 HTTP `Content-Encoding` 指定 `--content-encoding gzip` 或 `--content-encoding deflate`。例如：

```bash
python3 -m server.qoder_cloud.probe --input .local/protocol/raw-response.sse.gz --format sse --content-encoding gzip
```

如果抓包工具已解压正文，仍使用 `identity`（默认值），不要重复解压。输入应是已去掉 HTTP 头和传输分块框架的正文，不是完整 HTTP 报文；不猜测压缩或二进制格式。

输出仅含白名单中的数字、状态、用量和统计。不包含请求头、账号、模型回复、工具参数、gzip 原始文件名或错误正文。输出文件只允许新建，不覆盖输入或已有报告。报告版本为 `schema_version: 2`；诊断完成条件见下表。

| 字段 | 含义 |
|---|---|
| `usage_state` | `credits_observed`、`credits_missing`、`usage_absent`、`invalid_usage`、`upstream_error` |
| `credits_micro` | 精确整数，1 Credit = 1,000,000 单位；缺失为 null，不能按 0 结算 |
| `usage_snapshots` | 观测到的 usage 快照数；累计快照不相加 |
| `snapshot_regressions` | 当前 Credits 或输入 / 输出 / 总 / 缓存 / 推理 Token 小于此前最高观测值的事件数；跨缺失字段仍检查，不回填旧值 |
| `done_seen` | 是否观察到 `[DONE]` 标记；不单独证明业务成功 |
| `incomplete_event` | EOF 时是否残留不完整 SSE 事件；该事件被丢弃，不作为用量 |
| `input_format` / `content_encoding` | 显式指定的 `json` / `sse` 与 `identity` / `gzip` / `deflate` |
| `input_bytes` / `decoded_bytes` | 原始输入与解压后正文的字节数，分别受大小限制 |
| `stream_state` | `json_complete`、`sse_done`、`sse_incomplete_event` 或 `sse_end_unconfirmed`；只表示解析状态，不证明业务成功 |
| `review_reasons` | 用量未知 / 无效、上游错误、SSE 尾部不完整 / 缺少结束标记、用量快照回退等固定原因 |
| `diagnostic_ready` | 观测到有效 Credits、无上游错误和快照回退，且 JSON 可完整解析或 SSE 有结束标记且无不完整尾部 |
| `diagnostic_only` | 始终为 true，真实结算还需要服务器关联、幂等与完成状态 |

退出码：0 表示 `diagnostic_ready: true`；2 表示报告已生成但仍需核对；1 表示解析、编码、大小、官方数值或文件操作失败。0 不表示真实账号、计费或业务已经验证通过。

特别是 SSE 在完整事件边界中断时，可能已有 Credits 快照而 `incomplete_event: false`，但没有 `[DONE]`；此时为 `sse_end_unconfirmed`，退出码 2，不能将快照当成最终结算用量。其他原生结束信号在真实协议验证后单独适配，不能推定 HTTP EOF 就是成功结束。

## 离线核对同笔官方消耗

只接受手工提供的同笔请求消耗数值；不连接官方 API、不读取凭据、不修改余额。以下 0.125 为人工夹具的演示值：

```powershell
& ".local\p0-venv\Scripts\python.exe" -m server.qoder_cloud.probe --input server/tests/fixtures/synthetic-native.sse --official-credits 0.125 --summary
```

真实样本需将输入换为本机 `.local/protocol/` 的响应，并将 `--official-credits` 换为核实过的同笔实际消耗，不使用演示值。不是账号当前余额或多笔请求累计消耗；没有可靠的逐笔关联时仍保持待人工核对。

指定该选项才增加 `official_comparison`：

| 字段 | 含义 |
|---|---|
| `state` | `matched`（数字一致）、`mismatch`、`credits_missing` 或 `sample_unconfirmed` |
| `official_credits_micro` / `observed_credits_micro` | 官方手工值与响应观测值的整数微积分；未知观测值为 null |
| `difference_micro` | 观测值减官方值；未知则为 null，不推定为零 |
| `official_value_source` | 固定 `manual_unverified`，不把人工输入当成已验证回执 |
| `request_correlation_verified` | 始终 false；本工具没有验证请求、身份、时间窗口或账号归属 |
| `diagnostic_only` | 始终 true；不是平台账单 |

缺失 / 非有限 / 负数 / 超过六位精度的官方值被拒绝；数字不一致退出 2，并加 `official_credits_mismatch`。流未结束、快照回退或上游异常时，即使数字相同仍退出 2，状态为 `sample_unconfirmed`。不能通过手工数值消除已有待核对原因。

`--summary` 在 stderr 输出固定诊断字段，stdout 仍为单份 JSON。使用 `--output` 时 JSON 只写入新文件；输出文件不覆盖输入或已有报告。未指定核对 / 摘要选项时维持原有报告接口。

## 支持范围

支持 JSON 响应里的 `usage`，以及含 `statusCodeValue` 和 `body` 的原生 JSON / SSE 信封；`body` 可以是 JSON 对象或 JSON 字符串。SSE 支持 UTF-8 拆包、BOM、CR / LF / CRLF、多个 data 行和注释。

只解开明确识别的信封，不递归搜索对话正文中的同名字段。默认原始输入和解压正文分别限制在 64 MiB，解压输出分块不超过 64 KiB；单个 SSE 事件上限 2 MiB，计入实际 CR / LF / CRLF 与 UTF-8 字节，初始 BOM 除外。JSON 结构最多 64 层，原生信封最多解包 4 层；重复键（含转义后的同名键）、超限指数或结构深度均以安全错误拒绝。

非 2xx 原生状态不判为成功。显式 `usage: null` 清除旧快照，不把较早 Credits 当成最终用量；后续完整有效快照可恢复观测值，但历史回退 / 无效用量不会被清除。SSE 的裸 `data` 字段按空数据处理，不在结束标记后静默忽略；EOF 的注释或未知字段未以空行结束时仍标记不完整事件。

压缩流截断、校验和错误、尾随字节或解压超额均失败，不输出部分报告。

支持单成员 gzip 与带 zlib 包装的 HTTP deflate。不支持 raw deflate、多成员 gzip、多重 Content-Encoding、Brotli、zstd、归档压缩包、protobuf 或其他二进制协议；不能因此声称所有 Qoder IDE 流量都兼容。

只有 `server/tests/fixtures/` 下明确标注为 synthetic 的人工夹具可以提交。真实账号凭据、原始响应和对话不得进入仓库。下一步验收见 [协议验证操作](../docs/protocol-validation.md)。
