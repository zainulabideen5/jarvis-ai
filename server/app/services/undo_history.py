"""Undo/Redo for REVERSIBLE file/folder operations — general (any user, any
machine). JARVIS jab file/folder create/move/rename/delete kare (structured tools
se), woh yahan log hota hai with reverse-info. User chat se "undo karo"/"redo karo"
bole to reverse/redo ho jata hai.

Safe-delete: file ko PERMANENT delete nahi karte — ek recoverable JARVIS trash
folder mein move karte hain, taake undo se wapas aa sake (aur user bhi nikaal sake).

Irreversible cheezein (sent email/message, arbitrary scripts) yahan log NAHI hoti —
unke liye honestly "undo nahi ho sakta".
"""
from __future__ import annotations

import os
import shutil
import time

from app.core.logging import get_logger

log = get_logger(__name__)

TRASH_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "Jarvis", "trash")


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path.rstrip("\\/"))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _move(src: str, dst: str) -> None:
    _ensure_parent(dst)
    shutil.move(src, dst)


def trash_path_for(orig: str) -> str:
    """Recoverable trash mein ek unique jagah (original naam ke saath)."""
    name = os.path.basename(orig.rstrip("\\/")) or "item"
    sub = os.path.join(TRASH_DIR, str(int(time.time() * 1000)))
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, name)


class UndoHistory:
    """Process-wide session undo/redo stacks (singleton-style classmethods)."""

    _undo: list[dict] = []
    _redo: list[dict] = []

    # ---- recording (structured tools yeh call karte hain) ----
    @classmethod
    def record(cls, action: dict) -> None:
        cls._undo.append(action)
        cls._redo.clear()
        log.info("undo_recorded", kind=action.get("kind"), desc=action.get("desc", "")[:60])

    # ---- apply reverse / forward ----
    @staticmethod
    def _reverse(a: dict) -> None:
        kind = a.get("kind")
        if kind == "move":                       # src->dst tha → wapas dst->src
            _move(a["dst"], a["src"])
        elif kind == "delete":                   # orig->trash tha → trash->orig
            _move(a["trash"], a["orig"])
        elif kind == "create":                   # bana tha → trash mein bhejo
            tp = trash_path_for(a["path"])
            _move(a["path"], tp)
            a["trash"] = tp

    @staticmethod
    def _forward(a: dict) -> None:
        kind = a.get("kind")
        if kind == "move":
            _move(a["src"], a["dst"])
        elif kind == "delete":
            _move(a["orig"], a["trash"])
        elif kind == "create":                   # trash se wapas asli jagah
            _move(a["trash"], a["path"])

    # ---- public ----
    @classmethod
    def undo(cls) -> dict:
        if not cls._undo:
            return {"ok": False, "msg": "Undo karne ko kuch nahi hai, Boss."}
        a = cls._undo.pop()
        try:
            cls._reverse(a)
            cls._redo.append(a)
            return {"ok": True, "msg": f"Undo ho gaya — {a.get('desc', a.get('kind'))}"}
        except Exception as e:
            cls._undo.append(a)
            return {"ok": False, "msg": f"Undo nahi ho saka: {str(e)[:120]}"}

    @classmethod
    def redo(cls) -> dict:
        if not cls._redo:
            return {"ok": False, "msg": "Redo karne ko kuch nahi hai, Boss."}
        a = cls._redo.pop()
        try:
            cls._forward(a)
            cls._undo.append(a)
            return {"ok": True, "msg": f"Redo ho gaya — {a.get('desc', a.get('kind'))}"}
        except Exception as e:
            cls._redo.append(a)
            return {"ok": False, "msg": f"Redo nahi ho saka: {str(e)[:120]}"}

    @classmethod
    def last_desc(cls) -> str:
        return cls._undo[-1].get("desc", "") if cls._undo else ""
