#!/usr/bin/env python
"""共享叶模块：自然语言 → 结构化的「传输/守卫原语」层（0 依赖，只用 stdlib）。

从 p6_policy 的 NL 守卫段机械抽出的一批可复用原语：输入清洗、prompt 注入检测、
模型可用性/限流/结果缓存、Anthropic 协议请求与响应文本抽取、日志指纹。

**0 依赖纪律**：本文件禁止 import components / pages / db / config / sandbox_agent——
否则会与 p6/p10 产生循环或耦合。配置池（城市/行业枚举、动作、LTV 区间等）由**调用方**
经 build_prompt 注入；正则解析/校验（_validate_llm_draft 等）依赖业务配置，因此留在
p6_policy 等业务模块，不进本文件。

任何接入 AI 链路的业务模块（p6_policy.nl_parse、ai_decompose）都按名导入本文件，
共享同一组常量、限流态与缓存对象（_NL_LOCK/_NL_LLM_CALLS/_NL_LLM_CACHE 进程内单例）。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request

_NL_LOG = logging.getLogger(__name__)

# 仓库根：本文件在 <root>/tools/frontend/ 下，回退三层。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env():
    """只读仓库根 .env（与 sandbox_agent 同逻辑，读不写）。"""
    env = {}
    path = os.path.join(_REPO_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


# 开发态放宽（SF_DEV_MODE=1，dev_server.py 启动本地服务时注入）：
# 本地反复调试时，每端点 3 次/分 + 30 次/日 的额度与 200 字符输入上限会轻易触发
# 「静默降级」，让人误以为「未接入智能推理」。生产不设该开关即保持严格默认。
_DEV_RELAX = str(os.environ.get("SF_DEV_MODE", "")).strip() in ("1", "true", "True")

NL_LLM_MAX_CHARS = 2000 if _DEV_RELAX else 200
NL_LLM_DEFAULT_BASE = "https://api.mistral.ai/v1"
NL_LLM_DEFAULT_MODEL = "mistral-small-2603"
NL_LLM_TOTAL_BUDGET = 120  # 整条模型回退链的总耗时上限（同步 handler）。上调到 120 给单模型 90s 读超时 + 超时重试 1 次留出空间，
# 避免慢但仍有效的推理模型响应被预算硬截断（否则重试只会在读超时时又把同一请求立刻掐死）。
# 2026-10-14：换 某推理服务端点 的 deepseek-v4-flash-0731（推理模型，思考不可关）后，
# decompose 单次实测 ~50s，原 70s 预算余量不足。
# 800：同上的思考模型变体会先烧掉一部分 token 在 reasoning_content 上，300 在输入稍复杂时
# 可能被思考占满 → content 为空 → 静默降级。留余量到 800。
NL_LLM_MAX_TOKENS = 800
# decompose 的输出是 rationale + 1-3 个 option（每个含 reason/explains/impact/三语
# text_i18n），中文 JSON 轻松超 300 token——300 会截断输出 → _extract_json_object
# 失败 → 静默落确定性兜底。max_tokens 因此改为调用方可传参数：nl_parse 保持 800
# （NL_LLM_MAX_TOKENS 缺省），decompose 传本值（见 _nl_llm_call / _nl_llm_request 的 max_tokens 形参）。
# 4000：2026-10-14 换用 某推理服务端点 的 deepseek-v4-flash-0731（推理模型，enable_thinking /
# chat_template_kwargs / reasoning_effort 均无法关闭思考）后，1200 会被 reasoning_content 占满
# → content 为空、finish_reason=length → 静默落确定性兜底。实测 4000 可完整输出（~50s）。
NL_LLM_DECOMPOSE_MAX_TOKENS = 4000
# urllib 只接受**单一** socket 超时（连接与读共用），不像 requests 支持 (连接, 读) 元组。
# 2026-09-22：commandcode 网关上的推理变体（deepseek/deepseek-v4-flash-fast）偶发慢响应（负载高时首 token >25s），
# 原 25s 会被 TimeoutError 掐断而误报「无法连接」。放宽到 60s 并配合超时重试，抵消间歇性慢。
# 2026-10-14：换 某推理服务端点 的 deepseek-v4-flash-0731 后，decompose（max_tokens=4000）
# 实测 ~50s，60s 余量不足，再放宽到 90s。
NL_LLM_READ_TIMEOUT = 90  # 单次请求读超时预算（适配慢推理模型的间歇性延迟）
NL_LLM_ATTEMPTS = 2  # 同一模型对「读超时」的最大尝试次数（其余错误/超次数即换模型）
NL_LLM_RATE_PER_MIN = 30 if _DEV_RELAX else 3
NL_LLM_RATE_PER_DAY = 300 if _DEV_RELAX else 30
NL_LLM_CACHE_MAX = 200
NL_LLM_CACHE_TTL = 3600
NL_LLM_RESP_MAX_BYTES = (
    524288  # 上游响应体上限（字节）：防恶意上游吐超大响应拖垮 json 解析 / 内存（上游信任边界硬化）
)
NL_LLM_RAW_MAX_CHARS = 20000  # 抽取文本上限（字符）：模型输出按此截断，再进后续校验 / 展示

# 疑似 prompt 注入的模式（中英双语）。命中即拒绝调用——宁可误伤（回退正则），
# 也不把可能是指令的内容送进不可信模型。
_INJECTION_RES = (
    r"忽略(?:以上|前面|上述|之前)(?:的)?(?:指令|规则|提示|要求|内容)",
    r"(?:ignore|disregard|forget)\b.{0,12}(?:previous|above|all)\b.{0,12}(?:instruction|prompt|rule)",
    r"(?:system|assistant|developer)\s*[:：]",
    r"(?:你现在是|你是一个|扮演|假装是|act\s+as|pretend\s+to\s+be|role\s*play)",
    r"(?:system\s*prompt|开发者(?:指令|消息)|初始提示词|原始提示)",
    r"输出.{0,8}(?:提示词|prompt|指令|系统词)",
    r"(?:repeat|reveal|print|show)\b.{0,12}(?:above|previous|以上内容|提示词)",
    r"<\|?(?:im_start|im_end|endoftext|system)\|?>",
    r"(?:base64|rot13|用.{0,4}(?:编码|加密).{0,8}(?:指令|提示))",
    # URL 类收窄（历史 bug：裸 https?:// 会把「参考 https://… 的数据，房价下跌 10%」
    # 这类带来源引用的合法业务输入整条判成注入、静默阻断 LLM）。裸 URL 对本链路
    # 不构成注入——请求原语不会fetch URL，只有指向本机/内网/云元数据端点的地址
    # 才近乎必然是恶意指令，只拦这类。
    r"(?:sudo|rm\s+-rf|curl\s|wget\s)",
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1|169\.254\.169\.254|metadata)",
    r"重复(?:以上|上面|之前).{0,10}(?:内容|指令|提示)",
)

# 限流与结果缓存：进程内单实例足够（handler 低频，不需要 Redis）。
_NL_LOCK = threading.Lock()
_NL_LLM_CALLS = []  # [(ts, user)]
_NL_LLM_CACHE = {}  # key -> (ts, draft)


def _sanitize_nl_text(text):
    """清洗用户输入：去控制字符/零宽、NFKC、空白归一、截断到 NL_LLM_MAX_CHARS。

    返回 (clean, truncated)。只影响「是否送模型」与注入判据——正则解析跑的仍是原句，
    既有行为因此逐字节不变。
    """
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or ""))
    s = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", s)
    s = unicodedata.normalize("NFKC", s)
    # 定界逃逸防护：用户文本嵌在 <user_text>…</user_text> 内送模型（p6 的
    # _nl_build_prompt 与 decompose 的 _decompose_build_prompt 同款），原话里若带
    # 字面「</user_text>」可提前闭合定界块、把其后内容伪造成模型可执行上下文——
    # 两侧共用本清洗，剥除定界串（大小写/空白变体一并覆盖）。
    s = re.sub(r"</?\s*user_text\s*>", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:NL_LLM_MAX_CHARS], len(s) > NL_LLM_MAX_CHARS


def _looks_like_injection(text):
    """疑似 prompt 注入 → True。命中就不调模型。"""
    s = str(text or "")
    return any(re.search(p, s, re.I) for p in _INJECTION_RES)


def _nl_env():
    """读仓库根 .env（本地 _load_env 实现，0 依赖）。"""
    try:
        return _load_env()
    except Exception:  # noqa: BLE001 - 读不到就当没配，走正则
        return {}


def _nl_kv(env, *names, default=""):
    """按序取 .env 键，命中第一个非空即返回。"""
    for n in names:
        v = str((env or {}).get(n, "")).strip()
        if v:
            return v
    return default


def _nl_llm_enabled(env):
    """有 key 且开关未关。缺 key 即静默走正则（.env.example 已登记，避免无声降级）。"""
    key = _nl_kv(env, "LLM_KEY")
    if not key:
        return False
    return str((env or {}).get("SPACEFIN_NL_LLM", "1")).strip() not in ("0", "false", "False", "")


def _nl_rate_allow(user, bucket="nl"):
    """每桶（端点）每用户 3 次/分、30 次/日。超限即拒绝调用（不是报错）。

    bucket 隔离 nl_parse 与 decompose 两条链的配额：一次确认点击会**串行触发两个
    端点**，共享池会把单次操作烧掉 2/3 配额。只做检查、不登记：计数在请求成功
    返回后由 _nl_rate_record 登记——超时/失败的调用不烧配额（否则一次上游抖动
    就白扣 1/3 额度，放大降级频率）。
    """
    now = time.time()
    with _NL_LOCK:
        _NL_LLM_CALLS[:] = [c for c in _NL_LLM_CALLS if now - c[0] < 86400]
        if (
            sum(1 for c in _NL_LLM_CALLS if c[1] == user and c[2] == bucket and now - c[0] < 60)
            >= NL_LLM_RATE_PER_MIN
        ):
            return False
        if sum(1 for c in _NL_LLM_CALLS if c[1] == user and c[2] == bucket) >= NL_LLM_RATE_PER_DAY:
            return False
        return True


def _nl_rate_record(user, bucket="nl"):
    """请求成功返回后登记一次调用（配额检查在 _nl_rate_allow，二者分离）。"""
    with _NL_LOCK:
        _NL_LLM_CALLS.append((time.time(), user, bucket))


def _nl_cache_get(key):
    now = time.time()
    with _NL_LOCK:
        hit = _NL_LLM_CACHE.get(key)
        if not hit:
            return None
        ts, draft = hit
        if now - ts > NL_LLM_CACHE_TTL:
            _NL_LLM_CACHE.pop(key, None)
            return None
        return draft


def _nl_cache_put(key, draft):
    with _NL_LOCK:
        if len(_NL_LLM_CACHE) >= NL_LLM_CACHE_MAX:
            oldest = min(_NL_LLM_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _NL_LLM_CACHE.pop(oldest, None)
        _NL_LLM_CACHE[key] = (time.time(), draft)


def _extract_json_object(raw):
    """从模型回复里抠出 JSON 对象。失败返回 None——绝不退化成返回原文。"""
    s = str(raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.M).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def _nl_llm_models(env):
    """模型回退链：LLM_MODELS 逗号分隔取第一个为主；未配置时退回 LLM_MODEL 单值；都没有就用默认。"""
    raw = _nl_kv(env, "LLM_MODELS")
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    single = _nl_kv(env, "LLM_MODEL")
    return [single] if single else [NL_LLM_DEFAULT_MODEL]


def _nl_extract_text(data):
    """取 OpenAI 兼容响应的文本：choices[0].message.content；兼容历史 Anthropic 块数组。

    拿不到就返回空串——绝不退化成「把整个响应当结论」（sandbox_agent 的旧坑）。
    """
    data = data or {}
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        c = msg.get("content")
        if isinstance(c, str) and c:
            return c[:NL_LLM_RAW_MAX_CHARS]
    blocks = data.get("content") or []
    if isinstance(blocks, dict):
        blocks = [blocks]
    out = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") in (None, "text"):
            out.append(str(b.get("text") or ""))
    t = "".join(out)
    return t[:NL_LLM_RAW_MAX_CHARS] if len(t) > NL_LLM_RAW_MAX_CHARS else t


def _nl_finish_reason(data):
    """取响应的结束原因：OpenAI choices[0].finish_reason / Anthropic stop_reason。

    「length / max_tokens」= 输出被 max_tokens 掐断——调用方（decompose）据此在
    JSON 解析失败时给出「模型输出被截断」的明确降级原因，而不是笼统的「无法解析」。
    """
    data = data or {}
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        fr = (choices[0] or {}).get("finish_reason")
        if fr:
            return str(fr)
    sr = data.get("stop_reason")
    return str(sr) if sr else None


def _nl_llm_request(
    model, text, env, read_timeout, build_prompt, max_tokens=NL_LLM_MAX_TOKENS, meta=None
):
    """单个模型的一次请求。

    走 **OpenAI 兼容协议**（POST {base}/chat/completions + Authorization: Bearer）——Mistral
    官方仅提供该兼容端点（https://api.mistral.ai/v1/chat/completions）。

    build_prompt(text) -> (system, user) 由调用方注入：nl_parse 传自己的规则抽取模板，
    ai_decompose 传自己的 step/condition 推理模板（复用本请求/回退/预算链路）。
    max_tokens 由调用方按输出体量传（nl_parse 小抽取 300 / decompose 1000）。
    meta 传 dict 时，把本次响应的 finish_reason 写回（截断检测用）。
    """
    env = env or {}
    key = _nl_kv(env, "LLM_KEY")
    base = _nl_kv(env, "LLM_BASE_URL", default=NL_LLM_DEFAULT_BASE)
    sys_p, usr_p = build_prompt(text)
    messages = []
    if sys_p:
        messages.append({"role": "system", "content": sys_p})
    messages.append({"role": "user", "content": usr_p})
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            # 统一带真实标识，避免网关因默认 UA(Python-urllib/x.y) 拦截。
            "User-Agent": "spacefin-nl/1.0",
        },
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=read_timeout) as resp:
        raw_body = resp.read()
        if len(raw_body) > NL_LLM_RESP_MAX_BYTES:
            raise ValueError("llm response body too large")
        data = json.loads(raw_body.decode("utf-8", "replace"))
    if meta is not None:
        # 仅成功路径写 meta（异常时由调用方回退，meta 留空即「无模型信息」）：
        # 模型名与耗时随响应上抛，供 decompose/nl_parse 前端展示「由哪个模型生成」。
        meta["finish_reason"] = _nl_finish_reason(data)
        meta["model"] = model
        meta["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return _nl_extract_text(data)


def _is_llm_timeout(exc):
    """判断异常是否属于读超时（urllib 超时会以 TimeoutError 出现，URLError 则把超时藏在 .reason）。"""
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, TimeoutError)


def _nl_llm_call(text, env, build_prompt, max_tokens=NL_LLM_MAX_TOKENS, meta=None):
    """按模型链依次尝试，第一个成功的返回；全失败抛最后一个异常。

    总预算 NL_LLM_TOTAL_BUDGET 秒：同步 handler 里不能因为回退链把请求拖到几十秒。
    同一模型对「读超时」自动补 1 次重试：推理模型偶发慢响应（首 token 超 25s），
    首次超时并不代表模型不可用，重试一次可抵消间歇性慢，避免误报「无法连接」。
    max_tokens / meta 原样透传给每次 _nl_llm_request（meta 收最后一个成功响应的
    finish_reason；全失败时抛异常，meta 不会被写）。
    """
    last = None
    deadline = time.time() + NL_LLM_TOTAL_BUDGET
    for model in _nl_llm_models(env):
        # 每个模型最多尝试 N_ATTEMPTS 次；仅读超时触发重试，其余错误直接换模型。
        attempt = 0
        while attempt < NL_LLM_ATTEMPTS:
            attempt += 1
            left = deadline - time.time()
            if left <= 0:
                break
            try:
                return _nl_llm_request(
                    model,
                    text,
                    env,
                    min(NL_LLM_READ_TIMEOUT, left),
                    build_prompt,
                    max_tokens=max_tokens,
                    meta=meta,
                )
            except Exception as exc:  # noqa: BLE001 - 换下一个模型；全挂由调用方回退
                last = exc
                _NL_LOG.warning(
                    "llm 模型 %s 调用失败（第 %d 次）：%s", model, attempt, type(exc).__name__
                )
                if (
                    not (isinstance(exc, TimeoutError) or _is_llm_timeout(exc))
                    or attempt >= NL_LLM_ATTEMPTS
                ):
                    break
    raise last


def _log_nl(out, text, user, stage, cost=None):
    """日志只留指纹与耗时——输入原文会进不可信模型的留档体系，不能落在这里。"""
    _NL_LOG.info(
        "nl_parse stage=%s mode=%s user=%s len=%d sha=%s%s",
        stage,
        out.get("nl_mode"),
        user,
        len(text or ""),
        hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8],
        "" if cost is None else f" cost={cost:.2f}s",
    )
