"""Where secrets come from (D-30): the environment first, then your OS keychain.

In GitHub Actions the environment holds repository secrets, passed only to
the steps that need them. On your computer, keep secrets in the OS keychain
(macOS Keychain, Windows Credential Manager, or Secret Service on Linux)
instead of shell profiles or .env files:

  pip install keyring                      # once (the "local" extra)
  python -m sealed.keys new-data-key       # create the Parquet key and save it
  python -m sealed.keys set R2_ACCESS_KEY_ID      # hidden prompt, saved
  python -m sealed.keys status             # where each secret is found; never prints values
  python -m sealed.keys export SEALED_DATA_KEY | gh secret set SEALED_DATA_KEY -R you/sealed-tracker

Both repos read the same keychain entries (service "sealed-tracker"), so you
set them once per computer. Copied verbatim into the Terapeak repo (python -m
terapeak.keys); keep the copies identical.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import os
import secrets
import sys

SERVICE = "sealed-tracker"
DATA_KEY = "SEALED_DATA_KEY"
NAMES = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", DATA_KEY)


def _keychain_get(name: str) -> str | None:
    try:
        import keyring
        from keyring.errors import NoKeyringError
    except ImportError:  # the "local" extra isn't installed (e.g. a CI runner)
        return None
    try:
        return keyring.get_password(SERVICE, name)
    except NoKeyringError:  # no usable backend
        return None
    except Exception as e:  # e.g. a locked keychain: say so rather than silently act as if unset
        print(f"warning: could not read {name} from the OS keychain ({type(e).__name__})", file=sys.stderr)
        return None


def _keychain_set(name: str, value: str) -> None:
    import keyring  # raises ImportError with a clear message if the extra isn't installed
    keyring.set_password(SERVICE, name, value)


def get(name: str) -> str | None:
    return os.environ.get(name) or _keychain_get(name)


def source(name: str) -> str:
    if os.environ.get(name):
        return "environment"
    return "keychain" if _keychain_get(name) else "missing"


def new_data_key() -> str:
    """256 random bits from the OS CSPRNG, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def validate_data_key(value: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise ValueError(f"{DATA_KEY} must be 32 bytes (256 bits), base64-encoded")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show where each secret is found (never its value)")
    new = sub.add_parser("new-data-key", help="create the Parquet encryption key in the keychain")
    new.add_argument("--replace", action="store_true",
                     help="replace an existing key (files written with the old key become unreadable without it)")
    st = sub.add_parser("set", help="save a secret to the keychain (hidden prompt)")
    st.add_argument("name", choices=NAMES)
    ex = sub.add_parser("export", help="print a secret to stdout, for piping into `gh secret set`")
    ex.add_argument("name", choices=NAMES)
    args = ap.parse_args(argv)

    if args.cmd == "status":
        for name in NAMES:
            print(f"{name}: {source(name)}")
        return 0
    if args.cmd == "new-data-key":
        if _keychain_get(DATA_KEY) and not args.replace:
            print(f"{DATA_KEY} already exists in the keychain; not replacing it. Data written with it would "
                  "become unreadable. Use --replace only if you have a backup of the old key.", file=sys.stderr)
            return 1
        _keychain_set(DATA_KEY, new_data_key())
        print(f"Saved a new {DATA_KEY} to the keychain. Next: back it up in your password manager "
              f"(python -m {__package__}.keys export {DATA_KEY}) and add it to GitHub (see README).")
        return 0
    if args.cmd == "set":
        value = getpass.getpass(f"{args.name}: ").strip()
        if args.name == DATA_KEY:
            validate_data_key(value)
        _keychain_set(args.name, value)
        print(f"Saved {args.name} to the keychain.")
        return 0
    value = get(args.name)
    if not value:
        print(f"{args.name} is not set in the environment or the keychain.", file=sys.stderr)
        return 1
    if sys.stdout.isatty():
        print("Warning: printing a secret to the terminal. Pipe it instead, e.g. into `gh secret set`.",
              file=sys.stderr)
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
