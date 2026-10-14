from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

from hash_password import encode


def read_value(path: Path, name: str) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("'\"")
    raise ValueError(f"{name} is not present in {path}")


def provision(source: Path, output: Path, force: bool = False) -> None:
    if output.exists() and not force:
        print(f"kept existing {output}")
        return
    gateway_key = read_value(source, "HOUMO_API_KEY")
    if len(gateway_key) < 12:
        raise ValueError("HOUMO_API_KEY is too short to use as the initial administrator password")
    content = "\n".join(
        [
            f"ADMIN_PASSWORD_HASH='{encode(gateway_key)}'",
            f"ADMIN_AGENT_TOKEN={secrets.token_urlsafe(48)}",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.next.{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)
    print(f"created {output} with mode 0600")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Provision ModelDeck runtime secrets")
    parser.add_argument("--source", type=Path, required=True, help="existing Houmo .env file")
    parser.add_argument("--output", type=Path, required=True, help="ModelDeck .env path")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args()
    provision(args.source, args.output, args.force)
