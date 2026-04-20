import json
from typing import Optional
from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import At, Plain
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .core import MemoryManager, ToolHandler


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
        
        self._debug_log("【插件初始化】开始初始化", {
            "config": self.config
        })
        
        self.memory_manager = MemoryManager(
            plugin_name="astrbot_plugin_starfate_light_memory",
            max_recall_results=self.max_recall_results,
            memory_expire_days=self.memory_expire_days,
            debug_mode=self.debug_mode
        )
        
        self.tool_handler = ToolHandler(self.memory_manager, debug_mode=self.debug_mode)
        
        self._debug_log("【插件初始化】完成")
        logger.info(f"[StarfateMemory] 插件初始化完成 - debug_mode: {self.debug_mode}")
    
    def _debug_log(self, message: str, data: dict = None):
        if self.debug_mode:
            if data:
                logger.debug(f"[StarfateMemory][DEBUG] {message}: {json.dumps(data, ensure_ascii=False, default=str)}")
            else:
                logger.debug(f"[StarfateMemory][DEBUG] {message}")
    
    def _is_mentioned(self, event: AstrMessageEvent) -> bool:
        """检查机器人是否被@"""
        message_obj = event.message_obj
        self_id = message_obj.self_id
        
        for comp in message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == str(self_id):
                return True
        return False
    
    def _is_command(self, event: AstrMessageEvent) -> bool:
        """检查消息是否为命令（以/开头）"""
        message_str = event.message_str.strip()
        return message_str.startswith("/")
    
    def _is_llm_triggered(self, event: AstrMessageEvent) -> bool:
        """检查是否触发了LLM（命令不会触发LLM）"""
        return not self._is_command(event)
    
    @dataclass
    class RecallMemoryTool(FunctionTool[AstrAgentContext]):
        plugin_ref: "LightMemoryPlugin" = None
        
        name: str = "recall_memory"
        description: str = "当需要回忆或查询与用户的历史对话内容时调用此工具。仅在确实需要参考过去对话信息时才使用。"
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": "检索关键词，多个关键词用空格分隔。请提取用户问题中最核心的词汇。"
                    }
                },
                "required": ["keywords"]
            }
        )
        
        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            event = context.context.event
            keywords = kwargs.get("keywords", "")
            
            self.plugin_ref._debug_log("【工具调用】recall_memory 被调用", {"keywords": keywords})
            
            if not keywords:
                self.plugin_ref._debug_log("【工具调用】关键词为空")
                return ToolExecResult(result="错误：未提供检索关键词。")
            
            message_obj = event.message_obj
            if message_obj.group_id:
                chat_type = "group"
            else:
                chat_type = "private"
            
            isolated_session_id = f"{chat_type}_{message_obj.session_id}"
            
            result_text = await self.plugin_ref.tool_handler.handle_recall_memory(
                event=event,
                keywords=keywords,
                session_id_override=isolated_session_id
            )
            
            self.plugin_ref._debug_log("【工具调用】返回结果", {"result_length": len(result_text)})
            return ToolExecResult(result=result_text)
    
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊消息：无条件记录"""
        self._debug_log("【私聊监听】收到私聊消息")
        await self._record_message(event, "private")
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊消息：只有被@且触发LLM才记录"""
        self._debug_log("【群聊监听】收到群聊消息")
        
        if not self._is_mentioned(event):
            self._debug_log("【群聊监听】未被@，跳过记录")
            return
        
        if not self._is_llm_triggered(event):
            self._debug_log("【群聊监听】命令消息，跳过记录")
            return
        
        self._debug_log("【群聊监听】被@且触发LLM，记录消息")
        await self._record_message(event, "group")
    
    async def _record_message(self, event: AstrMessageEvent, chat_type: str):
        """统一的消息记录方法"""
        message_obj = event.message_obj
        session_id = message_obj.session_id
        content = message_obj.message_str
        
        if not content or not content.strip():
            self._debug_log("【消息监听】跳过空消息")
            return
        
        isolated_session_id = f"{chat_type}_{session_id}"
        
        self_id = message_obj.self_id
        sender_id = message_obj.sender.user_id if message_obj.sender else None
        role = "assistant" if (sender_id and str(sender_id) == str(self_id)) else "user"
        
        self._debug_log("【消息监听】准备写入记忆", {
            "chat_type": chat_type,
            "original_session": session_id,
            "isolated_session": isolated_session_id,
            "role": role,
            "content_length": len(content),
            "self_id": self_id,
            "sender_id": sender_id
        })
        
        success = await self.memory_manager.add_memory(
            session_id=isolated_session_id,
            role=role,
            content=content,
            timestamp=message_obj.timestamp
        )
        
        if success:
            self._debug_log("【消息监听】记忆写入成功")
            logger.debug(f"[StarfateMemory] 已记录[{chat_type}]: [{role}] {content[:50]}...")
        else:
            self._debug_log("【消息监听】记忆写入失败")
            logger.warning(f"[StarfateMemory] 记录失败: {isolated_session_id}")
    
    @filter.command("memory_stats")
    async def cmd_memory_stats(self, event: AstrMessageEvent):
        self._debug_log("【命令】memory_stats 被调用")
        
        stats = await self.memory_manager.get_stats()
        
        response = f"""📊 **轻量记忆统计**
- 总记忆条数: {stats.get('total_records', 0)}
- 总用户数: {stats.get('total_users', 0)}
- 总会话数: {stats.get('total_sessions', 0)}
- 数据库路径: {stats.get('db_path', 'N/A')}
- 最大召回数: {self.max_recall_results}
- 记忆保留: {self.memory_expire_days} 天
- 调试模式: {'开启' if self.debug_mode else '关闭'}"""
        
        yield event.plain_result(response)
    
    @filter.command("memory_recent")
    async def cmd_memory_recent(self, event: AstrMessageEvent, limit: str = "5"):
        self._debug_log("【命令】memory_recent 被调用", {"limit": limit})
        
        try:
            limit_num = int(limit)
        except ValueError:
            limit_num = 5
        
        message_obj = event.message_obj
        if message_obj.group_id:
            chat_type = "group"
        else:
            chat_type = "private"
        
        isolated_session_id = f"{chat_type}_{message_obj.session_id}"
        records = await self.memory_manager.get_recent_memory(isolated_session_id, limit_num)
        
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
    async def cmd_memory_search(self, event: AstrMessageEvent, keywords: str):
        """按关键词检索记忆（调试用）"""
        self._debug_log("【命令】memory_search 被调用", {"keywords": keywords})
        
        if not keywords:
            yield event.plain_result("请提供检索关键词，例如: /memory_search 颜色")
            return
        
        message_obj = event.message_obj
        if message_obj.group_id:
            chat_type = "group"
        else:
            chat_type = "private"
        
        isolated_session_id = f"{chat_type}_{message_obj.session_id}"
        results = await self.memory_manager.search_memory(
            session_id=isolated_session_id,
            keywords=keywords
        )
        
        if not results:
            yield event.plain_result(f"未找到与「{keywords}」相关的记忆。")
            return
        
        lines = [f"🔍 **找到 {len(results)} 条与「{keywords}」相关的记忆**\n"]
        for i, r in enumerate(results, 1):
            role_icon = "👤" if r["role"] == "user" else "🤖"
            content = r["content"][:150] + "..." if len(r["content"]) > 150 else r["content"]
            lines.append(f"{i}. {role_icon} {content}")
        
        yield event.plain_result("\n".join(lines))
    
    @filter.command("memory_clear")
    async def cmd_memory_clear(self, event: AstrMessageEvent):
        self._debug_log("【命令】memory_clear 被调用")
        
        message_obj = event.message_obj
        if message_obj.group_id:
            chat_type = "group"
        else:
            chat_type = "private"
        
        isolated_session_id = f"{chat_type}_{message_obj.session_id}"
        
        confirm = event.message_str.replace("/memory_clear", "").strip()
        if confirm.lower() != "confirm":
            yield event.plain_result("⚠️ 此操作将清除当前会话的所有记忆！如需确认，请输入: /memory_clear confirm")
            return
        
        success = await self.memory_manager.delete_session_memory(isolated_session_id)
        if success:
            self._debug_log("【命令】记忆清除成功")
            yield event.plain_result("✅ 当前会话的记忆已清除。")
        else:
            self._debug_log("【命令】记忆清除失败")
            yield event.plain_result("❌ 清除失败，请查看日志。")
    
    async def terminate(self):
        self._debug_log("【插件卸载】开始卸载")
        logger.info("[StarfateMemory] 插件正在卸载...")
