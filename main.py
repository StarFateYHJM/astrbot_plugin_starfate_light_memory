import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import At
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .memory_manager import MemoryManager


@register(
    "astrbot_plugin_starfate_light_memory",
    "Starfate",
    "轻量级智能记忆插件 - 基于 AI 自主调度的低成本记忆方案",
    "1.0.0",
    "https://github.com/starfate/astrbot_plugin_starfate_light_memory"
)
class LightMemoryPlugin(Star):
    
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        
        self.config = config or {}
        self.debug_mode = self.config.get("debug_mode", False)
        self.max_recall_results = self.config.get("max_recall_results", 5)
        self.memory_expire_days = self.config.get("memory_expire_days", 30)
        
        self._debug_log("【插件初始化】开始", {"config": self.config})
        
        self.memory_manager = MemoryManager(
            plugin_name="astrbot_plugin_starfate_light_memory",
            max_recall_results=self.max_recall_results,
            memory_expire_days=self.memory_expire_days,
            debug_mode=self.debug_mode
        )
        
        self._debug_log("【插件初始化】完成")
        logger.info(f"[StarfateMemory] 插件初始化完成 - debug_mode: {self.debug_mode}")
    
    def _debug_log(self, message: str, data: dict = None):
        if self.debug_mode:
            if data:
                logger.debug(f"[StarfateMemory][DEBUG] {message}: {json.dumps(data, ensure_ascii=False, default=str)}")
            else:
                logger.debug(f"[StarfateMemory][DEBUG] {message}")
    
    def _get_session_id(self, event: AstrMessageEvent) -> str:
        return event.message_obj.session_id
    
    def _get_chat_type(self, event: AstrMessageEvent) -> str:
        return "group" if event.message_obj.group_id else "private"
    
    def _is_mentioned(self, event: AstrMessageEvent) -> bool:
        message_obj = event.message_obj
        self_id = str(message_obj.self_id)
        for comp in message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == self_id:
                return True
        return False
    
    def _is_command(self, event: AstrMessageEvent) -> bool:
        return event.message_str.strip().startswith("/")
    
    def _get_role(self, event: AstrMessageEvent) -> str:
        message_obj = event.message_obj
        self_id = str(message_obj.self_id)
        sender_id = str(message_obj.sender.user_id) if message_obj.sender else None
        return "assistant" if sender_id == self_id else "user"
    
    def _format_memory_results(self, results: List[Dict[str, Any]], keywords: str) -> str:
        lines = [f"找到 {len(results)} 条与「{keywords}」相关的历史记忆：\n"]
        for i, record in enumerate(results, 1):
            role_display = "用户" if record["role"] == "user" else "助手"
            content = record["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"{i}. [{role_display}] {content}")
        lines.append("\n请基于以上历史信息，结合用户当前的问题进行回复。")
        return "\n".join(lines)
    
    # ==================== LLM 工具 ====================
    
    @dataclass
    class RecallMemoryTool(FunctionTool[AstrAgentContext]):
        plugin_ref: "LightMemoryPlugin" = None
        
        name: str = "recall_memory"
        description: str = "当需要回忆或查询与用户的历史对话内容时调用此工具。仅在确实需要参考过去对话信息时才使用。"
        parameters: dict = field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "检索关键词，多个关键词用空格分隔。请提取用户问题中最核心的词汇。"
                }
            },
            "required": ["keywords"]
        })
        
        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            event = context.context.event
            keywords = kwargs.get("keywords", "")
            
            self.plugin_ref._debug_log("【工具调用】recall_memory", {"keywords": keywords})
            
            if not keywords:
                return ToolExecResult(result="错误：未提供检索关键词。")
            
            session_id = self.plugin_ref._get_session_id(event)
            results = await self.plugin_ref.memory_manager.search_memory(
                session_id=session_id, keywords=keywords
            )
            
            if not results:
                return ToolExecResult(result=f"未找到与「{keywords}」相关的历史对话记录。")
            
            formatted = self.plugin_ref._format_memory_results(results, keywords)
            return ToolExecResult(result=formatted)
    
    # ==================== 消息监听 ====================
    
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        await self._record_message(event)
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if self._is_command(event):
            return
        if not self._is_mentioned(event):
            return
        await self._record_message(event)
    
    async def _record_message(self, event: AstrMessageEvent):
        content = event.message_obj.message_str
        if not content or not content.strip():
            return
        
        session_id = self._get_session_id(event)
        role = self._get_role(event)
        chat_type = self._get_chat_type(event)
        
        self._debug_log("【消息监听】写入记忆", {
            "session_id": session_id,
            "chat_type": chat_type,
            "role": role,
            "content_preview": content[:50]
        })
        
        success = await self.memory_manager.add_memory(
            session_id=session_id,
            role=role,
            content=content,
            timestamp=event.message_obj.timestamp
        )
        
        if success and self.debug_mode:
            logger.debug(f"[StarfateMemory] 已记录[{chat_type}]: [{role}] {content[:50]}...")
    
    # ==================== AI 回复记录 ====================
    
    @filter.on_after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        self._debug_log("【钩子】消息已发送，准备记录")
        
        message_obj = event.message_obj
        content = message_obj.message_str
        
        if not content or not content.strip():
            self._debug_log("【钩子】消息内容为空")
            return
        
        # 只记录 assistant 发出的消息（群聊中可能还有其他消息）
        if message_obj.role != "assistant":
            self._debug_log("【钩子】非 AI 回复，跳过", {"role": message_obj.role})
            return
        
        session_id = self._get_session_id(event)
        
        self._debug_log("【钩子】记录 AI 回复", {
            "session_id": session_id,
            "content_preview": content[:50]
        })
        
        await self.memory_manager.add_memory(
            session_id=session_id,
            role="assistant",
            content=content,
            timestamp=message_obj.timestamp
        )
    
    # ==================== 指令 ====================
    
    @filter.command("memory_stats")
    async def cmd_memory_stats(self, event: AstrMessageEvent):
        stats = await self.memory_manager.get_stats()
        response = (
            f"📊 **轻量记忆统计**\n"
            f"- 总记忆条数: {stats.get('total_records', 0)}\n"
            f"- 总会话数: {stats.get('total_sessions', 0)}\n"
            f"- 最大召回数: {self.max_recall_results}\n"
            f"- 记忆保留: {self.memory_expire_days} 天\n"
            f"- 调试模式: {'开启' if self.debug_mode else '关闭'}"
        )
        yield event.plain_result(response)
    
    @filter.command("memory_recent")
    async def cmd_memory_recent(self, event: AstrMessageEvent, limit: str = "5"):
        try:
            limit_num = int(limit)
        except ValueError:
            limit_num = 5
        
        session_id = self._get_session_id(event)
        records = await self.memory_manager.get_recent_memory(session_id, limit_num)
        
        if not records:
            yield event.plain_result("当前会话暂无记忆记录。")
            return
        
        lines = [f"📝 **最近 {len(records)} 条记忆**\n"]
        for r in records:
            role_icon = "👤" if r["role"] == "user" else "🤖"
            content = r["content"][:100] + "..." if len(r["content"]) > 100 else r["content"]
            lines.append(f"{role_icon} {content}")
        
        yield event.plain_result("\n".join(lines))
    
    @filter.command("memory_search")
    async def cmd_memory_search(self, event: AstrMessageEvent, *, keywords: str = ""):
        keywords = keywords.strip()
        if not keywords:
            yield event.plain_result("请提供检索关键词，例如: /memory_search 颜色")
            return
        
        session_id = self._get_session_id(event)
        results = await self.memory_manager.search_memory(session_id=session_id, keywords=keywords)
        
        if not results:
            yield event.plain_result(f"未找到与「{keywords}」相关的记忆。")
            return
        
        lines = [f"🔍 **找到 {len(results)} 条与「{keywords}」相关的记忆**\n"]
        for i, r in enumerate(results, 1):
            role_icon = "👤" if r["role"] == "user" else "🤖"
            content = r["content"][:150] + "..." if len(r["content"]) > 150 else r["content"]
            lines.append(f"{i}. {role_icon} {content}")
        
        yield event.plain_result("\n".join(lines))
    
    @filter.command("memory_export")
    async def cmd_memory_export(self, event: AstrMessageEvent, format_type: str = "txt"):
        if format_type not in ("txt", "json"):
            format_type = "txt"
        
        session_id = self._get_session_id(event)
        filepath = await self.memory_manager.export_memory(session_id, format_type)
        
        if filepath is None:
            yield event.plain_result("❌ 当前会话暂无记忆记录，无法导出。")
            return
        
        yield event.plain_result(f"✅ 记忆已导出 ({format_type} 格式)\n📁 {filepath}")
    
    @filter.command("memory_clear")
    async def cmd_memory_clear(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        
        if not message_str.endswith("confirm"):
            yield event.plain_result("⚠️ 此操作将清除当前会话的所有记忆！如需确认，请输入: /memory_clear confirm")
            return
        
        session_id = self._get_session_id(event)
        deleted = await self.memory_manager.delete_session_memory(session_id)
        yield event.plain_result(f"✅ 已清除 {deleted} 条记忆。")
    
    async def terminate(self):
        self._debug_log("【插件卸载】")
        logger.info("[StarfateMemory] 插件正在卸载...")
