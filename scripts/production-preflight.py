#!/usr/bin/env python3
"""Validate agentHub production .env without displaying secret values."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from common.preflight import check_production_env, exit_code, render  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", nargs="?", type=Path,
                        default=ROOT / ".env")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures")
    args = parser.parse_args()
    findings = check_production_env(args.env_file)
    print(render(findings))
    return exit_code(findings, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
