import json
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from typing import List, Dict, Any

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class MemoryManager:
    
    def __init__(self, plugin_name: str, max_recall_results: int = 5, memory_expire_days: int = 30, debug_mode: bool = False):
        self.plugin_name = plugin_name
        self.max_recall_results = max_recall_results
        self.memory_expire_days = memory_expire_days
        self.debug_mode = debug_mode
        
        data_path = get_astrbot_data_path()
        self.db_dir = data_path / "plugin_data" / plugin_name
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "memory.db"
        
        self._init_lock = asyncio.Lock()
        self._initialized = False
        
        self._debug_log("MemoryManager 初始化", {
            "plugin_name": plugin_name,
            "max_recall_results": max_recall_results,
            "memory_expire_days": memory_expire_days,
            "db_path": str(self.db_path)
        })
    
    def _debug_log(self, message: str, data: Dict[str, Any] = None):
        if self.debug_mode:
            if data:
                logger.debug(f"[StarfateMemory][DEBUG] {message}: {json.dumps(data, ensure_ascii=False, default=str)}")
            else:
                logger.debug(f"[StarfateMemory][DEBUG] {message}")
    
    async def _ensure_init(self):
        if self._initialized:
            self._debug_log("数据库已初始化，跳过")
            return
        
        async with self._init_lock:
            if self._initialized:
                return
            
            self._debug_log("开始初始化数据库", {"db_path": str(self.db_path)})
            
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS memory_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("CREATE INDEX IF NOT EXISTS idx_session_time ON memory_records(session_id, timestamp)")
                await db.commit()
            
            self._initialized = True
            self._debug_log("数据库初始化完成")
            logger.info(f"[StarfateMemory] 数据库初始化完成: {self.db_path}")
    
    async def add_memory(self, session_id: str, role: str, content: str, timestamp: float = None) -> bool:
        await self._ensure_init()
        
        if timestamp is None:
            timestamp = datetime.now().timestamp()
        
        self._debug_log("【写入流程】准备写入记忆", {
            "session_id": session_id,
            "role": role,
            "content_preview": content[:100] + "..." if len(content) > 100 else content,
            "timestamp": timestamp
        })
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    "INSERT INTO memory_records (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (session_id, role, content, timestamp)
                )
                await db.commit()
            
            self._debug_log("【写入流程】记忆写入成功", {
                "session_id": session_id,
                "role": role
            })
            return True
        except Exception as e:
            self._debug_log("【写入流程】记忆写入失败", {
                "error": str(e),
                "session_id": session_id
            })
            logger.error(f"[StarfateMemory] 写入记忆失败: {e}")
            return False
    
    async def search_memory(
        self, 
        session_id: str, 
        keywords: str, 
        limit: int = None
    ) -> List[Dict[str, Any]]:
        await self._ensure_init()
        
        if limit is None:
            limit = self.max_recall_results
        
        keyword_list = [kw.strip() for kw in keywords.split() if kw.strip()]
        
        self._debug_log("【检索流程】开始检索", {
            "session_id": session_id,
            "keywords": keywords,
            "keyword_list": keyword_list,
            "limit": limit,
            "memory_expire_days": self.memory_expire_days
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
                        WHERE session_id = ? AND timestamp > {expire_timestamp} AND ({conditions})
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
                
                self._debug_log("【检索流程】执行SQL查询", {
                    "query": query,
                    "params": params
                })
                
                db.row_factory = aiosqlite.Row
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    
                    results = []
                    for row in rows:
                        results.append({
                            "id": row["id"],
                            "session_id": row["session_id"],
                            "role": row["role"],
                            "content": row["content"],
                            "timestamp": row["timestamp"],
                            "created_at": row["created_at"]
                        })
                    
                    self._debug_log("【检索流程】检索完成", {
                        "result_count": len(results),
                        "keywords": keywords
                    })
                    
                    return results
                    
        except Exception as e:
            self._debug_log("【检索流程】检索失败", {
                "error": str(e),
                "keywords": keywords
            })
            logger.error(f"[StarfateMemory] 检索记忆失败: {e}")
            return []
    
    async def get_recent_memory(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        await self._ensure_init()
        
        self._debug_log("获取最近记忆", {
            "session_id": session_id,
            "limit": limit
        })
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM memory_records WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit)
                ) as cursor:
                    rows = await cursor.fetchall()
                    results = [dict(row) for row in rows]
                    
                    self._debug_log("获取最近记忆完成", {
                        "count": len(results)
                    })
                    
                    return results
        except Exception as e:
            self._debug_log("获取最近记忆失败", {"error": str(e)})
            logger.error(f"[StarfateMemory] 获取最近记忆失败: {e}")
            return []
    
    async def delete_session_memory(self, session_id: str) -> bool:
        await self._ensure_init()
        
        self._debug_log("【清理流程】删除会话记忆", {
            "session_id": session_id
        })
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute("DELETE FROM memory_records WHERE session_id = ?", (session_id,))
                deleted_count = cursor.rowcount
                await db.commit()
            
            self._debug_log("【清理流程】会话记忆已删除", {
                "session_id": session_id,
                "deleted_count": deleted_count
            })
            return True
        except Exception as e:
            self._debug_log("【清理流程】删除失败", {
                "error": str(e),
                "session_id": session_id
            })
            logger.error(f"[StarfateMemory] 删除会话记忆失败: {e}")
            return False
    
    async def get_stats(self) -> Dict[str, Any]:
        await self._ensure_init()
        
        self._debug_log("获取统计信息")
        
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                async with db.execute("SELECT COUNT(*) FROM memory_records") as cursor:
                    total_count = (await cursor.fetchone())[0]
                
                async with db.execute("SELECT COUNT(DISTINCT session_id) FROM memory_records") as cursor:
                    session_count = (await cursor.fetchone())[0]
                
                stats = {
                    "total_records": total_count,
                    "total_sessions": session_count,
                    "db_path": str(self.db_path)
                }
                
                self._debug_log("统计信息获取完成", stats)
                
                return stats
        except Exception as e:
            self._debug_log("获取统计信息失败", {"error": str(e)})
            logger.error(f"[StarfateMemory] 获取统计信息失败: {e}")
            return {"error": str(e)}
