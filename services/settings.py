"""配置快照、覆盖规则和有超时的正则匹配。"""

import copy
import hashlib
import json
import time
from functools import lru_cache

import regex

DEFAULTS = {
    "enabled": True,
    "detect_provider": "",
    "generate_provider": "",
    "search_mode": "required",
    "failure_policy": "stop",
    "persona_mode": "current",
    "persona_id": "",
    "kb_ids": [],
    "group_scope": "wake",
    "max_concurrency": 2,
    "history_messages": 12,
    "history_chars": 8000,
    "max_tool_calls": 8,
    "task_timeout": 180,
    "record_limit": 200,
    "presets": [],
}
ENUMS = {
    "search_mode": ("required", "auto", "off"),
    "failure_policy": ("stop", "grounded", "passthrough"),
    "persona_mode": ("current", "specified", "none"),
    "group_scope": ("wake", "all"),
}
OVERRIDES = (
    "detect_provider",
    "generate_provider",
    "search_mode",
    "failure_policy",
    "persona_mode",
    "persona_id",
    "kb_ids",
    "group_scope",
)
LIMITS = {
    "max_concurrency": (1, 16),
    "history_messages": (0, 50),
    "history_chars": (0, 30000),
    "max_tool_calls": (1, 30),
    "task_timeout": (15, 600),
    "record_limit": (0, 1000),
}


class WenshuError(Exception):
    """可以安全展示的错误，不携带外部原始异常。"""


def text(value, label, maximum=60000):
    if not isinstance(value, str) or len(value) > maximum:
        raise WenshuError(f"{label} 必须是长度不超过 {maximum} 的文本")
    return value


def strings(value, label, count=100):
    if not isinstance(value, list) or len(value) > count:
        raise WenshuError(f"{label} 必须是最多 {count} 项的列表")
    for item in value:
        if not text(item, label, 2000).strip():
            raise WenshuError(f"{label} 不能含空项")
    return value


@lru_cache(maxsize=512)
def compile_pattern(pattern):
    try:
        return regex.compile(pattern)
    except regex.error:
        raise WenshuError("正则无法编译，请检查规则") from None


def resolve(cfg, preset):
    return {
        key: copy.deepcopy(preset["overrides"].get(key, cfg[key])) for key in OVERRIDES
    }


def validate(raw):
    """校验独立快照，完整保留提示词和范文。"""
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise WenshuError("配置含未知字段")
    cfg = copy.deepcopy(DEFAULTS | raw)
    if type(cfg["enabled"]) is not bool:
        raise WenshuError("enabled 必须是布尔值")
    for key, (low, high) in LIMITS.items():
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise WenshuError(f"{key} 必须是 {low}～{high} 的整数")
    for key, choices in ENUMS.items():
        if cfg[key] not in choices:
            raise WenshuError(f"{key} 选项无效")
    for key in ("detect_provider", "generate_provider", "persona_id"):
        text(cfg[key], key, 200)
    strings(cfg["kb_ids"], "知识库")
    if not isinstance(cfg["presets"], list) or len(cfg["presets"]) > 100:
        raise WenshuError("最多配置 100 个预设")
    seen = set()
    fields = {
        "__template_key",
        "id",
        "name",
        "description",
        "enabled",
        "priority",
        "mode",
        "reply",
        "keywords",
        "patterns",
        "intent",
        "prompt",
        "example",
        "opening",
        "paragraphs",
        "overrides",
    }
    for preset in cfg["presets"]:
        if not isinstance(preset, dict) or set(preset) - fields:
            raise WenshuError("预设含未知字段")
        for key, default in {
            "description": "",
            "enabled": True,
            "priority": 0,
            "mode": "generate",
            "reply": "",
            "keywords": [],
            "patterns": [],
            "intent": "",
            "prompt": "",
            "example": "",
            "opening": "",
            "paragraphs": 0,
            "overrides": {},
        }.items():
            preset.setdefault(key, copy.deepcopy(default))
        for key in (
            "id",
            "name",
            "description",
            "intent",
            "prompt",
            "example",
            "reply",
            "opening",
        ):
            text(preset.get(key), key)
        if (
            not regex.fullmatch(r"[a-zA-Z0-9_-]{1,80}", preset["id"])
            or preset["id"] in seen
        ):
            raise WenshuError("预设 ID 必须唯一，且仅含字母、数字、下划线或连字符")
        seen.add(preset["id"])
        if not preset["name"].strip():
            raise WenshuError("预设名称不能为空")
        if type(preset["enabled"]) is not bool or type(preset["priority"]) is not int:
            raise WenshuError("预设启停或优先级类型错误")
        if type(preset["paragraphs"]) is not int or not 0 <= preset["paragraphs"] <= 30:
            raise WenshuError("段落数必须是 0～30，0 表示不检查")
        strings(preset["keywords"], "关键词", 30)
        strings(preset["patterns"], "正则", 20)
        for pattern in preset["patterns"]:
            compile_pattern(pattern)
        overrides = preset["overrides"]
        if not isinstance(overrides, dict) or set(overrides) - set(OVERRIDES):
            raise WenshuError("预设覆盖字段无效")
        for key, value in overrides.items():
            if key in ENUMS:
                if value not in ENUMS[key]:
                    raise WenshuError(f"覆盖项 {key} 无效")
            elif key == "kb_ids":
                strings(value, "覆盖知识库")
            else:
                text(value, key, 200)
                if key.endswith("provider") and not value.strip():
                    raise WenshuError("覆盖 provider 不能为空；请删除覆盖以继承")
        effective = resolve(cfg, preset)
        if effective["persona_mode"] == "specified" and not effective["persona_id"]:
            raise WenshuError("指定人格时必须选择人格")
        if preset["enabled"] and not (preset["keywords"] or preset["patterns"]):
            raise WenshuError("启用的预设至少需要一个触发规则")
        if preset["mode"] not in ("generate", "direct", "detect_only"):
            raise WenshuError("预设执行模式无效")
        if len(preset["reply"]) > 30000:
            raise WenshuError("固定文案不能超过 30000 字符")
        if preset["enabled"]:
            if preset["mode"] != "direct" and not preset["intent"].strip():
                raise WenshuError("需要判定意图的预设必须填写意图说明")
            if preset["mode"] == "generate" and not preset["prompt"].strip():
                raise WenshuError("检索生成模式必须填写生成要求")
            if preset["mode"] != "generate" and not preset["reply"].strip():
                raise WenshuError("固定文案模式必须填写要原样发送的文案")
    if cfg["persona_mode"] == "specified" and not cfg["persona_id"]:
        raise WenshuError("全局指定人格时必须选择人格")
    return cfg


def candidates(cfg, message, *, is_group, awake):
    if not cfg["enabled"] or not message or len(message) > 2000:
        return []
    matches = []
    deadline = time.monotonic() + 0.1

    def match_pattern(pattern):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return compile_pattern(pattern).search(message, timeout=min(0.015, remaining))

    for preset in cfg["presets"]:
        if not preset["enabled"]:
            continue
        if is_group and resolve(cfg, preset)["group_scope"] == "wake" and not awake:
            continue
        matched = any(word in message for word in preset["keywords"])
        try:
            matched = matched or any(
                match_pattern(pattern) for pattern in preset["patterns"]
            )
        except TimeoutError:
            raise WenshuError(f"预设 {preset['name']} 正则超时，请简化规则") from None
        if matched:
            matches.append(preset)
    return sorted(matches, key=lambda item: -item["priority"])


def revision(cfg):
    return hashlib.sha256(
        json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
