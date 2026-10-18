"""slack_notify：告警文案格式 + POST 行为（零依赖，monkeypatch urlopen）。"""

import importlib.util
import os
import sys

SPEC = importlib.util.spec_from_file_location(
    "slack_notify_under_test",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "slack_notify.py"),
)
sn = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sn
SPEC.loader.exec_module(sn)


class _FakeResp:
    def __init__(self, code):
        self._code = code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self._code


class _RecordingTransport:
    def __init__(self, code=200):
        self.code = code
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        return _FakeResp(self.code)


def test_build_alert_message_includes_roles_and_mentions():
    msg = sn.build_alert_message("risk_recalc", "2026-08-05", "开发/产品", "boom", "@oncall")
    assert "产品/开发/QA/审核" in msg
    assert "负责: 开发/产品" in msg
    assert "@oncall" in msg
    assert "任务: risk_recalc" in msg
    assert "详情: boom" in msg


def test_post_slack_returns_false_without_webhook():
    assert sn.post_slack("", "hi") is False


def test_post_slack_posts_json_on_success(monkeypatch):
    transport = _RecordingTransport(200)
    monkeypatch.setattr(sn.urllib.request, "urlopen", transport)
    assert sn.post_slack("https://hooks.slack.com/x", "hello") is True
    assert len(transport.requests) == 1
    req = transport.requests[0][0]
    body = __import__("json").loads(req.data.decode("utf-8"))
    assert body["text"] == "hello"


def test_post_slack_returns_false_on_network_error(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("conn refused")

    monkeypatch.setattr(sn.urllib.request, "urlopen", _boom)
    # 通知失败绝不能抛出——返回 False 即可。
    assert sn.post_slack("https://hooks.slack.com/x", "hi") is False
