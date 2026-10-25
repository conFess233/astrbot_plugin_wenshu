"""读取会话有效配置，复用宿主搜索和知识库，不写主聊天。"""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import logger, sp
from astrbot.core.tools import web_search_tools as web

from .settings import WenshuError

SEARCH_CLASSES = {
    "tavily": ("TavilyWebSearchTool", "TavilyExtractWebPageTool"),
    "bocha": ("BochaWebSearchTool",),
    "brave": ("BraveWebSearchTool",),
    "baidu_ai_search": ("BaiduWebSearchTool",),
    "firecrawl": ("FirecrawlWebSearchTool", "FirecrawlExtractWebPageTool"),
    "exa": ("ExaWebSearchTool", "ExaGetContentsTool"),
    "anysearch": ("AnySearchWebSearchTool",),
}


def message_text(event):
    """宿主唤醒阶段已去前缀；保留其他人的 @ 语义。"""
    parts = []
    for segment in event.get_messages():
        kind = type(segment).__name__
        if kind == "Plain":
            parts.append(segment.text)
        elif kind == "At" and str(segment.qq) != str(event.get_self_id()):
            parts.append(f" @用户({segment.qq}) ")
        elif kind == "AtAll":
            parts.append(" @全体成员 ")
    value = "".join(parts).strip()
    # message_str 是宿主实际处理后的正文；无其他 @ 时优先沿用其唤醒前缀处理。
    if not any(
        type(s).__name__ in ("AtAll", "At")
        and str(getattr(s, "qq", "all")) != str(event.get_self_id())
        for s in event.get_messages()
    ):
        value = event.message_str.strip()
    return value


class Host:
    def __init__(self, context):
        self.context = context

    async def resources(self):
        providers = []
        for provider in self.context.get_all_providers():
            meta = provider.meta()
            providers.append({"id": meta.id, "model": meta.model})
        personas = [
            {"id": p["name"], "name": p["name"]}
            for p in self.context.persona_manager.personas_v3
        ]
        kbs = [
            {"id": kb.kb_id, "name": kb.kb_name}
            for kb in await self.context.kb_manager.list_kbs()
        ]
        return {"providers": providers, "personas": personas, "knowledge_bases": kbs}

    async def validate_references(self, cfg):
        from .settings import resolve

        active = [p for p in cfg["presets"] if cfg["enabled"] and p["enabled"]]
        relevant = [p for p in active if p["mode"] != "direct"]
        if not relevant:
            return
        resources = await self.resources()
        providers = {item["id"] for item in resources["providers"]}
        personas = {item["id"] for item in resources["personas"]}
        kbs = {item["id"] for item in resources["knowledge_bases"]}
        for preset in relevant:
            effective = resolve(cfg, preset)
            fields = ["detect_provider"]
            if preset["mode"] == "generate":
                fields.append("generate_provider")
            for field in fields:
                if not effective[field] or effective[field] not in providers:
                    raise WenshuError(f"预设 {preset['name']} 需要有效的 {field}")
            if preset["mode"] == "generate":
                if (
                    effective["persona_mode"] == "specified"
                    and effective["persona_id"] not in personas
                ):
                    raise WenshuError("指定人格不存在")
                if set(effective["kb_ids"]) - kbs:
                    raise WenshuError("引用的知识库不存在或未加载")

    def profile(self, umo):
        source = self.context.get_config(umo=umo)
        profile = copy.deepcopy(dict(source))
        profile["_wenshu_profile_name"] = Path(
            getattr(source, "config_path", "会话有效配置")
        ).stem
        settings = profile.get("provider_settings", {})
        for provider in ("tavily", "bocha", "brave", "firecrawl", "exa", "anysearch"):
            key = f"websearch_{provider}_key"
            if isinstance(settings.get(key), str):
                settings[key] = [settings[key]] if settings[key] else []
        return profile

    async def conversation(self, umo):
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(umo)
        return await manager.get_conversation(umo, cid) if cid else None

    async def history(self, event, cfg):
        limit, budget = cfg["history_messages"], cfg["history_chars"]
        if not limit or not budget:
            return []
        rows = []
        try:
            if event.get_group_id():
                # OneBot 原始消息保留发送者；不猜测共享主聊天历史中的身份。
                async with asyncio.timeout(8):
                    result = await event.bot.call_action(
                        "get_group_msg_history",
                        group_id=int(event.get_group_id()),
                        count=limit + 1,
                    )
                messages = result.get("messages", [])
                current = str(getattr(event.message_obj, "message_id", ""))
                for row in messages[-(limit + 1) :]:
                    if str(row.get("message_id")) == current:
                        break
                    parts = row.get("message", [])
                    if not isinstance(parts, list):
                        continue
                    value = "".join(
                        s.get("data", {}).get("text", "")
                        for s in parts
                        if s.get("type") == "text"
                    )
                    sender = str(
                        row.get("user_id", row.get("sender", {}).get("user_id", ""))
                    )
                    rows.append({"sender": sender, "text": value})
            else:
                conv = await self.conversation(event.unified_msg_origin)
                history = json.loads(conv.history or "[]") if conv else []
                for row in history[-limit:]:
                    content = row.get("content", "")
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "")
                            for p in content
                            if p.get("type") == "text"
                        )
                    if row.get("role") in ("user", "assistant") and isinstance(
                        content, str
                    ):
                        rows.append(
                            {
                                "sender": str(event.get_sender_id())
                                if row["role"] == "user"
                                else "bot",
                                "text": content,
                            }
                        )
        except Exception as exc:  # noqa: BLE001 -- 宿主边界隔离异常，原始载荷不回显
            logger.warning(
                "文枢：近期历史不可用，类型=%s；继续使用当前消息", type(exc).__name__
            )
        bounded = []
        for row in reversed(rows[-limit:]):
            value = row["text"][-budget:]
            if value:
                bounded.append({"sender": row["sender"], "text": value})
                budget -= len(value)
            if budget <= 0:
                break
        return list(reversed(bounded))

    async def persona(self, event, options, profile):
        # 当前人格的工具限制始终作为权限上限，不因选择其他写作人格而绕过。
        conv = await self.conversation(event.unified_msg_origin)
        _, current, _, _ = await self.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=getattr(conv, "persona_id", None),
            platform_name=event.get_platform_name(),
            provider_settings=profile.get("provider_settings", {}),
        )
        selected = current
        if options["persona_mode"] == "none":
            selected = None
        elif options["persona_mode"] == "specified":
            selected = next(
                (
                    p
                    for p in self.context.persona_manager.personas_v3
                    if p["name"] == options["persona_id"]
                ),
                None,
            )
            if selected is None:
                raise WenshuError("指定人格不存在，请重新选择")
        allowed = None
        for persona in (current, selected):
            tools = persona.get("tools") if persona else None
            if tools is not None:
                allowed = set(tools) if allowed is None else allowed & set(tools)
        return (selected.get("prompt", "") if selected else ""), allowed

    async def tools(self, event, options, profile, allowed):
        tools = {}
        if options["search_mode"] != "off" and profile.get("provider_settings", {}).get(
            "web_search", False
        ):
            provider = profile["provider_settings"].get("websearch_provider", "tavily")
            for class_name in SEARCH_CLASSES.get(provider, ()):
                cls = getattr(web, class_name, None)
                if cls:
                    tool = self.context.get_llm_tool_manager().get_builtin_tool(cls)
                    if (
                        tool
                        and getattr(tool, "active", True)
                        and (allowed is None or tool.name in allowed)
                    ):
                        tools[tool.name] = tool
        selected_ids = options["kb_ids"]
        session = (
            await sp.session_get(event.unified_msg_origin, "kb_config", default={})
            or {}
        )
        if "kb_ids" in session:
            selected_ids = [kb for kb in selected_ids if kb in session["kb_ids"]]
        kb_names = []
        for kb_id in selected_ids:
            kb = await self.context.kb_manager.get_kb(kb_id)
            if kb is None:
                raise WenshuError("选中的知识库未加载，请检查配置")
            kb_names.append(kb.kb.kb_name)
        if kb_names and (allowed is None or "astr_kb_search" in allowed):
            from astrbot.api import FunctionTool

            tools["astr_kb_search"] = FunctionTool(
                name="astr_kb_search",
                description="检索已授权知识库中的事实资料。",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
        # 内置工具仅访问这个配置快照；不调用会保存宿主配置的 legacy normalize helper。
        readonly = SimpleNamespace(get_config=lambda **_: profile)
        wrapper = SimpleNamespace(
            context=SimpleNamespace(context=readonly, event=event)
        )
        return tools, wrapper, kb_names

    async def call_tool(self, name, tool, arguments, wrapper, kb_names, profile):
        if name == "astr_kb_search":
            result = await self.context.kb_manager.retrieve(
                query=arguments["query"],
                kb_names=kb_names,
                top_k_fusion=profile.get("kb_fusion_top_k", 20),
                top_m_final=profile.get("kb_final_top_k", 5),
            )
            return result.get("context_text", "") if result else ""
        return await tool.call(wrapper, **arguments)

    def search_status(self, profile):
        settings = profile.get("provider_settings", {})
        return {
            "enabled": bool(settings.get("web_search", False)),
            "provider": settings.get("websearch_provider", "tavily"),
            "profile_id": str(profile.get("_wenshu_profile_name", "会话有效配置")),
        }
