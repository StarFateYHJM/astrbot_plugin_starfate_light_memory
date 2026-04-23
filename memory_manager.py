import json
import re
import asyncio
import hashlib
import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class MemoryManager:
    
    def __init__(self, plugin_name: str, max_recall_results: int = 5, memory_expire_days: int = 30, debug_mode: bool = False):
        self.plugin_name = plugin_name
        self.max_recall_results = max_recall_results
        self.memory_expire_days = memory_expire_days
        self.debug_mode = debug_mode
        
        data_path_str = get_astrbot_data_path()
        data_path = Path(data_path_str) if isinstance(data_path_str, str) else data_path_str
        self.db_dir = data_path / "plugin_data" / plugin_name
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "memory.db"
        self.export_dir = self.db_dir / "exports"
        self.export_dir.mkdir(exist_ok=True)
        
        self._init_lock = asyncio.Lock()
        self._initialized = False
    
    def _debug_log(self, message: str, data: Dict[str, Any] = None):
        if self.debug_mode:
            if data:
                logger.debug(f"[StarfateMemory][DEBUG] {message}: {json.dumps(data, ensure_ascii=False, default=str)}")
            else:
                logger.debug(f"[StarfateMemory][DEBUG] {message}")
    
    async def _ensure_init(self):
        if self._initialized:
            return
        
        async with self._init_lock:
            if self._initialized:
                return
            
            try:
                async with aiosqlite.connect(str(self.db_path)) as db:
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS memory_records (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL,
                            role TEXT NOT NULL,
                            content TEXT NOT NULL,
                            content_hash TEXT NOT NULL,
                            timestamp REAL NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    await db.execute("CREATE INDEX IF NOT EXISTS idx_session_time ON memory_records(session_id, timestamp)")
                    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_session_hash ON memory_records(session_id, content_hash)")
                    await db.commit()
                
                self._initialized = True
                self._debug_log("数据库初始化完成")
                logger.info(f"[StarfateMemory] 数据库初始化完成: {self.db_path}")
                
            except Exception as e:
                self._debug_log("数据库初始化失败", {"error": str(e)})
                logger.error(f"[StarfateMemory] 数据库初始化失败: {e}")
                raise
    
    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.md5(content.encode("utf-8")).hexdigest()
    
    @staticmethod
    def _highlight_keywords(text: str, keyword_list: List[str]) -> str:
        for kw in keyword_list:
            text = re.sub(f"({re.escape(kw)})", r"**\1**", text, flags=re.IGNORECASE)
        return text
    
    async def add_memory(self, session_id: str, role: str, content: str, timestamp: float = None) -> bool:
        await self._ensure_init()
        
        if timestamp is None:
            timestamp = datetime.now().timestamp()
        
        content_hash = self._hash_content(content)
        
        self._debug_log("【写入流程】准备写入记忆", {
            "session_id": session_id,
            "role": role,
            "content_preview": content[:100] + "..." if len(content) > 100 else content,
            "timestamp": timestamp
        })
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO memory_records (session_id, role, content, content_hash, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, content_hash, timestamp)
                )
                await db.commit()
            
            self._debug_log("【写入流程】记忆写入成功", {"session_id": session_id, "role": role})
            return True
        except Exception as e:
            self._debug_log("【写入流程】记忆写入失败", {"error": str(e), "session_id": session_id})
            logger.error(f"[StarfateMemory] 写入记忆失败: {e}")
            return False
    
    async def merge_memories(self, session_id: str, memories: List[Dict[str, Any]]) -> int:
        await self._ensure_init()
        
        data = []
        for mem in memories:
            role = mem.get("role", "user")
            content = mem.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            timestamp = mem.get("timestamp", datetime.now().timestamp())
            content_hash = self._hash_content(content)
            data.append((session_id, role, content, content_hash, timestamp))
        
        if not data:
            return 0
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.executemany(
                    "INSERT OR REPLACE INTO memory_records (session_id, role, content, content_hash, timestamp) VALUES (?, ?, ?, ?, ?)",
                    data
                )
                await db.commit()
            
            self._debug_log("【合并写入】完成", {"session_id": session_id, "count": len(data)})
            return len(data)
        except Exception as e:
            self._debug_log("【合并写入】失败", {"error": str(e)})
            logger.error(f"[StarfateMemory] 合并写入失败: {e}")
            return 0
    
    async def search_memory(self, session_id: str, keywords: str, limit: int = None) -> List[Dict[str, Any]]:
        await self._ensure_init()
        
        if limit is None:
            limit = self.max_recall_results
        
        keyword_list = [kw.strip() for kw in keywords.split() if kw.strip()]
        
        self._debug_log("【检索流程】开始检索", {
            "session_id": session_id,
            "keywords": keywords,
            "keyword_list": keyword_list,
            "limit": limit
        })
        
        if not keyword_list:
            self._debug_log("【检索流程】关键词列表为空，返回空结果")
            return []
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                conditions = " OR ".join([f"content LIKE '%' || ? || '%'" for _ in keyword_list])
                
                if self.memory_expire_days > 0:
                    expire_timestamp = (datetime.now() - timedelta(days=self.memory_expire_days)).timestamp()
                    query = f"""
                        SELECT id, session_id, role, content, timestamp, created_at
                        FROM memory_records
                        WHERE session_id = ? 
                        AND timestamp > {expire_timestamp} 
                        AND ({conditions})
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """
                else:
                    query = f"""
                        SELECT id, session_id, role, content, timestamp, created_at
                        FROM memory_records
                        WHERE session_id = ? AND ({conditions})
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """
                params = [session_id] + keyword_list + [limit]
                
                self._debug_log("【检索流程】执行SQL查询", {"query": query, "params": params})
                
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    results = [dict(row) for row in rows]
                    
                    for r in results:
                        r["content"] = self._highlight_keywords(r["content"], keyword_list)
                    
                    self._debug_log("【检索流程】检索完成", {"result_count": len(results), "keywords": keywords})
                    return results
                    
        except Exception as e:
            self._debug_log("【检索流程】检索失败", {"error": str(e)})
            logger.error(f"[StarfateMemory] 检索记忆失败: {e}")
            return []
    
    async def get_recent_memory(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        await self._ensure_init()
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM memory_records WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit)
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"[StarfateMemory] 获取最近记忆失败: {e}")
            return []
    
    async def get_all_memory(self, session_id: str) -> List[Dict[str, Any]]:
        await self._ensure_init()
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM memory_records WHERE session_id = ? ORDER BY timestamp ASC",
                    (session_id,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"[StarfateMemory] 获取所有记忆失败: {e}")
            return []
    
    async def export_memory(self, session_id: str, format_type: str = "txt") -> Optional[Path]:
        await self._ensure_init()
        
        records = await self.get_all_memory(session_id)
        if not records:
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if format_type == "json":
            filepath = self.export_dir / f"memory_{session_id}_{timestamp}.json"
            export_data = []
            for r in records:
                export_data.append({
                    "role": r["role"],
                    "content": r["content"],
                    "timestamp": r["timestamp"]
                })
            filepath.write_text(json.dumps(export_data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            filepath = self.export_dir / f"memory_{session_id}_{timestamp}.txt"
            lines = [
                f"# 记忆导出",
                f"# 会话ID: {session_id}",
                f"# 导出时间: {datetime.now()}",
                f"# 消息数量: {len(records)}",
                "=" * 50,
                ""
            ]
            for i, r in enumerate(records, 1):
                role_display = "👤 用户" if r["role"] == "user" else "🤖 助手"
                time_str = datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"[{i}] [{time_str}] {role_display}:")
                content = r["content"]
                try:
                    content_list = json.loads(content)
                    if isinstance(content_list, list):
                        for item in content_list:
                            if item.get("type") == "text":
                                lines.append(item.get("text", ""))
                except:
                    lines.append(content)
                lines.append("-" * 40)
                lines.append("")
            
            filepath.write_text("\n".join(lines), encoding="utf-8")
        
        return filepath
    
    async def delete_session_memory(self, session_id: str) -> int:
        await self._ensure_init()
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute("DELETE FROM memory_records WHERE session_id = ?", (session_id,))
                deleted_count = cursor.rowcount
                await db.commit()
            
            self._debug_log("【清理流程】删除完成", {"session_id": session_id, "deleted_count": deleted_count})
            return deleted_count
        except Exception as e:
            logger.error(f"[StarfateMemory] 删除记忆失败: {e}")
            return 0
    
    async def get_stats(self) -> Dict[str, Any]:
        await self._ensure_init()
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                async with db.execute("SELECT COUNT(*) FROM memory_records") as cursor:
                    total_count = (await cursor.fetchone())[0]
                
                async with db.execute("SELECT COUNT(DISTINCT session_id) FROM memory_records") as cursor:
                    session_count = (await cursor.fetchone())[0]
                
                return {
                    "total_records": total_count,
                    "total_sessions": session_count,
                    "db_path": str(self.db_path)
                }
        except Exception as e:
            return {"error": str(e)}
