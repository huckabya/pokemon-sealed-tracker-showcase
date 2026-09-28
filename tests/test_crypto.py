"""Parquet encryption at rest (D-31) and where secrets come from (D-30)."""
import base64
import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sealed import crypto, keys, schemas, store

KEY = base64.b64encode(bytes(range(32))).decode()
OTHER_KEY = base64.b64encode(bytes(range(1, 33))).decode()
DAY = dt.date(2026, 9, 26)


def prices() -> pa.Table:
    at = dt.datetime(2026, 9, 26, 21, 47, tzinfo=dt.timezone.utc)
    return pa.Table.from_pylist([
        {"snapshot_date": DAY, "product_id": 668496, "sub_type_name": "Normal", "market_price": 150.5,
         "low_price": 139.99, "mid_price": 155.0, "high_price": 300.0, "direct_low_price": None,
         "source": "tcgcsv", "ingested_at": at},
        {"snapshot_date": DAY, "product_id": 668541, "sub_type_name": "Normal", "market_price": 82.38,
         "low_price": 75.0, "mid_price": 84.0, "high_price": 150.0, "direct_low_price": 80.0,
         "source": "tcgcsv", "ingested_at": at},
    ], schema=schemas.FACT_PRICE_DAILY)


def test_keys_come_from_the_environment_first_then_the_keychain(monkeypatch):
    stored = {"SEALED_DATA_KEY": OTHER_KEY}
    monkeypatch.setattr(keys, "_keychain_get", lambda name: stored.get(name))
    assert keys.get("SEALED_DATA_KEY") == OTHER_KEY
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    assert keys.get("SEALED_DATA_KEY") == KEY
    assert keys.get("R2_ACCESS_KEY_ID") is None


def test_new_data_key_is_256_bits_and_bad_keys_are_rejected():
    k = keys.new_data_key()
    assert len(base64.b64decode(k, validate=True)) == 32 and k != keys.new_data_key()
    assert keys.validate_data_key(KEY) == KEY
    for bad in ("", "short", base64.b64encode(b"x" * 16).decode(), "not base64 at all!!" * 3):
        with pytest.raises(ValueError, match="32 bytes"):
            keys.validate_data_key(bad)


def test_status_never_prints_values(monkeypatch, capsys):
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    monkeypatch.setattr(keys, "_keychain_get", lambda name: "secret-id" if name == "R2_ACCESS_KEY_ID" else None)
    assert keys.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "SEALED_DATA_KEY: environment" in out and "R2_ACCESS_KEY_ID: keychain" in out
    assert "R2_SECRET_ACCESS_KEY: missing" in out
    assert KEY not in out and "secret-id" not in out


def test_new_refuses_to_replace_an_existing_key(monkeypatch):
    saved = {}
    monkeypatch.setattr(keys, "_keychain_get", lambda name: saved.get(name))
    monkeypatch.setattr(keys, "_keychain_set", lambda name, value: saved.__setitem__(name, value))
    assert keys.main(["new-data-key"]) == 0
    first = saved["SEALED_DATA_KEY"]
    assert keys.main(["new-data-key"]) == 1 and saved["SEALED_DATA_KEY"] == first


def test_encrypted_round_trip_keeps_the_schema():
    data = crypto.encrypt_table(prices(), KEY)
    assert crypto.is_encrypted(data) and b"668496" not in data and b"tcgcsv" not in data
    back = crypto.decrypt_bytes(data, KEY, schemas.FACT_PRICE_DAILY)
    assert back.schema == schemas.FACT_PRICE_DAILY
    assert back.to_pylist() == prices().to_pylist()


def test_wrong_or_missing_key_fails_loudly(tmp_path):
    data = crypto.encrypt_table(prices(), KEY)
    with pytest.raises(crypto.DecryptionFailed, match="fact_price_daily/x.parquet: wrong key"):
        crypto.decrypt_bytes(data, OTHER_KEY, label="fact_price_daily/x.parquet")
    (tmp_path / "x.parquet").write_bytes(data)
    with pytest.raises(OSError):  # other readers can't open it without the key either
        pq.read_table(tmp_path / "x.parquet")


def test_any_tampering_is_detected():
    data = crypto.encrypt_table(prices(), KEY)
    for pos in range(4, len(data) - 4, 29):  # a spread of bytes between the magic markers
        bad = bytearray(data)
        bad[pos] ^= 0x01
        with pytest.raises(crypto.DecryptionFailed):
            crypto.decrypt_bytes(bytes(bad), KEY)


def test_columns_added_later_read_as_nulls_like_plaintext():
    older = prices().drop_columns(["direct_low_price"])
    back = crypto.decrypt_bytes(crypto.encrypt_table(older, KEY), KEY, schemas.FACT_PRICE_DAILY)
    assert back.schema == schemas.FACT_PRICE_DAILY and back.column("direct_low_price").null_count == 2


def test_store_encrypts_when_a_key_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    path = store.write_daily(tmp_path, "fact_price_daily", DAY, prices())
    with open(path, "rb") as fh:
        assert crypto.is_encrypted(fh.read())
    st = store.open_store(tmp_path)
    rel = store.daily_rel("fact_price_daily", DAY)
    assert st.read_parquet(rel, schemas.FACT_PRICE_DAILY).to_pylist() == prices().to_pylist()
    assert st.num_rows(rel) == 2
    assert crypto.audit(st) == {"encrypted": [rel], "plaintext": []}


def test_store_reads_old_plaintext_files_and_names_the_missing_key(tmp_path, monkeypatch):
    rel = store.daily_rel("fact_price_daily", DAY)
    store.write_daily(tmp_path, "fact_price_daily", DAY, prices())  # no key: plaintext (local only)
    assert crypto.audit(store.open_store(tmp_path))["plaintext"] == [rel]
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    st = store.open_store(tmp_path)
    assert st.read_parquet(rel, schemas.FACT_PRICE_DAILY).num_rows == 2
    st.write_parquet("dim_x.parquet", prices())
    monkeypatch.delenv("SEALED_DATA_KEY")
    with pytest.raises(crypto.MissingKey, match="SEALED_DATA_KEY"):
        store.open_store(tmp_path).read_parquet("dim_x.parquet")


def test_public_store_never_encrypts(tmp_path, monkeypatch):
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    st = store.open_store(tmp_path, public=True)
    st.write_parquet("gold/product_latest.parquet", prices())
    assert pq.read_table(tmp_path / "gold" / "product_latest.parquet").num_rows == 2


def test_gold_upload_is_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("SEALED_DATA_KEY", KEY)
    local = tmp_path / "gold"
    (local / "price_series" / "group_id=24541").mkdir(parents=True)
    pq.write_table(prices(), local / "price_series" / "group_id=24541" / "data_0.parquet")
    (local / "manifest.json").write_text("{}")
    st = store.open_store(tmp_path / "data")
    assert st.upload_dir(local, "gold") == 2
    assert crypto.audit(st) == {"encrypted": ["gold/price_series/group_id=24541/data_0.parquet"], "plaintext": []}
    assert (tmp_path / "data" / "gold" / "manifest.json").read_text() == "{}"
