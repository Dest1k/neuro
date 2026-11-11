"""Small CLI around existing DSH work, not a replacement agent framework."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .budget import BudgetLedger
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
    cpu = commands.add_parser("cpu-check", help="Compile/run offline C/C++ component checks with GCC")
    cpu.add_argument("--cxx")
    cpu.add_argument("--output", type=Path)
    cost = commands.add_parser("cost", help="Price a final API usage JSON; never calls an API")
    cost.add_argument("--usage", required=True, type=Path)
    cost.add_argument("--model", required=True)
    cost.add_argument("--format", default="auto", choices=[
        "auto", "openai", "openai_chat", "openai_responses", "anthropic"])
    cost.add_argument("--prices", type=Path, default=ROOT / "configs/model-prices.json")
    cost.add_argument("--output", type=Path)
    budget = commands.add_parser("budget", help="Persistent request reservations; connect these hooks to DSH before dispatch")
    budget.add_argument("--db", type=Path, default=ROOT / "artifacts/model-budget.sqlite3")
    budget.add_argument("--project", type=Path, default=ROOT / "configs/project.json")
    budget.add_argument("--prices", type=Path, default=ROOT / "configs/model-prices.json")
    budget.add_argument("--output", type=Path)
    actions = budget.add_subparsers(dest="budget_action", required=True)
    reserve = actions.add_parser("reserve", help="Hold full-price maximum input/output cost before a request")
    reserve.add_argument("--campaign", required=True)
    reserve.add_argument("--session", required=True)
    reserve.add_argument("--request", required=True)
    reserve.add_argument("--model")
    reserve.add_argument("--input-tokens", type=int)
    reserve.add_argument("--output-tokens", type=int)
    begin = actions.add_parser("begin", help="Obtain the single-use dispatch permission for a reservation")
    begin.add_argument("--request", required=True)
    reconcile = actions.add_parser("reconcile", help="Record a final usage receipt or preserve an unresolved charge")
    reconcile.add_argument("--request", required=True)
    receipt = reconcile.add_mutually_exclusive_group(required=True)
    receipt.add_argument("--usage", type=Path)
    receipt.add_argument("--missing-usage", action="store_true")
    reconcile.add_argument("--format", default="auto", choices=[
        "auto", "openai", "openai_chat", "openai_responses", "anthropic"])
    cancel = actions.add_parser("cancel", help="Release only a request that was never dispatched")
    cancel.add_argument("--request", required=True)
    status = actions.add_parser("status", help="Inspect actual settled costs and outstanding reservations")
    status.add_argument("--campaign", required=True)
    status.add_argument("--session")
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
        elif args.command == "budget":
            ledger = BudgetLedger.from_project(args.db, args.project, args.prices)
            success = True
            if args.budget_action == "reserve":
                result = ledger.reserve(campaign_id=args.campaign, session_id=args.session,
                    reservation_id=args.request, model_id=args.model,
                    max_input_tokens=args.input_tokens, max_output_tokens=args.output_tokens)
            elif args.budget_action == "begin":
                result = ledger.begin_dispatch(args.request)
                success = result["dispatch_permitted"] is True
            elif args.budget_action == "reconcile":
                receipt_read_error = False
                try:
                    payload = None if args.missing_usage else read_json(args.usage)
                except (OSError, ValueError, UnicodeError):
                    # The call was already dispatched. A broken/missing receipt
                    # must become explicit uncertainty, never free budget.
                    payload = None
                    receipt_read_error = True
                result = ledger.reconcile(args.request, payload, api_format=args.format)
                if receipt_read_error:
                    result["receipt_read_error"] = True
                success = result["reconciled"] is True and not result["overrun"]
            elif args.budget_action == "cancel":
                result = ledger.cancel_undispatched(args.request)
            else:
                result = ledger.status(campaign_id=args.campaign, session_id=args.session)
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
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
