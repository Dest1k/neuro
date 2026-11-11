"""Small CLI around existing DSH work, not a replacement agent framework."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .cost import estimate_cost, load_pricing, normalize_usage
from .experiments import evaluate, run_command, write_result
from .workspace import ROOT, cpu_check, doctor, process_ok, read_json, source_check


def parser():
    cli = argparse.ArgumentParser(description="neuro: offline development and evidence tools")
    commands = cli.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("doctor", help="Read-only local toolchain inventory")
    inventory.add_argument("--target", choices=["offline", "gpu"], default="offline")
    inventory.add_argument("--output", type=Path)
    source = commands.add_parser("source-check", help="Verify pinned and unmodified dependencies")
    source.add_argument("--output", type=Path)
    cpu = commands.add_parser("cpu-check", help="Compile/run offline C++ component checks with GCC")
    cpu.add_argument("--cxx")
    cpu.add_argument("--output", type=Path)
    cost = commands.add_parser("cost", help="Price a final API usage JSON; never calls an API")
    cost.add_argument("--usage", required=True, type=Path)
    cost.add_argument("--model", required=True)
    cost.add_argument("--format", default="auto", choices=[
        "auto", "openai", "openai_chat", "openai_responses", "anthropic"])
    cost.add_argument("--prices", type=Path, default=ROOT / "configs/model-prices.json")
    cost.add_argument("--output", type=Path)
    comparison = commands.add_parser("compare", help="Validate evidence and compare measured candidates")
    comparison.add_argument("--candidate", required=True, type=Path)
    comparison.add_argument("--baseline", required=True, type=Path)
    comparison.add_argument("--output", type=Path)
    run = commands.add_parser("run", help="Run one local subprocess with a timeout and bounded logs")
    run.add_argument("--timeout", type=float, default=60)
    run.add_argument("--stage", choices=["build", "verification", "measurement"], default="measurement")
    run.add_argument("--cwd", type=Path, default=ROOT)
    run.add_argument("--output", type=Path)
    run.add_argument("argv", nargs=argparse.REMAINDER, help="-- executable argument ... (no shell)")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args.target)
            success = result["passed"]
        elif args.command == "source-check":
            result = source_check()
            success = result["passed"]
        elif args.command == "cpu-check":
            result = cpu_check(cxx=args.cxx)
            success = result["passed"]
        elif args.command == "cost":
            usage = normalize_usage(read_json(args.usage), args.format)
            result = estimate_cost(usage, load_pricing(args.prices, args.model))
            # Incomplete usage must not silently satisfy an automated budget check.
            success = result["accounting_complete"]
        elif args.command == "compare":
            result = evaluate(read_json(args.candidate), read_json(args.baseline))
            success = result["passed"]
        else:
            command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            result = run_command(command, cwd=args.cwd, timeout_s=args.timeout, stage=args.stage)
            success = process_ok(result)
        if args.output:
            result = write_result(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if success else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
