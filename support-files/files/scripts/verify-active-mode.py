#!/usr/bin/env python3
"""Run the installed verifier after an explicit install or upgrade."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys


VERIFY = pathlib.Path("/home/cix/gateway-admin/startup_verify.py")


def main() -> int:
    source = os.environ.get("MODELDECK_VERIFY_SOURCE", "install")
    if source not in {"install", "upgrade"}:
        source = "install"
    return subprocess.run(
        [sys.executable, str(VERIFY), "--force", "--source", source], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
