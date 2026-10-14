from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import secrets


def encode(password: str, iterations: int = 260_000) -> str:
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(iterations),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a ModelDeck administrator password hash")
    parser.add_argument("password", nargs="?", help="omit to enter without terminal echo")
    args = parser.parse_args()
    value = args.password if args.password is not None else getpass.getpass("Administrator password: ")
    if len(value) < 12:
        raise SystemExit("password must contain at least 12 characters")
    print(encode(value))
