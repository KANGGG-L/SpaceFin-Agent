"""tools/lake 测试公共 fixture：零真实 MinIO，全部走录制型假 transport。

被测模块在 lakemods.py（唯一模块名，原因见该文件顶部）。MINIO 配置由 fixture
固化到假端点，绝不真连 127.0.0.1:9000。
"""

import datetime

import lakemods
import pytest

minio_sync = lakemods.minio_sync


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class RecordingTransport:
    """录制型假 transport：替换 requests.request / requests.get。

    响应按队列依次返回，队列用尽后重复最后一个（默认 200 空响应）。
    """

    def __init__(self):
        self.calls = []  # (kind, method, url, data, headers)
        self._responses = []

    def reply(self, status_code=200, text=""):
        self._responses.append(FakeResponse(status_code, text))
        return self

    def _next(self):
        return self._responses.pop(0) if self._responses else FakeResponse()

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append(("request", method, url, data, headers))
        return self._next()

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("get", "GET", url, b"", headers))
        return self._next()


@pytest.fixture
def transport(monkeypatch):
    """把 requests 换成录制型假 transport，并固化 MINIO 配置指向假端点。"""
    t = RecordingTransport()
    monkeypatch.setattr(minio_sync.requests, "request", t.request)
    monkeypatch.setattr(minio_sync.requests, "get", t.get)
    monkeypatch.setattr(
        minio_sync,
        "MINIO",
        {
            "endpoint": "http://minio.test:9000",
            "access_key": "AKIDEXAMPLE",
            "secret_key": "SECRETKEY",
            "bucket": "housing",
        },
    )
    return t


class _FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 5, 12, 0, 0, tzinfo=tz)


@pytest.fixture
def frozen_time(monkeypatch):
    """把 datetime.datetime 固化为 2026-08-05T12:00:00Z，保证 SigV4 签名可复现。"""
    monkeypatch.setattr(datetime, "datetime", _FixedDateTime)
