"""Offline sampling CLI. Never sends requests or reads browser/account stores."""

import argparse
import json
import os
from pathlib import Path
import sys

from .protocol import ProtocolError, sample


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Extract allowlisted Qoder usage metadata; not a billing receipt.")
    parser.add_argument("--input", required=True, help="Local raw response file, or - for stdin")
    parser.add_argument("--format", choices=("sse", "json"), default="sse", dest="input_format")
    parser.add_argument("--output", help="New report file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.output and args.input != "-" and Path(args.output).resolve() == Path(args.input).resolve():
            raise ProtocolError("input and output must be different files")
        if args.input == "-":
            report = sample(sys.stdin.buffer, args.input_format)
        else:
            with open(args.input, "rb") as source:
                report = sample(source, args.input_format)
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content)
        else:
            print(content, end="")
        return 0 if report["usage_state"] == "credits_observed" and not report["incomplete_event"] else 2
    except (OSError, ProtocolError):
        # Do not expose raw exceptions: their message may contain private paths,
        # attacker-controlled input fragments, or credential-bearing payloads.
        print("Protocol sampling failed: check format, size, encoding and file permissions; raw input was not printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
