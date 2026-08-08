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


def _signed(method: str, url: str, data, access_key: str, secret_key: str, extra_headers=None):
    """对 S3 请求做 SigV4 签名并发送（GET/PUT/DELETE/COPY 通用）。

    data 可为 bytes 或「打开的文件对象」：文件对象时按分块算 payload sha256（避免整文件
    读内存），并 seek(0) 后交给 requests 流式上传。extra_headers 透传进签名与请求头
    （如服务端 copy 的 x-amz-copy-source）。返回原始 Response，调用方判 status_code。
    GET 的 query string 走 url 原文（path 编码只作用于路径部分）。
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    path = urllib.parse.quote(parsed.path, safe="/")
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    if isinstance(data, (bytes, bytearray)):
        payload_hash = hashlib.sha256(data).hexdigest()
        send_data = data
    else:  # 文件对象：分块算 hash 后回到开头，交给 requests 流式读取（控制内存）
        data.seek(0)
        h = hashlib.sha256()
        for chunk in iter(lambda: data.read(1024 * 1024), b""):
            h.update(chunk)
        payload_hash = h.hexdigest()
        data.seek(0)
        send_data = data

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        headers.update(extra_headers)
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
    if extra_headers:
        headers_out.update(extra_headers)
    if method in ("PUT", "DELETE"):
        return requests.request(
            method, url, data=send_data or b"", headers=headers_out, timeout=300
        )
    return requests.get(url, headers=headers_out, timeout=300)


def _ensure_bucket(endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
    """确保桶存在：PUT /bucket 幂等（已存在时 MinIO 返回 409，可忽略）。"""
    resp = _signed("PUT", f"{endpoint}/{bucket}", b"", access_key, secret_key)
    if resp.status_code not in (200, 409):
        raise RuntimeError(f"create bucket {bucket} failed: {resp.status_code} {resp.text[:200]}")


def put_object(bucket: str, key: str, data: bytes) -> None:
    """上传单个对象到 MinIO 桶（data 为 bytes）。"""
    endpoint = MINIO["endpoint"].rstrip("/")
    resp = _signed(
        "PUT", f"{endpoint}/{bucket}/{key}", data, MINIO["access_key"], MINIO["secret_key"]
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"put object {bucket}/{key} failed: {resp.status_code} {resp.text[:200]}"
        )


def put_object_file(bucket: str, key: str, path: str) -> None:
    """流式上传本地文件到 MinIO（分块算 hash，整文件不驻留内存）。

    大 Parquet 直接 f.read() 会占满内存，这里把文件对象交给 requests 流式读取，
    payload hash 由 _signed 分块计算。
    """
    endpoint = MINIO["endpoint"].rstrip("/")
    with open(path, "rb") as f:
        resp = _signed(
            "PUT", f"{endpoint}/{bucket}/{key}", f, MINIO["access_key"], MINIO["secret_key"]
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"put object {bucket}/{key} from {path} failed: {resp.status_code} {resp.text[:200]}"
        )


def copy_object(bucket: str, src_key: str, dst_key: str) -> None:
    """服务端复制对象（原子搬前缀用，不重传、不占内存）。"""
    endpoint = MINIO["endpoint"].rstrip("/")
    url = f"{endpoint}/{bucket}/{dst_key}"
    resp = _signed(
        "PUT",
        url,
        b"",
        MINIO["access_key"],
        MINIO["secret_key"],
        extra_headers={"x-amz-copy-source": f"/{bucket}/{src_key}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"copy object {bucket}/{src_key} -> {bucket}/{dst_key} failed: "
            f"{resp.status_code} {resp.text[:200]}"
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

    幂等语义：MinIO 只保留「本次快照」——上传后清空对应 type 前缀下的历史对象。
    lake_tvf() 用 `sale/*.parquet` 通配读取，若历史快照不清理，跨日运行会把多天
    快照拼在一起（url_key 重复、TVF 行数单调膨胀、ods_housing_sale_lake 重复）。

    健壮性（C 类，原实现先 clear_prefix 再上传）：中途失败会留下「前缀已清、快照残缺」
    的半截状态。改为三阶段事务式上传：
      1) 全部文件**流式**上传到 staging 前缀 `pending/<date>/<type>/<fname>`（整文件不驻留内存）；
      2) 全部 staging 成功**之后**才清旧前缀 `sale/` `rent/`（任一分片上传失败则直接上抛，
         旧快照原样保留，绝不会出现「清完即崩」的残缺）；
      3) 服务端 copy 把 staging 搬到正式前缀（不重传、不占内存），再清 staging。
    只有阶段 2、3 全部成功，正式前缀才是完整新快照；任意阶段失败都保持旧快照可用。
    """
    _ensure_bucket(MINIO["endpoint"], MINIO["bucket"], MINIO["access_key"], MINIO["secret_key"])
    result = {}
    date_dir = os.path.join(lake_dir, f"dt={snapshot_date}")
    if not os.path.isdir(date_dir):
        raise RuntimeError(f"lake snapshot dir not found: {date_dir}")

    staging_prefix = f"pending/{snapshot_date}/"

    # 阶段 1：全部文件流式上传到 staging 前缀（上传失败直接上抛，旧快照不动）
    for house_type in ("sale", "rent"):
        count = size_mb = 0
        type_dir = os.path.join(date_dir, f"type={house_type}")
        for city_dir in sorted(os.listdir(type_dir)) if os.path.isdir(type_dir) else []:
            city_path = os.path.join(type_dir, city_dir)
            if not os.path.isdir(city_path):
                continue
            for fname in sorted(os.listdir(city_path)):
                if not fname.endswith(".parquet"):
                    continue
                fpath = os.path.join(city_path, fname)
                put_object_file(MINIO["bucket"], f"{staging_prefix}{house_type}/{fname}", fpath)
                count += 1
                size_mb += os.path.getsize(fpath) / 1024 / 1024
        result[house_type] = (count, round(size_mb, 2))

    # 阶段 2：全部 staging 成功后才清旧前缀（避免「清完即崩 → 残缺快照」）
    for house_type in ("sale", "rent"):
        clear_prefix(MINIO["bucket"], f"{house_type}/")

    # 阶段 3：staging 服务端 copy 到正式前缀，再清 staging
    for house_type in ("sale", "rent"):
        staging_base = f"{staging_prefix}{house_type}/"
        for src in list_objects(MINIO["bucket"], staging_base):
            dst = src
            if dst.startswith(staging_base):
                dst = f"{house_type}/" + dst[len(staging_base) :]
            copy_object(MINIO["bucket"], src, dst)
        for src in list_objects(MINIO["bucket"], staging_base):
            delete_object(MINIO["bucket"], src)

    return result
