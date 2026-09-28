"""Parquet encryption at rest (D-31), using DuckDB's native AES-256-GCM.

Files follow the Parquet Modular Encryption spec: footer and every column are
encrypted and authenticated with one 256-bit key (SEALED_DATA_KEY, see keys.py).
A wrong key or a tampered file fails the GCM tag check instead of returning
garbage. Encrypted files end in b"PARE" instead of b"PAR1", so old plaintext
files stay readable and `audit` can tell them apart without the key.

Writing needs DuckDB's httpfs extension, which supplies OpenSSL's secure
random numbers; DuckDB >= 1.4.2 refuses to encrypt without it
(CVE-2025-64429), and so does this module. Reading needs only DuckDB.
R2 never sees the key: files are encrypted before upload.

  python -m sealed.crypto audit r2://sealed-data   # counts encrypted vs plaintext; exit 1 if any plaintext

Copied verbatim into the Terapeak repo (python -m terapeak.crypto); keep the copies identical.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import duckdb
import pyarrow as pa

KEY_NAME = "sealed"
ENCRYPTED_MAGIC = b"PARE"
PARQUET_OPTS_SQL = "FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 9"

_CONNECTIONS: dict[tuple[str, str], duckdb.DuckDBPyConnection] = {}


class MissingKey(RuntimeError):
    pass


class DecryptionFailed(RuntimeError):
    pass


class UnexpectedPlaintext(RuntimeError):
    """A plaintext file where only encrypted files belong: someone with write
    access could otherwise swap in unauthenticated data."""


def is_encrypted(data_or_tail: bytes) -> bool:
    return data_or_tail[-4:] == ENCRYPTED_MAGIC


def _connection(key: str, write: bool) -> duckdb.DuckDBPyConnection:
    from .keys import validate_data_key  # the key is interpolated into SQL: base64 only
    validate_data_key(key)
    cache_key = ("w" if write else "r", key)
    if cache_key not in _CONNECTIONS:
        con = duckdb.connect(":memory:")
        if write:
            try:
                con.execute("LOAD httpfs")
            except duckdb.Error:
                try:
                    con.execute("INSTALL httpfs")
                    con.execute("LOAD httpfs")
                except duckdb.Error as e:
                    raise RuntimeError(
                        "DuckDB's httpfs extension (OpenSSL) is needed to write encrypted Parquet securely. "
                        "Run once with network access: python -c \"import duckdb; duckdb.sql('INSTALL httpfs')\""
                    ) from e
        con.execute(f"PRAGMA add_parquet_key('{KEY_NAME}', '{key}')")
        _CONNECTIONS[cache_key] = con
    return _CONNECTIONS[cache_key]


def _forget(key: str, write: bool) -> None:
    """Drop a cached connection after an error: some decryption failures are
    FATAL in DuckDB and invalidate the whole database."""
    con = _CONNECTIONS.pop(("w" if write else "r", key), None)
    if con is not None:
        try:
            con.close()
        except duckdb.Error:
            pass


def conform(table: pa.Table, schema: pa.Schema | None) -> pa.Table:
    """DuckDB returns its own Arrow types (e.g. the session time zone); cast back.
    Columns added to the schema after a file was written come back as nulls,
    as they do for plaintext files read with pyarrow."""
    if schema is None:
        return table
    for field in schema:
        if field.name not in table.column_names:
            table = table.append_column(field.name, pa.nulls(table.num_rows, field.type))
    return table.select(schema.names).cast(schema)


def encrypt_table(table: pa.Table, key: str) -> bytes:
    con = _connection(key, write=True)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "t.parquet"
        con.register("_plain", table)
        try:
            con.execute(f"COPY _plain TO '{out.as_posix()}' "
                        f"({PARQUET_OPTS_SQL}, ENCRYPTION_CONFIG {{footer_key: '{KEY_NAME}'}})")
            con.unregister("_plain")
        except duckdb.Error:
            _forget(key, write=True)
            raise
        return out.read_bytes()


def decrypt_file(path: str | Path, key: str, schema: pa.Schema | None = None, label: str | None = None) -> pa.Table:
    """Any failure to read an encrypted file (wrong key, tampering, truncation)
    raises DecryptionFailed naming `label` (the object's real path)."""
    con = _connection(key, write=False)
    try:
        table = con.execute("SELECT * FROM read_parquet(?, encryption_config = {footer_key: '" + KEY_NAME + "'})",
                            [str(path)]).to_arrow_table()
    except duckdb.Error as e:
        _forget(key, write=False)
        raise DecryptionFailed(f"{label or path}: wrong key, or a corrupted or tampered file ({e})") from e
    return conform(table, schema)


def decrypt_bytes(data: bytes, key: str, schema: pa.Schema | None = None, label: str | None = None) -> pa.Table:
    """Decrypt a downloaded file. Only ciphertext touches disk; the plaintext stays in memory."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.parquet"
        path.write_bytes(data)
        return decrypt_file(path, key, schema, label or "downloaded file")


def audit(store) -> dict[str, list[str]]:
    """Every .parquet file in a store, split by whether it is encrypted. Needs no key."""
    out: dict[str, list[str]] = {"encrypted": [], "plaintext": []}
    for rel in store.list_files(""):
        out["encrypted" if is_encrypted(store.read_tail(rel, 4)) else "plaintext"].append(rel)
    return out


def main(argv: list[str] | None = None) -> int:
    from .store import open_store

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("audit", help="count encrypted and plaintext Parquet files")
    au.add_argument("data", nargs="?", default=os.environ.get("DATA_URI", "data"))
    args = ap.parse_args(argv)
    result = audit(open_store(args.data))
    print(f"encrypted: {len(result['encrypted'])}  plaintext: {len(result['plaintext'])}")
    for rel in result["plaintext"][:20]:
        print(f"  plaintext: {rel}")
    return 1 if result["plaintext"] else 0


if __name__ == "__main__":
    sys.exit(main())
