"""End-to-end against an S3-compatible server (moto) standing in for Cloudflare R2."""
import datetime as dt
import os
import urllib.request

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto.server import ThreadedMotoServer

from sealed import collect, crypto, gold, quality, store
from sealed.metrics import connect
from tests.test_collect import FakeClient
from tests.test_crypto import DAY, KEY, prices


@pytest.fixture
def bucket(monkeypatch):
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    for k, v in {"S3_ENDPOINT_URL": endpoint, "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                 aws_access_key_id="test", aws_secret_access_key="test").create_bucket(Bucket="sealed-data")
    yield "s3://sealed-data"
    # moto keeps buckets in process memory across servers; start each test empty
    urllib.request.urlopen(urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST"))
    server.stop()


def test_collect_query_and_upload_on_bucket(bucket, monkeypatch, now, tmp_path):
    monkeypatch.setattr(quality, "MIN_ROWS", 1)
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    assert collect.run(bucket, FakeClient(), tmp_path / "none.csv", now, lambda m: None) == 0
    st = store.open_store(bucket)
    assert st.exists("fact_price_daily/2026/2026-09-26.parquet")
    assert store.has_daily(bucket, "fact_price_daily", dt.date(2026, 9, 26))
    # rerun is a no-op on the bucket too
    client = FakeClient()
    assert collect.run(bucket, client, tmp_path / "none.csv", now, lambda m: None) == 0 and client.calls == 0

    con = connect(bucket)
    assert con.sql("SELECT count(*) FROM v_price_series").fetchone()[0] == 3

    manifest = gold.build(bucket, tmp_path / "gold", upload_prefix="gold")
    assert manifest["uploaded_files"] >= 5
    assert st.exists("gold/manifest.json") and st.exists("gold/product_latest.parquet")
    # every Parquet object in the bucket is encrypted (D-31); JSON/CSV are not Parquet
    audit = crypto.audit(st)
    assert audit["plaintext"] == [] and "fact_price_daily/2026/2026-09-26.parquet" in audit["encrypted"]


def test_bucket_refuses_plaintext_without_a_key(bucket, monkeypatch, now, tmp_path):
    monkeypatch.setattr(quality, "MIN_ROWS", 1)
    with pytest.raises(crypto.MissingKey, match="refusing to write plaintext"):
        collect.run(bucket, FakeClient(), tmp_path / "none.csv", now, lambda m: None)
    assert not store.open_store(bucket).exists("fact_price_daily/2026/2026-09-26.parquet")


def test_bucket_refuses_a_swapped_in_plaintext_file(bucket, monkeypatch, tmp_path):
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    st = store.open_store(bucket)
    st.write_parquet("dim_product.parquet", prices())
    buf = pa.BufferOutputStream()
    pq.write_table(prices(), buf)  # forged: plaintext with made-up prices
    st.write_bytes("dim_product.parquet", buf.getvalue().to_pybytes())
    with pytest.raises(crypto.UnexpectedPlaintext, match="not encrypted"):
        st.read_parquet("dim_product.parquet")


def test_public_bucket_takes_plaintext_parquet(bucket, monkeypatch):
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    pub = store.open_store(bucket, public=True)
    pub.write_parquet("gold/product_latest.parquet", prices())
    assert crypto.audit(pub)["plaintext"] == ["gold/product_latest.parquet"]
    assert pub.read_parquet("gold/product_latest.parquet").num_rows == 2


def test_bucket_writes_create_no_directory_objects(bucket, monkeypatch):
    # A bucket has no directories. Writing a 'fact_price_daily/' marker object
    # is refused under the bucket lock (first live run, 2026-09-28), so only
    # the file itself may be written.
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    store.write_daily(bucket, "fact_price_daily", DAY, prices())
    store.open_store(bucket, public=True).write_parquet("gold/product_latest.parquet", prices())
    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    objects = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="sealed-data")["Contents"])
    assert objects == ["fact_price_daily/2026/2026-09-26.parquet", "gold/product_latest.parquet"]
