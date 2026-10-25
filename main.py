"""文枢入口：消息路由、生命周期和 Plugin Pages API。"""

import asyncio
import copy
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .services.engine import Engine, Run
from .services.host import Host, message_text
from .services.records import Records
from .services.settings import (
    WenshuError,
    candidates,
    resolve,
    revision,
    text,
    validate,
)

PLUGIN_NAME = "astrbot_plugin_wenshu"


class Wenshu(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.host = Host(context)
        self.engine = Engine(context, self.host)
        self.records = Records(StarTools.get_data_dir(PLUGIN_NAME))
        self.save_lock = asyncio.Lock()
        self.active = set()
        self.tasks = set()
        self.closed = False
        self.api_handlers = []
        self.test_jobs = {}
        self.job_expiry = {}

    async def initialize(self):
        await self.records.load()
        for endpoint, methods, handler in (
            ("settings", ["GET", "POST"], self.settings_api),
            ("resources", ["GET"], self.resources_api),
            ("records", ["GET"], self.records_api),
            ("test", ["POST"], self.test_api),
            ("test-result", ["GET"], self.test_result_api),
            ("examples", ["GET"], self.examples_api),
        ):

            async def guarded(handler=handler):
                if not request.username:
                    return error_response("请先登录 AstrBot Dashboard", status_code=401)
                if self.closed:
                    return error_response("插件已停用", status_code=503)
                try:
                    return await handler()
                except WenshuError as exc:
                    return error_response(str(exc))
                except Exception as exc:  # noqa: BLE001 -- 宿主边界隔离异常，原始载荷不回显
                    # provider 原始异常可能带请求或凭证，仅记录类型。
                    logger.error("文枢：管理 API 失败，类型=%s", type(exc).__name__)
                    return error_response(
                        "操作失败，请检查配置和宿主日志", status_code=500
                    )

            self.api_handlers.append(guarded)
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}", guarded, methods, f"文枢 {endpoint}"
            )
        logger.info("文枢：已加载，预设仅用于独立请求")

    def snapshot(self):
        return validate(dict(self.config))

    def acquire(self, key, cfg):
        if self.closed:
            raise WenshuError("插件正在停用")
        if key in self.active:
            raise WenshuError("正在生成，请稍候")
        if len(self.active) >= cfg["max_concurrency"]:
            raise WenshuError("文枢当前繁忙，请稍后重试")
        self.active.add(key)
        self.tasks.add(asyncio.current_task())

    def release(self, key):
        self.active.discard(key)
        self.tasks.discard(asyncio.current_task())

    async def record(self, run, status, started, cfg, *, test=False, reason=""):
        await self.records.add(
            {
                "id": uuid.uuid4().hex,
                "time": datetime.now(UTC).isoformat(),
                "preset_id": run.selected["id"] if run.selected else "",
                "preset_name": run.selected["name"] if run.selected else "",
                "detect_provider": run.detect_provider,
                "generate_provider": run.generate_provider,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "tool_calls": run.tool_calls,
                "search_calls": run.search_calls,
                "search_successes": run.search_successes,
                "kb_successes": run.kb_successes,
                "usage": run.usage or None,
                "usage_complete": run.usage_calls == run.llm_calls
                and bool(run.llm_calls),
                "status": status,
                "test": test,
                "reason": reason,
            },
            cfg["record_limit"],
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def route(self, event: AstrMessageEvent):
        """检测候选并独立生成文案；普通消息保持原有流程。"""
        if (
            self.closed
            or event.get_platform_name() != "aiocqhttp"
            or event.is_stopped()
        ):
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        cfg = None
        run = Run()
        started = time.monotonic()
        key = (event.unified_msg_origin, str(event.get_sender_id()))
        acquired = False
        send_attempted = False
        status, reason = "failed", ""
        try:
            cfg = self.snapshot()
            message = message_text(event)
            matches = await asyncio.to_thread(
                candidates,
                cfg,
                message,
                is_group=bool(event.get_group_id()),
                awake=event.is_at_or_wake_command,
            )
            if not matches:
                return
            self.acquire(key, cfg)
            acquired = True
            async with asyncio.timeout(cfg["task_timeout"]):
                history = (
                    await self.host.history(event, cfg)
                    if matches[0]["mode"] != "direct"
                    else []
                )
                decision = await self.engine.decide(
                    cfg, matches, message, history, str(event.get_sender_id()), run
                )
                if decision["status"] == "no_match":
                    status = "no_match"
                    return
                if decision["status"] == "needs_topic":
                    body = "请补充这次文案的主题或对象，并带上触发词重新发送。"
                    status = "needs_topic"
                else:
                    body = await self.engine.respond(
                        event, cfg, decision, message, history, run
                    )
                    status = "degraded" if run.degraded else "success"
                # 检测和生成期间保持事件可继续；仅在确定要发送时停止。
                event.stop_event()
                send_attempted = True
                await event.send(event.plain_result(body))
        except asyncio.CancelledError:
            if acquired:
                event.stop_event()
            raise
        except Exception as exc:  # noqa: BLE001 -- 宿主边界隔离异常，原始载荷不回显
            reason = (
                str(exc)
                if isinstance(exc, WenshuError)
                else (
                    "任务超时"
                    if isinstance(exc, TimeoutError)
                    else "执行失败，请检查宿主日志"
                )
            )
            logger.warning("文枢：任务未正常完成，类型=%s", type(exc).__name__)
            if send_attempted:
                status, reason = "send_unknown", "发送结果未知，未重试，也未交回主聊天"
                return
            if cfg is None:
                return  # 配置无效时不误拦所有普通消息，管理页可查看校验错误。
            policy = (
                resolve(cfg, run.selected)["failure_policy"]
                if run.selected
                else cfg["failure_policy"]
            )
            if policy == "passthrough":
                status = "passthrough"
                return
            event.stop_event()
            await event.send(event.plain_result("文枢：" + reason))
        finally:
            if acquired:
                self.release(key)
                await self.record(run, status, started, cfg, reason=reason)

    async def settings_api(self):
        if request.method == "GET":
            cfg = self.snapshot()
            return json_response({"config": cfg, "revision": revision(cfg)})
        payload = await request.json()
        if not isinstance(payload, dict):
            raise WenshuError("请求必须是 JSON 对象")
        cfg = validate(payload.get("config"))
        await self.host.validate_references(cfg)
        async with self.save_lock:
            current = self.snapshot()
            if payload.get("revision") != revision(current):
                return error_response(
                    "配置已被其他页面修改，请保留草稿并重新加载后合并", status_code=409
                )
            old = copy.deepcopy(dict(self.config))
            try:
                await asyncio.to_thread(self.config.save_config, cfg)
            except Exception:  # noqa: BLE001 -- 保存失败必须回滚内存快照
                self.config.clear()
                self.config.update(old)
                raise WenshuError("保存失败，旧配置和当前页面草稿均保留") from None
            return json_response({"config": cfg, "revision": revision(cfg)})

    async def resources_api(self):
        result = await self.host.resources()
        umo = request.query.get("umo", "")
        if umo:
            text(umo, "会话标识", 300)
            result["search"] = self.host.search_status(self.host.profile(umo))
        return json_response(result)

    async def records_api(self):
        return json_response({"items": copy.deepcopy(self.records.items)})

    async def examples_api(self):
        data = await asyncio.to_thread(
            (Path(__file__).parent / "examples.json").read_text, encoding="utf-8-sig"
        )
        return json_response(json.loads(data))

    async def test_api(self):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise WenshuError("请求必须是 JSON 对象")
        cfg = validate(payload.get("config"))
        message = text(payload.get("message"), "模拟消息", 2000)
        mode = payload.get("mode", "rules")
        if mode not in ("rules", "detect", "full"):
            raise WenshuError("测试模式无效")
        if (
            type(payload.get("is_group", True)) is not bool
            or type(payload.get("awake", True)) is not bool
        ):
            raise WenshuError("群聊与唤醒标记必须是布尔值")
        matches = await asyncio.to_thread(
            candidates,
            cfg,
            message,
            is_group=payload.get("is_group", True),
            awake=payload.get("awake", True),
        )
        result = {
            "candidates": [
                {"id": p["id"], "name": p["name"], "patterns": p["patterns"]}
                for p in matches
            ],
            "draft": True,
            "history_source": "手工输入",
            "body": "",
        }
        if mode == "rules" or not matches:
            return json_response(result)
        needs_detection = matches[0]["mode"] != "direct"
        if needs_detection and payload.get("confirm_cost") is not True:
            raise WenshuError("付费测试前必须确认模型调用费用")
        await self.host.validate_references(cfg)
        history = payload.get("history", [])
        if not isinstance(history, list) or len(history) > cfg["history_messages"]:
            raise WenshuError("手工历史数量超限")
        total = 0
        for row in history:
            if not isinstance(row, dict):
                raise WenshuError("历史应为包含 sender、text 的对象列表")
            text(row.get("sender"), "发送者", 200)
            total += len(text(row.get("text"), "历史正文", cfg["history_chars"]))
        if total > cfg["history_chars"]:
            raise WenshuError("手工历史总长度超限")
        sender = text(payload.get("sender", "test-user"), "模拟用户", 200)
        umo = text(payload.get("umo", ""), "会话标识", 300)
        may_generate = needs_detection and any(p["mode"] == "generate" for p in matches)
        if mode == "full" and may_generate and not umo:
            raise WenshuError(
                "完整试运行需要实际会话标识，以解析其配置和人格；可用 /sid 查看"
            )
        # 仅提供读取配置和身份所需的对象；不存在发送方法，不可能向 QQ 发送。
        event = SimpleNamespace(
            unified_msg_origin=umo,
            get_sender_id=lambda: sender,
            get_platform_name=lambda: "aiocqhttp",
        )
        owner = request.username
        key = ("web-test", owner)
        admission = cfg | {
            "max_concurrency": min(
                cfg["max_concurrency"], self.snapshot()["max_concurrency"]
            )
        }
        self.acquire(key, admission)
        job_id = uuid.uuid4().hex
        job = {"owner": owner, "status": "running"}
        self.test_jobs[job_id] = job

        # Plugin Pages 网关限时 60 秒；耗时测试在独立任务中执行，正文仅暂存内存。
        async def work():
            run, started, status, reason = Run(), time.monotonic(), "failed", ""
            try:
                async with asyncio.timeout(cfg["task_timeout"]):
                    decision = await self.engine.decide(
                        cfg, matches, message, history, sender, run
                    )
                    result["decision"] = decision
                    status = decision["status"]
                    if mode == "full" and status == "matched":
                        status = "failed"
                        result["body"] = await self.engine.respond(
                            event, cfg, decision, message, history, run
                        )
                        status = "degraded" if run.degraded else "success"
                    result.update(
                        {
                            "status": status,
                            "stages": run.stages,
                            "sources": run.sources,
                            "detect_provider": run.detect_provider,
                            "generate_provider": run.generate_provider,
                            "tool_calls": run.tool_calls,
                            "usage": run.usage or None,
                            "usage_complete": run.usage_calls == run.llm_calls,
                        }
                    )
                    job["result"] = result
                    job["status"] = "done"
            except asyncio.CancelledError:
                status, reason = "cancelled", "插件停用，试运行已取消"
                job.update(status="failed", error=reason)
                raise
            except Exception as exc:  # noqa: BLE001 -- 后台任务边界，禁止回传原始凭证或请求
                reason = (
                    str(exc)
                    if isinstance(exc, WenshuError)
                    else (
                        "试运行超时"
                        if isinstance(exc, TimeoutError)
                        else "试运行失败，请检查宿主日志"
                    )
                )
                job.update(status="failed", error=reason)
                logger.warning("文枢：试运行失败，类型=%s", type(exc).__name__)
            finally:
                self.release(key)
                await self.record(run, status, started, cfg, test=True, reason=reason)
                if not self.closed:
                    self.job_expiry[job_id] = asyncio.get_running_loop().call_later(
                        300, self.expire_job, job_id
                    )

        self.tasks.discard(asyncio.current_task())
        job_task = asyncio.create_task(work(), name=f"wenshu-test-{job_id}")
        self.tasks.add(job_task)
        return json_response({"job_id": job_id})

    def expire_job(self, job_id):
        self.test_jobs.pop(job_id, None)
        self.job_expiry.pop(job_id, None)

    async def test_result_api(self):
        job_id = request.query.get("id", "")
        job = self.test_jobs.get(job_id)
        if not job or job["owner"] != request.username:
            return error_response(
                "试运行不存在或结果已过期，请重新测试", status_code=404
            )
        return json_response(
            {key: value for key, value in job.items() if key != "owner"}
        )

    async def terminate(self):
        self.closed = True
        tasks = list(self.tasks - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()
        for handle in self.job_expiry.values():
            handle.cancel()
        self.job_expiry.clear()
        self.test_jobs.clear()
        # 仅清理仍属于本实例的路由，防止误删重载后新实例的注册。
        self.context.registered_web_apis[:] = [
            item
            for item in self.context.registered_web_apis
            if item[1] not in self.api_handlers
        ]
