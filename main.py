import json
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

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
        
        self._register_llm_tools()
        
        self._debug_log("【插件初始化】完成")
        logger.info(f"[StarfateMemory] 插件初始化完成 - debug_mode: {self.debug_mode}")
    
    def _debug_log(self, message: str, data: dict = None):
        if self.debug_mode:
            if data:
                logger.debug(f"[StarfateMemory][DEBUG] {message}: {json.dumps(data, ensure_ascii=False, default=str)}")
            else:
                logger.debug(f"[StarfateMemory][DEBUG] {message}")
    
    def _register_llm_tools(self):
        self._debug_log("【工具注册】注册 recall_memory 工具")
        
        self.context.register_llm_tool(
            name="recall_memory",
            description="当需要回忆或查询与用户的历史对话内容时调用此工具。仅在确实需要参考过去对话信息时才使用。",
            parameters=self.tool_handler.get_tool_parameters(),
            handler=self._on_recall_memory
        )
        
        self._debug_log("【工具注册】recall_memory 工具注册完成")
        logger.info("[StarfateMemory] LLM 工具 recall_memory 已注册")
    
    async def _on_recall_memory(self, event: AstrMessageEvent, **kwargs):
        self._debug_log("【工具回调】_on_recall_memory 被调用", {"kwargs": kwargs})
        
        keywords = kwargs.get("keywords", "")
        
        if not keywords:
            self._debug_log("【工具回调】关键词为空")
            return "错误：未提供检索关键词。"
        
        result_text = await self.tool_handler.handle_recall_memory(
            event=event,
            keywords=keywords
        )
        
        return result_text
    
    @filter.on_message_received
    async def on_message_received(self, event: AstrMessageEvent):
        self._debug_log("【消息监听】收到新消息")
        
        message_obj = event.message_obj
        session_id = message_obj.session_id
        role = "user" if message_obj.role == "user" else "assistant"
        content = message_obj.message_str
        
        if not content or not content.strip():
            self._debug_log("【消息监听】跳过空消息")
            return
        
        self._debug_log("【消息监听】准备写入记忆", {
            "session_id": session_id,
            "role": role,
            "content_length": len(content)
        })
        
        success = await self.memory_manager.add_memory(
            session_id=session_id,
            role=role,
            content=content,
            timestamp=message_obj.timestamp
        )
        
        if success:
            self._debug_log("【消息监听】记忆写入成功")
            logger.debug(f"[StarfateMemory] 已记录: [{role}] {content[:50]}...")
        else:
            self._debug_log("【消息监听】记忆写入失败")
            logger.warning(f"[StarfateMemory] 记录失败: {session_id}")
    
    @filter.command("memory_stats")
    async def cmd_memory_stats(self, event: AstrMessageEvent):
        self._debug_log("【命令】memory_stats 被调用")
        
        stats = await self.memory_manager.get_stats()
        
        response = f"""📊 **轻量记忆统计**
- 总记忆条数: {stats.get('total_records', 0)}
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
        
        session_id = event.message_obj.session_id
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
    
    @filter.command("memory_clear")
    async def cmd_memory_clear(self, event: AstrMessageEvent):
        self._debug_log("【命令】memory_clear 被调用")
        
        session_id = event.message_obj.session_id
        
        confirm = event.message_str.replace("/memory_clear", "").strip()
        if confirm.lower() != "confirm":
            yield event.plain_result("⚠️ 此操作将清除当前会话的所有记忆！如需确认，请输入: /memory_clear confirm")
            return
        
        success = await self.memory_manager.delete_session_memory(session_id)
        if success:
            self._debug_log("【命令】记忆清除成功")
            yield event.plain_result("✅ 当前会话的记忆已清除。")
        else:
            self._debug_log("【命令】记忆清除失败")
            yield event.plain_result("❌ 清除失败，请查看日志。")
    
    async def terminate(self):
        self._debug_log("【插件卸载】开始卸载")
        logger.info("[StarfateMemory] 插件正在卸载...")
