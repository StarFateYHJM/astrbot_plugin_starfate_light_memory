import json
from typing import Dict, Any, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .memory_manager import MemoryManager


class ToolHandler:
    
    def __init__(self, memory_manager: MemoryManager, debug_mode: bool = False):
        self.memory_manager = memory_manager
        self.debug_mode = debug_mode
    
    def _debug_log(self, message: str, data: Dict[str, Any] = None):
        if self.debug_mode:
            if data:
                logger.debug(f"[StarfateMemory][DEBUG] {message}: {json.dumps(data, ensure_ascii=False, default=str)}")
            else:
                logger.debug(f"[StarfateMemory][DEBUG] {message}")
    
    def get_tool_parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "检索关键词，多个关键词用空格分隔。请提取用户问题中最核心的词汇。"
                }
            },
            "required": ["keywords"]
        }
    
    async def handle_recall_memory(
        self, 
        event: AstrMessageEvent, 
        keywords: str
    ) -> str:
        session_id = event.message_obj.session_id
        
        self._debug_log("【工具调用流程】收到 recall_memory 调用", {
            "session_id": session_id,
            "keywords": keywords
        })
        
        if not session_id:
            self._debug_log("【工具调用流程】无法获取 session_id")
            logger.warning("[StarfateMemory] 无法获取 session_id")
            return "无法获取当前会话信息。"
        
        logger.info(f"[StarfateMemory] 召回请求 - session: {session_id[:20]}..., keywords: {keywords}")
        
        self._debug_log("【工具调用流程】调用 memory_manager.search_memory")
        results = await self.memory_manager.search_memory(
            session_id=session_id,
            keywords=keywords
        )
        
        if not results:
            self._debug_log("【工具调用流程】未找到相关记忆", {"keywords": keywords})
            return f"未找到与「{keywords}」相关的历史对话记录。"
        
        formatted_result = self._format_memory_results(results, keywords)
        
        self._debug_log("【工具调用流程】记忆格式化完成，返回给LLM", {
            "result_count": len(results),
            "formatted_length": len(formatted_result)
        })
        
        return formatted_result
    
    def _format_memory_results(self, results: List[Dict[str, Any]], keywords: str) -> str:
        lines = [f"找到 {len(results)} 条与「{keywords}」相关的历史记忆：\n"]
        
        for i, record in enumerate(results, 1):
            role_display = "用户" if record["role"] == "user" else "助手"
            content = record["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            
            lines.append(f"{i}. [{role_display}] {content}")
        
        lines.append("\n请基于以上历史信息，结合用户当前的问题进行回复。")
        
        return "\n".join(lines)
