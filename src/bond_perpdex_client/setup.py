"""Create an owner-only environment file and optional persistent signing key."""

import argparse
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .configuration import Settings, reference_table, template
from .credentials import load_ed25519_private_key
from .models import SafetyError


def ensure_environment(path: Path, subaccount: str | None = None) -> bool:
    if subaccount is not None and not re.fullmatch(r"[\x21-\x7e]{1,12}", subaccount):
        raise SafetyError("Subaccount must be 1-12 printable ASCII bytes without spaces")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w") as stream:
        stream.write(template({"BOND_SUBACCOUNT": subaccount} if subaccount is not None else None))
    return True


def ensure_signing_key(path: Path) -> bool:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        load_ed25519_private_key(path)
        return False
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(
            Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--subaccount", help="Name to write when creating a new environment file")
    parser.add_argument("--generate-signing-key", action="store_true")
    parser.add_argument(
        "--print-template", action="store_true", help="Print the canonical .env.example"
    )
    parser.add_argument(
        "--print-reference", action="store_true", help="Print the variable reference table"
    )
    args = parser.parse_args()
    if args.print_reference:
        print(reference_table(), end="")
        return
    if args.print_template:
        print(template(), end="")
        return
    created = ensure_environment(args.env_file, args.subaccount)
    print(f"{'Created' if created else 'Preserved existing'} environment: {args.env_file}")
    settings = Settings.load(args.env_file)
    settings.validate()
    if args.generate_signing_key:
        key_path = settings.path("BOND_ED25519_PRIVATE_KEY_FILE")
        created = ensure_signing_key(key_path)
        print(f"{'Created' if created else 'Preserved existing'} Ed25519 key: {key_path}")
    print("Ready: run make quickstart. Edit your environment file to tune the example.")


if __name__ == "__main__":
    main()
