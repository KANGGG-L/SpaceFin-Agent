"""批次③-B · R2-6（校准重建入口）/ R2-8（保存门径常驻）/ R2-9（合成数据标注）静态与路由不变量。

先红后绿的锚点：这三处改动前的代码都不满足下列断言——
- R2-8 改前 `#p6-save` 旁无 `#p6-save-hint`，`syncSaveState` 只写 title；
- R2-9 改前 `ltv>1` 行无行内解释 title（明细折叠的「演示数据」徽标已随统一口径移除）；
- R2-6 改前缺失/过期态只有一句「请联系数据团队 / 请重新生成」纯文本，无 make 命令、
  无按钮，`app.py` 无 `/api/persona/refresh` 路由、无 `persona_refresh` 审计。

运行时行为（按钮真实点击 → 后端重建 → 页面刷新）由工单 §H-11 页面实测另记。
"""

import json
import re
from pathlib import Path

import app as frontend_app
import db
import pytest

ROOT = Path(__file__).resolve().parents[3]
JS = ROOT / "tools" / "frontend" / "static" / "pages" / "p6_policy.js"


def _js_text() -> str:
    return JS.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    pattern = "function " + re.escape(name) + r"\([^)]*\)[^{}]*\{.*?\n  \}"
    m = re.search(pattern, text, re.S)
    assert m, f"未找到 function {name}"
    return m.group(0)


# ================= R2-8 保存门径常驻 =================


def test_save_button_has_adjacent_persistent_hint_span():
    text = _js_text()
    assert 'id="p6-save-hint"' in text, "缺 #p6-save-hint 常驻提示元素"
    # 骨架里 span 紧跟保存按钮（同一 flex 行），不是别的角落里。
    seg = text[text.find('id="p6-save"') : text.find('id="p6-save"') + 180]
    assert 'id="p6-save-hint"' in seg, "#p6-save-hint 不在保存按钮旁"


def test_sync_save_state_writes_hint_text():
    body = _function_body(_js_text(), "syncSaveState")
    assert "hint.textContent" in body, "syncSaveState 不写 #p6-save-hint"
    assert "未推演" in body and "运行推演" in body, "未推演态缺少「先点运行推演」文案"
    # 历史只读态新增一处常驻提示（原因文案），与默认态提示并列——两处都经 i18n，文案不堆解释。
    assert body.count("hint.textContent") == 2, "hint 文案驱动点应为默认态 + 历史只读态两处"
    assert "历史只读" in body, "历史只读态缺少原因文案（保存按钮禁用须明示为什么）"
    assert "window.confirm" not in body, "R2-8 不得新增确认框（第一轮 §0.1：会与保存确认弹两次）"


# ================= R2-9 合成数据标注 =================


def test_hit_detail_fold_has_no_demo_badge():
    text = _js_text()
    # 数据全合成统一前提后（见 README「数据全合成」），明细层不再挂合成数据徽标/说明。
    assert "演示数据</span>" not in text and "本页为演示用合成/脱敏数据" not in text, (
        "明细折叠仍残留「演示数据」徽标或合成数据说明"
    )
    # 明细折叠仍以「逐笔明细」为标题入口（口径同 R2-9，仅去徽标，不删主体）。
    assert "逐笔明细" in text, "明细折叠标题被误删"


def test_extreme_ltv_row_gets_explanatory_title():
    body = _function_body(_js_text(), "renderHitDetailTable")
    assert "非估值错误" in body, "ltv>1 行缺「非估值错误」行内解释"
    # 行内解释说明该样本为估值低于账面余额的尾部样本（单一数据口径，无需再标「合成」）。
    assert "尾部样本" in body, "行内解释未点明尾部样本"
    # 判据必须是条件拼进 title，不是全量替换点击展开的提示。
    # i18n 后标题经 t() 渲染（en/zh-HK 下翻译），仍须存在且未被极端值提示整体替换。
    assert 'title="${t("点击查看抵押物 / 风险网格详情")}' in body, "展开提示被极端值提示整体替换了"


def test_extreme_ltv_number_is_not_clamped():
    """308.07% 必须仍按原值渲染（平台可信度来自如实呈现，不 clamp 不滤除）。"""
    body = _function_body(_js_text(), "renderHitDetailTable")
    assert "fmtLtv(r.ltv)" in body or "fmtLtv(r.ltv_base)" in body, "明细仍渲染原始 LTV"
    assert "Math.min" not in body and "toFixed(0)" not in body, "明细渲染里出现 clamp 痕迹"


# ================= R2-6 校准重建入口 =================


def test_calibration_states_offer_regen_command_and_button():
    text = _js_text()
    # 缺失态（!data.available 分支）与过期/就绪态（notice.innerHTML）都要有可操作入口。
    assert text.count("make persona-refresh") >= 2, (
        "缺失态与过期态都应出现 make persona-refresh 命令（当前出现次数不足 2）"
    )
    assert text.count("refreshPersonaReport(this)") >= 2, "两处状态都应挂「重新生成」按钮"


def test_refresh_persona_function_uses_post_and_reloads():
    body = _function_body(_js_text(), "refreshPersonaReport")
    assert 'api("/api/persona/refresh"' in body, "按钮未调 /api/persona/refresh"
    assert '"POST"' in body, "重建是写操作，必须 POST"
    assert "正在重建" in body, "按钮运行中缺「正在重建」文案（A-3 同款）"
    assert "disabled" in body, "运行中未禁用按钮（防重复提交）"
    assert "loadSandbox()" in body, "重建成功未重新拉取沙盘（过期态不会随产物刷新）"


def test_refresh_persona_exposed_as_global():
    text = _js_text()
    assert "window.refreshPersonaReport = refreshPersonaReport" in text, (
        "IIFE 内联 onclick 无法寻址 refreshPersonaReport（需挂 window）"
    )


# ---------------- R2-6 后端：/api/persona/refresh 路由 + 审计 ----------------


class _RoutedHandler(frontend_app.SpaceFinApp):
    """与 test_report_actions.py 同套路：不建 socket 的 handler stub。"""

    def __init__(self, user, body=None, path=""):
        self._user = user
        self._body = body or {}
        self.path = path
        self.client_address = ("203.0.113.7", 0)
        self.sent = None

    def _current_user(self):
        return self._user

    def _parse_body(self):
        return self._body

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.sent = {"code": code, "body": body, "ctype": ctype, "extra": extra}

    def _send_json(self, code, data, extra=None):
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def _send_error(self, code, msg):
        self._send_json(code, {"error": msg})


class _FakeProc:
    def __init__(self, returncode=0, stdout="报告已写入", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_persona_refresh_route_exists_on_do_post():
    """路由注册不变量：POST /api/persona/refresh 必须在 app 分发器里、且 admin-only。"""
    # 直接断言源码里的分发分支（_RoutedHandler 只验证行为，这里锁注册本身）。
    src = Path(frontend_app.__file__).read_text(encoding="utf-8")
    assert 'path == "/api/persona/refresh"' in src, "app.py 未注册 /api/persona/refresh 路由"
    assert (
        "CAN_CONFIRM"
        in src[src.find("/api/persona/refresh") : src.find("/api/persona/refresh") + 400]
    ), "persona 重建是写操作，必须与确认同权限（admin）"


def test_persona_refresh_success_writes_audit(monkeypatch):
    calls = []
    monkeypatch.setattr(frontend_app, "_resolve_persona_python", lambda: "py")
    monkeypatch.setattr(frontend_app.subprocess, "run", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls.append(a))

    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, body={}, path="/api/persona/refresh")
    h.do_POST()

    assert h.sent["code"] == 200
    out = json.loads(h.sent["body"])
    assert out["ok"] is True and "报告已写入" in out["detail"]
    # 审计：action=persona_refresh / success / who / ip 一条不落。
    assert len(calls) == 1
    action, username, role, detail, result, ip = calls[0]
    assert action == "persona_refresh"
    assert username == "admin" and role == "admin"
    assert result == "success"
    assert ip == "203.0.113.7"


def test_persona_refresh_denied_for_non_admin(monkeypatch):
    monkeypatch.setattr(frontend_app.subprocess, "run", lambda *a, **k: pytest.fail("不应调用"))
    user = {"user": "postloan", "role": "postloan", "label": "贷后资产保全"}
    h = _RoutedHandler(user, body={}, path="/api/persona/refresh")
    h.do_POST()

    assert h.sent["code"] == 403
    assert "error" in json.loads(h.sent["body"])


def test_persona_refresh_failure_writes_failure_audit(monkeypatch):
    calls = []
    monkeypatch.setattr(frontend_app, "_resolve_persona_python", lambda: "py")
    monkeypatch.setattr(
        frontend_app.subprocess,
        "run",
        lambda *a, **k: _FakeProc(returncode=1, stdout="", stderr="boom"),
    )
    monkeypatch.setattr(db, "write_audit", lambda *a, **k: calls.append(a))

    user = {"user": "admin", "role": "admin", "label": "内部管理(风控/分析/管理员)"}
    h = _RoutedHandler(user, body={}, path="/api/persona/refresh")
    h.do_POST()

    assert h.sent["code"] == 500
    assert len(calls) == 1
    assert calls[0][0] == "persona_refresh" and calls[0][4] == "failure"


def test_persona_python_resolver_prefers_scipy_capable():
    """解释器解析：自身解释器带 scipy 则直接用，否则找候选里第一个带 scipy 的。"""
    src = Path(frontend_app.__file__).read_text(encoding="utf-8")
    assert "def _resolve_persona_python" in src, "缺解释器解析函数"
    assert "_has_scipy" in src, "解析没有 scipy 探测（会退回一个必然 ModuleNotFoundError 的解释器）"
