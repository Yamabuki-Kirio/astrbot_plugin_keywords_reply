from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
import os
import asyncio
import time

from .modules.command_triggered import CommandTriggeredModule
from .modules.auto_detect import AutoDetectModule
from .modules.utils import PluginUtils
from .webui.api import KeywordsReplyWebUIApi
from .webui.payloads import get_effective_recall_delay


class KeywordsReplyPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_keywords_reply")
        self.image_dir = os.path.join(self.data_dir, "images")
        self.record_dir = os.path.join(self.data_dir, "records")
        self.video_dir = os.path.join(self.data_dir, "videos")
        self.data_file = os.path.join(self.data_dir, "keywords.json")

        os.makedirs(self.image_dir, exist_ok=True)
        os.makedirs(self.record_dir, exist_ok=True)
        os.makedirs(self.video_dir, exist_ok=True)

        self._save_lock = asyncio.Lock()
        self._regex_cache = {}
        self._capture_sessions = {}
        self._capture_timeout = 300
        self.utils = PluginUtils(self)
        self.data = self.utils.normalize_data(self.utils.load_data())
        self.data_version = 1
        self.cmd_module = CommandTriggeredModule(self)
        self.detect_module = AutoDetectModule(self)
        self.webui_api = KeywordsReplyWebUIApi(self)
        self.webui_api.register()

    def _capture_session_key(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """按平台、会话和操作者隔离录入状态。"""
        return (
            str(event.get_platform_id() or event.get_platform_name() or ""),
            str(event.get_group_id() or "private"),
            str(event.get_sender_id() or ""),
        )

    def _get_capture_session(self, event: AstrMessageEvent):
        key = self._capture_session_key(event)
        session = self._capture_sessions.get(key)
        if session and time.monotonic() - session["updated_at"] > self._capture_timeout:
            self._capture_sessions.pop(key, None)
            if not self._capture_sessions:
                self.utils.cleanup_unused_media()
            return None
        return session

    @filter.command("添加")
    async def guided_add_cmd(self, event: AstrMessageEvent):
        """会话式添加顺序回复。用法: /添加 <检测词>"""
        if not self.utils.is_admin(event):
            denied = self.utils.permission_denied_result(event)
            if denied:
                yield denied
            return

        parts = event.message_str.strip().split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result("用法: /添加 <触发词>")
            return

        keyword = parts[1].strip()
        if keyword.startswith("/"):
            yield event.plain_result("触发词不能以 / 开头。")
            return

        existing = next(
            (
                cfg
                for cfg in self.data["auto_detect"]
                if not cfg.get("regex", False)
                and cfg.get("keyword", "").lower() == keyword.lower()
            ),
            None,
        )
        if existing:
            yield event.plain_result(
                f"检测词“{keyword}”已存在，请先删除原规则或换一个触发词。"
            )
            return

        if self._get_capture_session(event):
            yield event.plain_result("你已有一条规则正在录入，请先发送 /结束添加 或 /取消添加。")
            return

        key = self._capture_session_key(event)
        self._capture_sessions[key] = {
            "keyword": keyword,
            "entries": [],
            "group_id": str(event.get_group_id() or ""),
            "is_group": bool(event.get_group_id()),
            "updated_at": time.monotonic(),
        }
        event.stop_event()
        yield event.plain_result(
            "请发送要回复的内容，可连续发送文字、图片、语音或视频。\n"
            "完成后发送 /结束添加；放弃请发送 /取消添加。\n"
            "录入状态 5 分钟无操作会失效。"
        )

    @filter.command("结束添加")
    async def guided_add_finish_cmd(self, event: AstrMessageEvent):
        """结束会话式录入并保存顺序回复规则。"""
        key = self._capture_session_key(event)
        session = self._get_capture_session(event)
        if not session:
            yield event.plain_result("当前没有正在添加的规则，或录入状态已超时。")
            return
        if not session["entries"]:
            yield event.plain_result("还没有收到回复内容，请先发送内容再结束添加。")
            return

        keyword = session["keyword"]
        enabled = bool(session["is_group"])
        groups = [session["group_id"]] if session["is_group"] else []
        self.data["auto_detect"].append(
            {
                "keyword": keyword,
                "entries": session["entries"],
                "regex": False,
                "enabled": enabled,
                "mode": "whitelist",
                "groups": groups,
                "sequential": True,
            }
        )
        await self.utils.save_data_async()
        self._capture_sessions.pop(key, None)
        event.stop_event()
        yield event.plain_result(
            f"添加成功：检测词“{keyword}”，共 {len(session['entries'])} 条回复。"
        )

    @filter.command("取消添加")
    async def guided_add_cancel_cmd(self, event: AstrMessageEvent):
        """取消当前会话式录入。"""
        key = self._capture_session_key(event)
        session = self._capture_sessions.pop(key, None)
        if not session:
            yield event.plain_result("当前没有正在添加的规则。")
            return
        if not self._capture_sessions:
            self.utils.cleanup_unused_media()
        event.stop_event()
        yield event.plain_result(f"已取消添加检测词“{session['keyword']}”。")

    @filter.command("添加关键词")
    async def add_keyword_cmd(self, event: AstrMessageEvent):
        """添加新关键词和回复。用法: /添加关键词 <关键词> <回复内容>"""
        try:
            async for res in self.cmd_module.add_item(event):
                yield res
        except Exception as e:
            logger.error(f"添加关键词异常: {e}", exc_info=True)
            yield event.plain_result(f"添加关键词时发生错误: {e}")

    @filter.command("编辑关键词")
    async def edit_keyword_cmd(self, event: AstrMessageEvent):
        """修改关键词触发词。用法: /编辑关键词 <序号或旧关键词> <新关键词>"""
        try:
            async for res in self.cmd_module.edit_item(event):
                yield res
        except Exception as e:
            logger.error(f"编辑关键词异常: {e}", exc_info=True)
            yield event.plain_result(f"编辑关键词时发生错误: {e}")

    @filter.command("删除关键词")
    async def del_keyword_cmd(self, event: AstrMessageEvent):
        """删除关键词及其所有回复。用法: /删除关键词 <序号或关键词内容>"""
        try:
            async for res in self.cmd_module.del_items(event):
                yield res
        except Exception as e:
            logger.error(f"删除关键词异常: {e}", exc_info=True)
            yield event.plain_result(f"删除关键词时发生错误: {e}")

    @filter.command("启用关键词")
    async def enable_keyword_cmd(self, event: AstrMessageEvent):
        """在指定群聊或全局启用关键词。用法: /启用关键词 <序号或内容> [群号/全局]"""
        try:
            async for res in self.cmd_module.toggle_groups(event, True):
                yield res
        except Exception as e:
            logger.error(f"启用关键词异常: {e}", exc_info=True)
            yield event.plain_result(f"启用关键词时发生错误: {e}")

    @filter.command("禁用关键词")
    async def disable_keyword_cmd(self, event: AstrMessageEvent):
        """在指定群聊或全局禁用关键词。用法: /禁用关键词 <序号或内容> [群号/全局]"""
        try:
            async for res in self.cmd_module.toggle_groups(event, False):
                yield res
        except Exception as e:
            logger.error(f"禁用关键词异常: {e}", exc_info=True)
            yield event.plain_result(f"禁用关键词时发生错误: {e}")

    @filter.command("查看关键词列表", alias=["查看所有关键词"])
    async def list_keywords_cmd(self, event: AstrMessageEvent):
        """列出所有关键词。用法: /查看关键词列表"""
        try:
            async for res in self.cmd_module.list_items(event):
                yield res
        except Exception as e:
            logger.error(f"查看关键词列表异常: {e}", exc_info=True)
            yield event.plain_result(f"查看关键词列表时发生错误: {e}")

    @filter.command("查看关键词")
    async def view_keyword_cmd(self, event: AstrMessageEvent):
        """查看关键词详情。用法: /查看关键词 <序号或内容>"""
        try:
            async for res in self.cmd_module.view_item(event):
                yield res
        except Exception as e:
            logger.error(f"查看关键词异常: {e}", exc_info=True)
            yield event.plain_result(f"查看关键词详情时发生错误: {e}")

    @filter.command("查看关键词回复")
    async def view_keyword_reply_cmd(self, event: AstrMessageEvent):
        """查看指定关键词的某个回复。用法: /查看关键词回复 <关键词序号/内容> [回复序号]。语音/视频会另行发送。"""
        try:
            async for res in self.cmd_module.view_reply(event):
                yield res
        except Exception as e:
            logger.error(f"查看关键词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"查看回复时发生错误: {e}")

    @filter.command("添加关键词回复")
    async def add_keyword_reply_cmd(self, event: AstrMessageEvent):
        """为指定关键词添加新回复。用法: /添加关键词回复 <关键词序号/内容> [新回复内容]。可直接引用一条消息作为回复内容。"""
        try:
            async for res in self.cmd_module.add_reply(event):
                yield res
        except Exception as e:
            logger.error(f"添加关键词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"添加回复时发生错误: {e}")

    @filter.command("编辑关键词回复")
    async def edit_keyword_reply_cmd(self, event: AstrMessageEvent):
        """修改指定关键词的某个回复条目。用法: /编辑关键词回复 <关键词序号/内容> [回复序号] [新内容]。可直接引用一条消息作为新内容。"""
        try:
            async for res in self.cmd_module.edit_reply(event):
                yield res
        except Exception as e:
            logger.error(f"编辑关键词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"编辑回复时发生错误: {e}")

    @filter.command("删除关键词回复")
    async def del_keyword_reply_cmd(self, event: AstrMessageEvent):
        """删除指定关键词的某个回复条目。用法: /删除关键词回复 <关键词序号/内容> [回复序号]"""
        try:
            async for res in self.cmd_module.delete_reply(event):
                yield res
        except Exception as e:
            logger.error(f"删除关键词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"删除回复时发生错误: {e}")

    @filter.command("添加检测词")
    async def add_detect_cmd(self, event: AstrMessageEvent):
        """添加自动监听型检测词。用法: /添加检测词 [-r] <关键词> <回复内容> (-r 表示正则)"""
        try:
            async for res in self.detect_module.add_item(event):
                yield res
        except Exception as e:
            logger.error(f"添加检测词异常: {e}", exc_info=True)
            yield event.plain_result(f"添加检测词时发生错误: {e}")

    @filter.command("编辑检测词")
    async def edit_detect_cmd(self, event: AstrMessageEvent):
        """修改检测词触发词或正则开关。用法: /编辑检测词 [-r] <序号或内容> <新检测词>"""
        try:
            async for res in self.detect_module.edit_item(event):
                yield res
        except Exception as e:
            logger.error(f"编辑检测词异常: {e}", exc_info=True)
            yield event.plain_result(f"编辑检测词时发生错误: {e}")

    @filter.command("删除检测词")
    async def del_detect_cmd(self, event: AstrMessageEvent):
        """删除检测词及其所有回复。用法: /删除检测词 <序号或内容>"""
        try:
            async for res in self.detect_module.del_items(event):
                yield res
        except Exception as e:
            logger.error(f"删除检测词异常: {e}", exc_info=True)
            yield event.plain_result(f"删除检测词时发生错误: {e}")

    @filter.command("启用检测词")
    async def enable_detect_cmd(self, event: AstrMessageEvent):
        """在指定群聊或全局启用检测词。用法: /启用检测词 <序号或内容> [群号/全局]"""
        try:
            async for res in self.detect_module.toggle_groups(event, True):
                yield res
        except Exception as e:
            logger.error(f"启用检测词异常: {e}", exc_info=True)
            yield event.plain_result(f"启用检测词时发生错误: {e}")

    @filter.command("禁用检测词")
    async def disable_detect_cmd(self, event: AstrMessageEvent):
        """在指定群聊或全局禁用检测词。用法: /禁用检测词 <序号或内容> [群号/全局]"""
        try:
            async for res in self.detect_module.toggle_groups(event, False):
                yield res
        except Exception as e:
            logger.error(f"禁用检测词异常: {e}", exc_info=True)
            yield event.plain_result(f"禁用检测词时发生错误: {e}")

    @filter.command("查看检测词列表", alias=["查看所有检测词"])
    async def list_detects_cmd(self, event: AstrMessageEvent):
        """列出所有检测词。用法: /查看检测词列表"""
        try:
            async for res in self.detect_module.list_items(event):
                yield res
        except Exception as e:
            logger.error(f"查看检测词列表异常: {e}", exc_info=True)
            yield event.plain_result(f"查看检测词列表时发生错误: {e}")

    @filter.command("查看检测词")
    async def view_detect_cmd(self, event: AstrMessageEvent):
        """查看检测词详情。用法: /查看检测词 <序号或内容>"""
        try:
            async for res in self.detect_module.view_item(event):
                yield res
        except Exception as e:
            logger.error(f"查看检测词异常: {e}", exc_info=True)
            yield event.plain_result(f"查看检测词详情时发生错误: {e}")

    @filter.command("查看检测词回复")
    async def view_detect_reply_cmd(self, event: AstrMessageEvent):
        """查看指定检测词的某个回复。用法: /查看检测词回复 <检测词序号/内容> [回复序号]"""
        try:
            async for res in self.detect_module.view_reply(event):
                yield res
        except Exception as e:
            logger.error(f"查看检测词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"查看回复时发生错误: {e}")

    @filter.command("添加检测词回复")
    async def add_detect_reply_cmd(self, event: AstrMessageEvent):
        """为指定检测词添加新回复。用法: /添加检测词回复 <检测词序号/内容> [新回复内容]。可直接引用一条消息作为回复内容。"""
        try:
            async for res in self.detect_module.add_reply(event):
                yield res
        except Exception as e:
            logger.error(f"添加检测词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"添加回复时发生错误: {e}")

    @filter.command("编辑检测词回复")
    async def edit_detect_reply_cmd(self, event: AstrMessageEvent):
        """修改指定检测词的某个回复条目。用法: /编辑检测词回复 <检测词序号/内容> [回复序号] [新内容]。可直接引用一条消息作为新内容。"""
        try:
            async for res in self.detect_module.edit_reply(event):
                yield res
        except Exception as e:
            logger.error(f"编辑检测词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"编辑回复时发生错误: {e}")

    @filter.command("删除检测词回复")
    async def del_detect_reply_cmd(self, event: AstrMessageEvent):
        """删除指定检测词的某个回复条目。用法: /删除检测词回复 <检测词序号/内容> [回复序号]"""
        try:
            async for res in self.detect_module.delete_reply(event):
                yield res
        except Exception as e:
            logger.error(f"删除检测词回复异常: {e}", exc_info=True)
            yield event.plain_result(f"删除回复时发生错误: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理所有消息事件，包括命令触发和自动检测。"""
        msg = event.message_str.strip()

        session = self._get_capture_session(event)
        control_commands = {
            "/结束添加", "结束添加", "/取消添加", "取消添加"
        }
        is_start_command = (
            msg in {"/添加", "添加"}
            or msg.startswith("/添加 ")
            or msg.startswith("添加 ")
        )
        if session and msg not in control_commands and not is_start_command:
            entry, _ = self.utils.parse_message_to_entry(event.get_messages())
            if not self.utils.entry_has_payload(entry):
                yield event.plain_result("这条消息没有可保存的内容，请发送文字、图片、语音或视频。")
                event.stop_event()
                return
            processed_entry = await self.utils.process_entry_media(entry)
            if not self.utils.entry_has_payload(processed_entry):
                yield event.plain_result("媒体保存失败，请重新发送。")
                event.stop_event()
                return
            session["entries"].append(processed_entry)
            session["updated_at"] = time.monotonic()
            event.stop_event()
            return

        if not msg:
            return

        management_prefixes = [
            "/添加",
            "/编辑",
            "/删除",
            "/启用",
            "/禁用",
            "/查看",
            "/结束添加",
            "/取消添加",
            "添加",
            "编辑",
            "删除",
            "启用",
            "禁用",
            "查看",
        ]
        if any(msg.startswith(p) for p in management_prefixes):
            return

        recall_delay = self.config.get("recall_delay", "0 0").split()
        kw_delay = int(recall_delay[0]) if len(recall_delay) > 0 else 0
        dt_delay = int(recall_delay[1]) if len(recall_delay) > 1 else 0

        command_match = await self.cmd_module.handle_message(event)
        if command_match:
            entry = command_match["payload"]
            kw_delay = get_effective_recall_delay(
                self, command_match["rule"], "command_triggered"
            )
            if isinstance(entry, list):
                if command_match["rule"].get("sequential", False):
                    for index, item in enumerate(entry):
                        await self.utils.send_entry_reply(
                            event, item, kw_delay, use_quote=index == 0,
                            rule_cfg=command_match["rule"],
                        )
                else:
                    sent = await self.utils.send_entries_forward_reply(
                        event, entry, kw_delay, rule_cfg=command_match["rule"]
                    )
                    if not sent:
                        await self.utils.send_entry_reply(
                            event, entry[0], kw_delay, use_quote=True,
                            rule_cfg=command_match["rule"],
                        )
            else:
                await self.utils.send_entry_reply(
                    event,
                    entry,
                    kw_delay,
                    use_quote=True,
                    rule_cfg=command_match["rule"],
                )
            event.stop_event()
            return

        detect_match = await self.detect_module.handle_message(event)
        if detect_match:
            entry = detect_match["payload"]
            dt_delay = get_effective_recall_delay(
                self, detect_match["rule"], "auto_detect"
            )
            if isinstance(entry, list):
                if detect_match["rule"].get("sequential", False):
                    for index, item in enumerate(entry):
                        await self.utils.send_entry_reply(
                            event, item, dt_delay, use_quote=index == 0,
                            rule_cfg=detect_match["rule"],
                        )
                else:
                    sent = await self.utils.send_entries_forward_reply(
                        event, entry, dt_delay, rule_cfg=detect_match["rule"]
                    )
                    if not sent:
                        await self.utils.send_entry_reply(
                            event, entry[0], dt_delay, use_quote=True,
                            rule_cfg=detect_match["rule"],
                        )
            else:
                await self.utils.send_entry_reply(
                    event,
                    entry,
                    dt_delay,
                    use_quote=True,
                    rule_cfg=detect_match["rule"],
                )
            return
