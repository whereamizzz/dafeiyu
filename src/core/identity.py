"""发送者身份解析。

微信 4.x 的群消息 `real_sender_id` 指向 **同一条 message_*.db 里的 Name2Id**
(rowid → user_name)，而不是 message_resource.db 的 SenderName2Id。
按后者解析会把张三显示成李四、把某个昵称显示成数字 id。

这里用「多库候选 + 群成员归属」消歧：同一个数字 id 在不同 message 库里可能
映射到不同人，优先取该群成员表中的那个。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class SenderResolver:
    def __init__(self, db, ttl: float = 120.0):
        self._db = db
        self._ttl = ttl
        self._loaded_at = 0.0
        self._name2id: Dict[str, Dict[int, str]] = {}
        self._room_members: Dict[str, Set[str]] = {}
        self._contact_by_id: Dict[int, str] = {}

    # ---------------- 加载 ----------------
    def _refresh(self) -> None:
        db = self._db
        files = getattr(db, "_db_files", [])
        name2id: Dict[str, Dict[int, str]] = {}
        contact_by_id: Dict[int, str] = {}
        members: Dict[str, Set[str]] = {}
        for item in files:
            rel, path = item[0], item[1]
            base = os.path.basename(path)
            try:
                if base.startswith("message_") and base.endswith(".db"):
                    conn = db._open(rel)
                    try:
                        table = {}
                        for rid, name in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                            if name:
                                table[int(rid)] = str(name)
                        name2id[base] = table
                    finally:
                        conn.close()
                elif base == "contact.db":
                    conn = db._open(rel)
                    try:
                        for rid, name in conn.execute("SELECT rowid, username FROM name2id"):
                            if name:
                                contact_by_id[int(rid)] = str(name)
                        for room, mid in conn.execute("SELECT room_id, member_id FROM chatroom_member"):
                            members.setdefault(str(room), set()).add(int(mid))
                    finally:
                        conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"身份解析读取 {base} 失败: {exc}")
        self._name2id = name2id
        self._contact_by_id = contact_by_id
        self._room_members = members
        self._loaded_at = time.time()
        logger.info(
            f"身份解析已加载：{len(name2id)} 个消息库，{len(contact_by_id)} 个联系人，"
            f"{len(members)} 个群成员表"
        )

    def _ensure(self, force: bool = False) -> None:
        if force or not self._loaded_at or (time.time() - self._loaded_at) > self._ttl:
            self._refresh()

    # ---------------- 解析 ----------------
    def resolve(self, room_id: str, sender_id) -> str:
        """把 real_sender_id 解析成 wxid；解析不出返回空串。"""
        try:
            sid = int(sender_id)
        except (TypeError, ValueError):
            return ""
        if sid <= 0:
            return ""
        self._ensure()
        candidates: List[str] = []
        for table in self._name2id.values():
            name = table.get(sid)
            if name and name not in candidates:
                candidates.append(name)
        if not candidates:
            self._ensure(force=True)
            for table in self._name2id.values():
                name = table.get(sid)
                if name and name not in candidates:
                    candidates.append(name)
        if not candidates:
            # contact.name2id 兜底
            name = self._contact_by_id.get(sid, "")
            return name if name and not name.isdigit() else ""

        member_names = self._room_member_names(room_id)

        def score(name: str) -> int:
            value = 0
            if name in member_names:
                value += 8
            if name.endswith("@chatroom"):
                value -= 4
            if name.startswith("wxid_") or "_" in name:
                value += 2
            if name.isdigit():
                value -= 10
            return value

        candidates.sort(key=score, reverse=True)
        best = candidates[0]
        return best if best and not best.isdigit() else ""

    def _room_member_names(self, room_id: str) -> Set[str]:
        ids = self._room_members.get(room_id) or set()
        return {self._contact_by_id[i] for i in ids if i in self._contact_by_id}

    def display(self, sender_id, room_id: str, fallback: str = "") -> str:
        """直接给展示名：优先解析出的 wxid 的昵称，其次原值。"""
        wxid = self.resolve(room_id, sender_id)
        target = wxid or fallback
        if not target or target.isdigit():
            return "群友"
        try:
            name = self._db.get_nickname(target)
        except Exception:  # noqa: BLE001
            name = ""
        if not name or name.isdigit():
            return "群友"
        return name
