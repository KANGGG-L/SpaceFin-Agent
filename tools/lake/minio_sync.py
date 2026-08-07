"""MinIO 对象上传：把 data_lake/housing 的 Parquet 数据湖文件上传到 MinIO 桶。

不引入 minio/boto3 依赖（共享 venv 只装了 requests），这里用 requests 手写
AWS Signature V4 签名（MinIO 兼容 S3 协议）。上传路径按 类型 扁平化：
    housing/sale/<file>.parquet、housing/rent/<file>.parquet
城市信息保留在文件内的 district 列（安居客 district 即城市码），因此扁平化不丢维度。
"""

import datetime
import hashlib
import hmac
import os
import re
import urllib.parse

import requests
from config import MINIO

_REGION = "us-east-1"
_SERVICE = "s3"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date)
    k_region = _sign(k_date, _REGION)
    k_service = _sign(k_region, _SERVICE)
    return _sign(k_service, "aws4_request")


def _signed(method: str, url: str, data: bytes, access_key: str, secret_key: str):
    """对 S3 请求做 SigV4 签名并发送（GET/PUT/DELETE 通用）。

    返回原始 Response，调用方判 status_code。GET 的 query string 走 url 原文
    （path 编码只作用于路径部分）。
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    path = urllib.parse.quote(parsed.path, safe="/")
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(data).hexdigest()

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
    signed_headers = ";".join(sorted(headers))
    query = parsed.query
    canonical_request = (
        f"{method}\n{path}\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n{date_stamp}/{_REGION}/{_SERVICE}/aws4_request\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{date_stamp}/{_REGION}/{_SERVICE}/aws4_request, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers_out = {
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": auth,
    }
    if method in ("PUT", "DELETE"):
        return requests.request(method, url, data=data or b"", headers=headers_out, timeout=300)
    return requests.get(url, headers=headers_out, timeout=300)


def _ensure_bucket(endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
    """确保桶存在：PUT /bucket 幂等（已存在时 MinIO 返回 409，可忽略）。"""
    resp = _signed("PUT", f"{endpoint}/{bucket}", b"", access_key, secret_key)
    if resp.status_code not in (200, 409):
        raise RuntimeError(f"create bucket {bucket} failed: {resp.status_code} {resp.text[:200]}")


def put_object(bucket: str, key: str, data: bytes) -> None:
    """上传单个对象到 MinIO 桶。"""
    endpoint = MINIO["endpoint"].rstrip("/")
    resp = _signed(
        "PUT", f"{endpoint}/{bucket}/{key}", data, MINIO["access_key"], MINIO["secret_key"]
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"put object {bucket}/{key} failed: {resp.status_code} {resp.text[:200]}"
        )


def list_objects(bucket: str, prefix: str = "") -> list[str]:
    """列出桶内指定前缀下的对象 key（分页拉全，SigV4 签名）。

    prefix 中 '/' 必须编码为 %2F（MinIO 实测：签名以编码后字符串计算，
    原样 '/' 会 SignatureDoesNotMatch）。
    """
    endpoint = MINIO["endpoint"].rstrip("/")
    keys: list[str] = []
    marker = ""
    while True:
        params = [("list-type", "2")]
        if prefix:
            params.append(("prefix", urllib.parse.quote(prefix, safe="")))
        if marker:
            params.append(("continuation-token", urllib.parse.quote(marker, safe="")))
        query = "&".join(f"{k}={v}" for k, v in params)
        resp = _signed(
            "GET",
            f"{endpoint}/{bucket}?{query}",
            b"",
            MINIO["access_key"],
            MINIO["secret_key"],
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"list objects {bucket}/{prefix} failed: {resp.status_code} {resp.text[:200]}"
            )
        body = resp.text
        keys += re.findall(r"<Key>([^<]+)</Key>", body)
        truncated = re.search(r"<IsTruncated>([^<]+)</IsTruncated>", body)
        if not truncated or truncated.group(1) != "true":
            break
        token = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not token:
            break
        marker = token.group(1)
    return keys


def delete_object(bucket: str, key: str) -> None:
    """删除单个对象（不存在时 MinIO 返回 204，可忽略）。"""
    endpoint = MINIO["endpoint"].rstrip("/")
    resp = _signed(
        "DELETE", f"{endpoint}/{bucket}/{key}", b"", MINIO["access_key"], MINIO["secret_key"]
    )
    if resp.status_code not in (204, 200, 404):
        raise RuntimeError(
            f"delete object {bucket}/{key} failed: {resp.status_code} {resp.text[:200]}"
        )


def clear_prefix(bucket: str, prefix: str) -> int:
    """清空桶内指定前缀下的全部对象（湖快照每日重传，避免历史快照残留导致通配读重）。"""
    removed = 0
    for key in list_objects(bucket, prefix):
        delete_object(bucket, key)
        removed += 1
    if removed:
        print(f"[minio_sync] 清除 {prefix}* 旧对象 {removed} 个")
    return removed


def upload_lake_snapshot(lake_dir: str, snapshot_date: str) -> dict:
    """把 data_lake/housing/dt=<snapshot_date>/ 下 sale/rent 的 Parquet 上传到 MinIO。

    返回 {type: (文件数, 大小MB)}，用于对账与日志。

    幂等语义：MinIO 只保留「本次快照」——上传前先清空对应 type 前缀下的历史对象。
    lake_tvf() 用 `sale/*.parquet` 通配读取，若历史快照不清理，跨日运行会把多天
    快照拼在一起（url_key 重复、TVF 行数单调膨胀、ods_housing_sale_lake 重复）。
    """
    _ensure_bucket(MINIO["endpoint"], MINIO["bucket"], MINIO["access_key"], MINIO["secret_key"])
    result = {}
    date_dir = os.path.join(lake_dir, f"dt={snapshot_date}")
    if not os.path.isdir(date_dir):
        raise RuntimeError(f"lake snapshot dir not found: {date_dir}")
    for house_type in ("sale", "rent"):
        count = size_mb = 0
        clear_prefix(MINIO["bucket"], f"{house_type}/")
        type_dir = os.path.join(date_dir, f"type={house_type}")
        for city_dir in sorted(os.listdir(type_dir)) if os.path.isdir(type_dir) else []:
            city_path = os.path.join(type_dir, city_dir)
            if not os.path.isdir(city_path):
                continue
            for fname in sorted(os.listdir(city_path)):
                if not fname.endswith(".parquet"):
                    continue
                fpath = os.path.join(city_path, fname)
                with open(fpath, "rb") as f:
                    put_object(MINIO["bucket"], f"{house_type}/{fname}", f.read())
                count += 1
                size_mb += os.path.getsize(fpath) / 1024 / 1024
        result[house_type] = (count, round(size_mb, 2))
    return result
