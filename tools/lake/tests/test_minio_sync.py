"""tools/lake minio_sync 测试：零真实 MinIO，全部走录制型假 transport。

覆盖点（对应 minio_sync.py 的职责）：
- SigV4 签名：Authorization / x-amz-date / x-amz-content-sha256 头；canonical_request
  包含 query string（用独立参考实现复算签名验证）；GET 走 requests.get、PUT/DELETE
  走 requests.request；
- list_objects：分页拉全、prefix 中 '/' 编码为 %2F、无对象返回空、非 200 抛错；
- delete_object：204/404 幂等不抛、500 抛错；
- clear_prefix：先 list 后逐个 delete 并返回删除数；
- upload_lake_snapshot：上传前清空对应 type 前缀、快照目录缺失抛错、只传 parquet
  并返回对账信息。
"""

import hashlib
import hmac
import re
import urllib.parse

import lakemods
import pytest

minio_sync = lakemods.minio_sync
_signed = minio_sync._signed

# 固定签名时间（frozen_time fixture）：2026-08-05T12:00:00Z
_AMZ_DATE = "20260805T120000Z"
_DATE_STAMP = "20260805"
_ACCESS = "AKIDEXAMPLE"
_SECRET = "SECRETKEY"
_ENDPOINT = "http://minio.test:9000"
_BUCKET = "housing"


# ---------------------------------------------------------------- SigV4 辅助


def _reference_signature(
    method,
    url,
    data,
    access_key=_ACCESS,
    secret_key=_SECRET,
    amz_date=_AMZ_DATE,
    date_stamp=_DATE_STAMP,
):
    """独立复算 SigV4 签名，不调用被测模块的签名函数，用于验证签名串内容。"""

    def _sign(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.quote(parsed.path, safe="/")
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
    signed_headers = ";".join(sorted(headers))
    canonical_request = (
        f"{method}\n{path}\n{parsed.query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n{date_stamp}/us-east-1/s3/aws4_request\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, "us-east-1")
    k_service = _sign(k_region, "s3")
    k_signing = _sign(k_service, "aws4_request")
    return hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def _extract_signature(auth):
    m = re.search(r"Signature=([0-9a-f]{64})", auth)
    assert m, f"Authorization 里没有 Signature: {auth}"
    return m.group(1)


def _list_xml(keys, truncated=False, token=None):
    contents = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    truncated_tag = (
        "<IsTruncated>true</IsTruncated>" if truncated else "<IsTruncated>false</IsTruncated>"
    )
    token_tag = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f"<ListBucketResult>{truncated_tag}{contents}{token_tag}</ListBucketResult>"


def _make_snapshot(root, files):
    """构造快照目录树 root/dt=2026-08-05/type=<t>/<city>/<file>，内容为指定字节数。"""
    for rel, size in files.items():
        t, city, fname = rel.split("/")
        d = root / "dt=2026-08-05" / f"type={t}" / city
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_bytes(b"x" * size)


# ================================================================ SigV4 签名


def test_sigv4_auth_header_has_expected_format(transport, frozen_time):
    """Authorization 头符合 AWS4-HMAC-SHA256 Credential=ak/date/us-east-1/s3/aws4_request。"""
    _signed("GET", f"{_ENDPOINT}/{_BUCKET}?list-type=2", b"", _ACCESS, _SECRET)

    auth = transport.calls[0][4]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert f"Credential={_ACCESS}/{_DATE_STAMP}/us-east-1/s3/aws4_request" in auth
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert re.search(r"Signature=[0-9a-f]{64}", auth)


def test_sigv4_headers_amz_date_and_content_sha256(transport, frozen_time):
    """x-amz-date 为 UTC 时间戳，x-amz-content-sha256 等于请求体 sha256。"""
    body = b"parquet-bytes"
    _signed("PUT", f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet", body, _ACCESS, _SECRET)

    _, _, url, data, headers = transport.calls[0]
    assert headers["x-amz-date"] == _AMZ_DATE
    assert headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
    assert url.endswith("/sale/a.parquet")
    assert data == body


def test_sigv4_canonical_request_includes_query_string(transport, frozen_time):
    """query string 必须进入签名串：签名与「带 query 的参考签名」一致、与「无 query」不同。"""
    url = f"{_ENDPOINT}/{_BUCKET}?list-type=2&prefix=sale%2F"
    _signed("GET", url, b"", _ACCESS, _SECRET)

    sig = _extract_signature(transport.calls[0][4]["Authorization"])
    assert sig == _reference_signature("GET", url, b"")
    assert sig != _reference_signature("GET", url.split("?")[0], b"")


def test_sigv4_different_query_yields_different_signature(transport, frozen_time):
    """仅 query 不同时签名不同 → 证明 query 参与签名（而非只影响 URL）。"""
    _signed("GET", f"{_ENDPOINT}/{_BUCKET}?prefix=sale%2F", b"", _ACCESS, _SECRET)
    _signed("GET", f"{_ENDPOINT}/{_BUCKET}?prefix=rent%2F", b"", _ACCESS, _SECRET)

    s1 = _extract_signature(transport.calls[0][4]["Authorization"])
    s2 = _extract_signature(transport.calls[1][4]["Authorization"])
    assert s1 != s2


def test_sigv4_get_goes_through_requests_get(transport, frozen_time):
    _signed("GET", f"{_ENDPOINT}/{_BUCKET}?list-type=2", b"", _ACCESS, _SECRET)

    kind, method, url, _, _ = transport.calls[0]
    assert kind == "get"
    assert method == "GET"
    assert url == f"{_ENDPOINT}/{_BUCKET}?list-type=2"


def test_sigv4_put_goes_through_requests_request(transport, frozen_time):
    body = b"data"
    _signed("PUT", f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet", body, _ACCESS, _SECRET)

    kind, method, url, data, _ = transport.calls[0]
    assert kind == "request"
    assert method == "PUT"
    assert data == body
    assert url == f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet"


def test_sigv4_delete_goes_through_requests_request(transport, frozen_time):
    _signed("DELETE", f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet", b"", _ACCESS, _SECRET)

    kind, method, url, _, _ = transport.calls[0]
    assert kind == "request"
    assert method == "DELETE"
    assert url == f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet"


# ================================================================ list_objects


def test_list_objects_paginates_all_pages(transport):
    transport.reply(
        200, _list_xml(["sale/a.parquet", "sale/b.parquet"], truncated=True, token="tok-2")
    )
    transport.reply(200, _list_xml(["sale/c.parquet"]))

    keys = minio_sync.list_objects(_BUCKET, "sale/")

    assert keys == ["sale/a.parquet", "sale/b.parquet", "sale/c.parquet"]
    assert transport.calls[0][2] == f"{_ENDPOINT}/{_BUCKET}?list-type=2&prefix=sale%2F"
    assert transport.calls[1][2].endswith("continuation-token=tok-2")


def test_list_objects_prefix_slash_encoded_as_pct2f(transport):
    transport.reply(200, _list_xml([]))

    minio_sync.list_objects(_BUCKET, "sale/")

    url = transport.calls[0][2]
    assert "prefix=sale%2F" in url
    assert "prefix=sale/" not in url


def test_list_objects_no_objects_returns_empty_list(transport):
    transport.reply(200, _list_xml([]))

    assert minio_sync.list_objects(_BUCKET) == []


def test_list_objects_http_error_raises(transport):
    transport.reply(500, "internal error")

    with pytest.raises(RuntimeError, match="list objects"):
        minio_sync.list_objects(_BUCKET, "sale/")


# ================================================================ delete_object


def test_delete_object_204_ok(transport):
    transport.reply(204)

    minio_sync.delete_object(_BUCKET, "sale/a.parquet")

    assert transport.calls[0][1] == "DELETE"
    assert transport.calls[0][2] == f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet"


def test_delete_object_404_idempotent(transport):
    transport.reply(404)

    minio_sync.delete_object(_BUCKET, "sale/gone.parquet")  # 不存在也算成功，不抛


def test_delete_object_500_raises(transport):
    transport.reply(500, "boom")

    with pytest.raises(RuntimeError, match="delete object"):
        minio_sync.delete_object(_BUCKET, "sale/a.parquet")


# ================================================================ clear_prefix


def test_clear_prefix_deletes_each_listed_key(transport):
    transport.reply(200, _list_xml(["sale/a.parquet", "sale/b.parquet", "sale/c.parquet"]))

    removed = minio_sync.clear_prefix(_BUCKET, "sale/")

    assert removed == 3
    deletes = [c for c in transport.calls if c[0] == "request" and c[1] == "DELETE"]
    assert [c[2] for c in deletes] == [
        f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet",
        f"{_ENDPOINT}/{_BUCKET}/sale/b.parquet",
        f"{_ENDPOINT}/{_BUCKET}/sale/c.parquet",
    ]


def test_clear_prefix_empty_prefix_returns_zero(transport):
    transport.reply(200, _list_xml([]))

    assert minio_sync.clear_prefix(_BUCKET, "sale/") == 0
    assert not [c for c in transport.calls if c[1] == "DELETE"]


# ================================================================ upload_lake_snapshot


def test_upload_clears_old_prefix_before_upload(transport, tmp_path, monkeypatch):
    """上传前必须按 type 清空历史前缀（湖快照每日重传，防通配读重）。"""
    (tmp_path / "dt=2026-08-05").mkdir()  # 无 type=* 子目录，避免走上传分支
    calls = []
    monkeypatch.setattr(
        minio_sync, "clear_prefix", lambda bucket, prefix: calls.append((bucket, prefix)) or 0
    )

    minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")

    assert calls == [(_BUCKET, "sale/"), (_BUCKET, "rent/")]


def test_upload_missing_snapshot_dir_raises(transport, tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")


def test_upload_only_parquet_files_are_uploaded(transport, tmp_path, monkeypatch):
    _make_snapshot(
        tmp_path,
        {
            "sale/beijing/a.parquet": 16,
            "sale/beijing/a.txt": 8,  # 非 parquet 跳过
            "sale/shanghai/b.parquet": 16,
            "rent/guangzhou/c.parquet": 16,
        },
    )

    # 让 list_objects 在 staging 前缀下返回已上传的分片，使 copy 阶段能产出正式前缀 PUT
    def _staging_keys(bucket, prefix):
        if prefix.startswith("pending/"):
            t = prefix.split("/")[2]
            files = {"sale": ["a.parquet", "b.parquet"], "rent": ["c.parquet"]}[t]
            return [f"pending/2026-08-05/{t}/{f}" for f in files]
        return []

    monkeypatch.setattr(minio_sync, "list_objects", _staging_keys)

    result = minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")

    assert result["sale"][0] == 2 and result["rent"][0] == 1
    # 只看正式前缀的最终 PUT（copy 阶段），排除 staging 阶段的中间 PUT
    puts = [
        c[2]
        for c in transport.calls
        if c[1] == "PUT" and "/housing/" in c[2] and "/pending/" not in c[2]
    ]
    assert puts == [
        f"{_ENDPOINT}/{_BUCKET}/sale/a.parquet",
        f"{_ENDPOINT}/{_BUCKET}/sale/b.parquet",
        f"{_ENDPOINT}/{_BUCKET}/rent/c.parquet",
    ]
    assert not any("/a.txt" in u for u in puts)


def test_upload_reports_file_count_and_size_mb(transport, tmp_path):
    mb = 1024 * 1024
    _make_snapshot(
        tmp_path,
        {
            "sale/beijing/a.parquet": int(1.5 * mb),  # 1.5MB
            "sale/beijing/b.parquet": int(0.5 * mb),  # 0.5MB
            "rent/guangzhou/c.parquet": mb,  # 1.0MB
        },
    )

    result = minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")

    assert result["sale"] == (2, 2.0)
    assert result["rent"] == (1, 1.0)


# ================================================================ 事务式上传（C 类健壮性）


def test_upload_fails_before_clearing_old_prefix(monkeypatch, tmp_path):
    """任一分片上传失败 → 上抛且不调用 clear_prefix（旧快照原样保留，绝不残缺）。"""
    _make_snapshot(tmp_path, {"sale/beijing/a.parquet": 16, "sale/beijing/b.parquet": 16})

    cleared = []
    monkeypatch.setattr(minio_sync, "clear_prefix", lambda bucket, prefix: cleared.append(prefix))
    monkeypatch.setattr(
        minio_sync,
        "put_object_file",
        lambda bucket, key, path: (_ for _ in ()).throw(RuntimeError("staging failed")),
    )

    with pytest.raises(RuntimeError, match="staging failed"):
        minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")

    assert cleared == [], "上传失败仍清了旧前缀 → 会留下残缺快照"


def test_upload_stages_then_clears_then_moves_in_order(monkeypatch, tmp_path):
    """阶段顺序：全部 staging PUT → 清旧前缀 → copy 到正式前缀 → 删 staging。"""
    _make_snapshot(
        tmp_path,
        {"sale/beijing/a.parquet": 16, "rent/guangzhou/c.parquet": 16},
    )

    order = []
    monkeypatch.setattr(
        minio_sync,
        "put_object_file",
        lambda bucket, key, path: order.append(("put_staging", key)),
    )
    monkeypatch.setattr(
        minio_sync, "clear_prefix", lambda bucket, prefix: order.append(("clear", prefix))
    )
    monkeypatch.setattr(
        minio_sync,
        "list_objects",
        lambda bucket, prefix: (
            [f"pending/2026-08-05/{prefix.split('/')[2]}/a.parquet"]
            if prefix.startswith("pending/")
            else []
        ),
    )
    monkeypatch.setattr(
        minio_sync,
        "copy_object",
        lambda bucket, src, dst: order.append(("copy", src, dst)),
    )
    monkeypatch.setattr(
        minio_sync, "delete_object", lambda bucket, key: order.append(("del_staging", key))
    )

    minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")

    # 先全是 staging 上传
    assert all(k == "put_staging" for k, *_ in order if k == "put_staging")
    # clear 出现在所有 staging 之后
    first_clear = next(i for i, (k, *_) in enumerate(order) if k == "clear")
    assert all(k == "put_staging" for k, *_ in order[:first_clear])
    # copy 出现在 clear 之后，且拷贝到正式前缀 sale/ rent/
    copies = [entry[1:] for entry in order if entry[0] == "copy"]
    assert copies and all(d.startswith(("sale/", "rent/")) for _, d in copies)
    # staging 全部删除
    assert any(k == "del_staging" for k, *_ in order)


def test_upload_streams_from_file_not_bytes(monkeypatch, tmp_path):
    """staging 上传应走 put_object_file（文件流），而非整文件读内存的 put_object。"""
    _make_snapshot(tmp_path, {"sale/beijing/a.parquet": 16})
    seen = []
    monkeypatch.setattr(minio_sync, "put_object_file", lambda bucket, key, path: seen.append(path))
    monkeypatch.setattr(minio_sync, "clear_prefix", lambda bucket, prefix: None)
    monkeypatch.setattr(
        minio_sync,
        "list_objects",
        lambda bucket, prefix: [] if prefix.startswith("pending/") else [],
    )
    monkeypatch.setattr(minio_sync, "copy_object", lambda b, s, d: None)
    monkeypatch.setattr(minio_sync, "delete_object", lambda b, k: None)

    minio_sync.upload_lake_snapshot(str(tmp_path), "2026-08-05")

    assert seen and all(isinstance(p, str) and p.endswith(".parquet") for p in seen)
