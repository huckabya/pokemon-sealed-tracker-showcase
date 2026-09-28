"""Storage: the same layout on a local folder or a Cloudflare R2 bucket (D-20).

A data root is a local path ("data") or a bucket URI ("r2://sealed-data").
R2 credentials (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY) and the
Parquet key (SEALED_DATA_KEY) come from the environment or your OS keychain
(keys.py, D-30). For tests, "s3://bucket" plus S3_ENDPOINT_URL points at any
S3-compatible server (moto).

Encryption (D-31): with SEALED_DATA_KEY set, every Parquet file is written
AES-256-GCM encrypted (crypto.py) and decrypted on read. A private bucket
store refuses to write plaintext Parquet, so a missing secret fails the run
instead of leaking data, and refuses to read plaintext Parquet, so a file
swapped in by someone with write access can't pass as real data. Stores
opened with public=True never encrypt (the public bucket, D-25). Local
folders may mix old plaintext and encrypted files.

Layout (paths relative to the root):

  dim_group.parquet                          one row per TCGplayer group (set)
  dim_product.parquet                        one row per sealed product
  fact_price_daily/YYYY/YYYY-MM-DD.parquet   immutable, one file per snapshot
  fact_price_seed/<source>.parquet           third-party backfill (private only)
  fact_sales_daily/YYYY/YYYY-MM-DD.parquet   written by the Terapeak side repo
  fact_listings_daily/YYYY/YYYY-MM-DD.parquet written by the Terapeak side repo
  _reports/excluded_products.csv             classifier audit trail

Daily files are write-once. Dims are small and rewritten each run.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from sealed import crypto, keys

PARQUET_OPTS = {"compression": "zstd", "compression_level": 9}


class Store:
    """A data root on some pyarrow filesystem. `base` has no trailing slash.
    `key` encrypts Parquet (None = plaintext); `require_key` (private buckets)
    refuses plaintext Parquet in both directions."""

    def __init__(self, fs: pafs.FileSystem, base: str, uri: str, key: str | None = None,
                 require_key: bool = False) -> None:
        self.fs, self.base, self.uri = fs, base.rstrip("/"), uri
        self.key, self.require_key = key, require_key
        self.local = isinstance(fs, pafs.LocalFileSystem)

    def path(self, rel: str) -> str:
        return f"{self.base}/{rel}" if rel else self.base

    def exists(self, rel: str) -> bool:
        return self.fs.get_file_info(self.path(rel)).type == pafs.FileType.File

    def _ensure_parent(self, rel: str) -> None:
        """Local directories only. A bucket has no directories, and a marker object
        such as 'fact_price_daily/' is refused under the bucket lock (D-30)."""
        if self.local:
            self.fs.create_dir(self.path(rel).rsplit("/", 1)[0], recursive=True)

    def list_files(self, rel_dir: str, suffix: str = ".parquet") -> list[str]:
        """Relative paths of files under rel_dir (recursive), sorted."""
        sel = pafs.FileSelector(self.path(rel_dir), recursive=True, allow_not_found=True)
        out = []
        for info in self.fs.get_file_info(sel):
            if info.type == pafs.FileType.File and info.path.endswith(suffix):
                out.append(info.path[len(self.base) + 1:])
        return sorted(out)

    def write_parquet(self, rel: str, table: pa.Table) -> None:
        if self.key:
            self.write_bytes(rel, crypto.encrypt_table(table, self.key))
            return
        if self.require_key:
            raise crypto.MissingKey(f"refusing to write plaintext Parquet to {self.uri}: "
                                    f"{keys.DATA_KEY} is not set (D-31)")
        self._ensure_parent(rel)
        if self.local:  # atomic rename locally
            tmp = self.path(rel) + ".tmp"
            pq.write_table(table, tmp, filesystem=self.fs, **PARQUET_OPTS)
            self.fs.move(tmp, self.path(rel))
        else:  # a single object PUT is atomic on S3/R2
            pq.write_table(table, self.path(rel), filesystem=self.fs, **PARQUET_OPTS)

    def read_bytes(self, rel: str) -> bytes:
        with self.fs.open_input_file(self.path(rel)) as fh:
            return fh.readall()

    def read_tail(self, rel: str, n: int) -> bytes:
        with self.fs.open_input_file(self.path(rel)) as fh:
            fh.seek(max(0, fh.size() - n))
            return fh.read(n)

    def read_parquet(self, rel: str, schema: pa.Schema | None = None) -> pa.Table:
        data = self.read_bytes(rel)  # one GET per file on R2
        if not crypto.is_encrypted(data):
            if self.require_key:
                raise crypto.UnexpectedPlaintext(f"{self.path(rel)} is not encrypted; every Parquet file in "
                                                 f"{self.uri} should be (D-31). Check who wrote it.")
            return pq.read_table(pa.BufferReader(data), schema=schema)
        if not self.key:
            raise crypto.MissingKey(f"{self.path(rel)} is encrypted and {keys.DATA_KEY} is not set "
                                    "(check with: python -m sealed.keys status)")
        if self.local:
            return crypto.decrypt_file(self.path(rel), self.key, schema, self.path(rel))
        return crypto.decrypt_bytes(data, self.key, schema, self.path(rel))

    def read_many(self, rels: list[str], schema: pa.Schema) -> pa.Table:
        if not rels:
            return schema.empty_table()
        return pa.concat_tables([self.read_parquet(r, schema) for r in rels])

    def num_rows(self, rel: str) -> int:
        if crypto.is_encrypted(self.read_tail(rel, 4)) or self.require_key:
            return self.read_parquet(rel).num_rows
        with self.fs.open_input_file(self.path(rel)) as fh:
            return pq.ParquetFile(fh).metadata.num_rows

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._ensure_parent(rel)
        target = self.path(rel) + (".tmp" if self.local else "")  # atomic rename locally
        with self.fs.open_output_stream(target) as fh:
            fh.write(data)
        if self.local:
            self.fs.move(target, self.path(rel))

    def upload_dir(self, local_dir: Path, rel_prefix: str) -> int:
        """Copy a local directory tree under rel_prefix (used for gold/).
        Parquet files go through write_parquet, so they are encrypted here too."""
        n = 0
        for f in sorted(p for p in Path(local_dir).rglob("*") if p.is_file()):
            rel = f"{rel_prefix}/{f.relative_to(local_dir).as_posix()}"
            if f.suffix == ".parquet" and (self.key or self.require_key):
                self.write_parquet(rel, pq.ParquetFile(f).read())
            else:
                self.write_bytes(rel, f.read_bytes())
            n += 1
        return n


def open_store(root: str | Path | Store, public: bool = False) -> Store:
    """public=True: the public bucket or a public build. Never encrypts (D-25, D-31)."""
    if isinstance(root, Store):
        return root
    uri = str(root)
    key = None if public else keys.get(keys.DATA_KEY)
    if key:
        keys.validate_data_key(key)
    for scheme in ("r2://", "s3://"):
        if uri.startswith(scheme):
            bucket_path = uri[len(scheme):].strip("/")
            endpoint = os.environ.get("S3_ENDPOINT_URL")
            if scheme == "r2://" and not endpoint:
                account = keys.get("R2_ACCOUNT_ID")
                if not account:
                    raise RuntimeError("R2_ACCOUNT_ID is not set (python -m sealed.keys status)")
                endpoint = f"https://{account}.r2.cloudflarestorage.com"
            fs = pafs.S3FileSystem(
                access_key=keys.get("R2_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID"),
                secret_key=keys.get("R2_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
                endpoint_override=endpoint,
                region="auto" if scheme == "r2://" else os.environ.get("AWS_REGION", "us-east-1"),
            )
            return Store(fs, bucket_path, uri, key=key, require_key=not public)
    local = Path(uri).resolve()
    local.mkdir(parents=True, exist_ok=True)
    return Store(pafs.LocalFileSystem(), local.as_posix(), uri, key=key)


# ---- daily fact files -------------------------------------------------------

def daily_rel(table: str, day: dt.date) -> str:
    return f"{table}/{day:%Y}/{day.isoformat()}.parquet"


def daily_path(root, table: str, day: dt.date) -> Path:
    """Local path of a daily file (local stores only; used by tests)."""
    return Path(open_store(root).path(daily_rel(table, day)))


def has_daily(root, table: str, day: dt.date) -> bool:
    return open_store(root).exists(daily_rel(table, day))


def latest_daily(root, table: str) -> dt.date | None:
    files = open_store(root).list_files(table)
    return dt.date.fromisoformat(files[-1].rsplit("/", 1)[1][:-8]) if files else None


def write_daily(root, table_name: str, day: dt.date, table: pa.Table, overwrite: bool = False) -> str:
    st = open_store(root)
    rel = daily_rel(table_name, day)
    if st.exists(rel) and not overwrite:
        raise FileExistsError(f"{st.path(rel)} already exists; daily files are write-once")
    st.write_parquet(rel, table)
    return st.path(rel)


# ---- dimensions and reports ----------------------------------------------------

def read_table(root, rel: str, schema: pa.Schema) -> pa.Table:
    st = open_store(root)
    return st.read_parquet(rel, schema) if st.exists(rel) else schema.empty_table()


def merge_dim(existing: pa.Table, incoming: pa.Table, key: str) -> pa.Table:
    """Upsert by key: newest attributes win, first_seen keeps the earliest date,
    last_seen keeps the latest. Rows absent from `incoming` are kept unchanged
    (a product that disappears from TCGplayer keeps its history)."""
    rows: dict[Any, dict[str, Any]] = {r[key]: r for r in existing.to_pylist()}
    for new in incoming.to_pylist():
        old = rows.get(new[key])
        if old is not None:
            new["first_seen"] = min(old["first_seen"], new["first_seen"])
            new["last_seen"] = max(old["last_seen"], new["last_seen"])
        rows[new[key]] = new
    return pa.Table.from_pylist([rows[k] for k in sorted(rows)], schema=incoming.schema)


def write_dim(root, name: str, table: pa.Table) -> str:
    st = open_store(root)
    st.write_parquet(f"{name}.parquet", table)
    return st.path(f"{name}.parquet")


def write_excluded_report(root, rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["product_id", "group_id", "name", "reason"])
    writer.writeheader()
    writer.writerows(sorted(rows, key=lambda r: (r["reason"], r["product_id"])))
    st = open_store(root)
    st.write_bytes("_reports/excluded_products.csv", buf.getvalue().encode("utf-8"))
    return st.path("_reports/excluded_products.csv")
