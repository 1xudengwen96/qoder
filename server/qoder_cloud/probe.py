"""Offline sampling CLI. Never sends requests or reads browser/account stores."""

import argparse
import json
import os
from pathlib import Path
import sys

from .protocol import ProtocolError, sample
from .usage import InvalidUsage, compare_credits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Extract allowlisted Qoder usage metadata; not a billing receipt.")
    parser.add_argument("--input", required=True, help="Local raw response file, or - for stdin")
    parser.add_argument("--format", choices=("sse", "json"), default="sse", dest="input_format")
    parser.add_argument("--output", help="New report file; existing files are never overwritten")
    parser.add_argument("--content-encoding", choices=("identity", "gzip", "deflate"), default="identity",
                        help="原始 HTTP 响应正文的 Content-Encoding；已解压的文件使用 identity")
    parser.add_argument("--official-credits", help="手工提供同笔请求的官方 Credits 消耗；仅离线数字核对，不访问账号")
    parser.add_argument("--summary", action="store_true", help="在 stderr 输出固定诊断摘要，stdout 保持 JSON")
    args = parser.parse_args(argv)
    try:
        if args.output and args.input != "-" and Path(args.output).resolve() == Path(args.input).resolve():
            raise ProtocolError("input and output must be different files")
        if args.input == "-":
            report = sample(sys.stdin.buffer, args.input_format, content_encoding=args.content_encoding)
        else:
            with open(args.input, "rb") as source:
                report = sample(source, args.input_format, content_encoding=args.content_encoding)
        if args.official_credits is not None:
            usage = report["usage"]
            comparison = compare_credits(usage["credits_micro"] if usage is not None else None,
                                         args.official_credits, sample_ready=report["diagnostic_ready"])
            report["official_comparison"] = comparison
            if comparison["state"] != "matched":
                reason = "official_credits_mismatch" if comparison["state"] == "mismatch" else "official_comparison_unconfirmed"
                report["review_reasons"].append(reason)
                report["diagnostic_ready"] = False
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
        else:
            print(content, end="")
        if args.summary:
            print(f"用量={report['usage_state']} 流状态={report['stream_state']} "
                  f"事件={report['events_seen']} 快照={report['usage_snapshots']} "
                  f"需核对={','.join(report['review_reasons']) or 'none'}；仅诊断，非账单", file=sys.stderr)
        return 0 if report["diagnostic_ready"] else 2
    except (OSError, ProtocolError, InvalidUsage):
        # Do not expose raw exceptions: their message may contain private paths,
        # attacker-controlled input fragments, or credential-bearing payloads.
        print("协议采样未完成：检查格式、大小、编码、官方 Credits 数值与文件权限；未输出原始输入。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
