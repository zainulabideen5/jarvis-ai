"""Image duplicate finder — REAL local script (vision/LLM nahi). Kisi bhi folder
ki images scan karke EXACT same files (MD5 hash) ko group karta hai, aur ek CSV
report banata hai. General: koi bhi folder, koi bhi user. Honest: jo actually
mila wahi report karta hai — kuch invent nahi.

Pillow optional (dimensions ke liye); MD5 dedup ko kisi dep ki zaroorat nahi.
"""
from __future__ import annotations

import csv
import hashlib
import os

from app.core.logging import get_logger

log = get_logger(__name__)

_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".heic"}


def _md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def find_image_duplicates(folder: str, recursive: bool = True) -> dict:
    """Folder ki saari images ko EXACT-content (MD5) se group karo. Returns dict
    with stats + groups (sirf woh jinme 2+ copies hain)."""
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not os.path.isdir(folder):
        return {"ok": False, "error": f"Folder nahi mila: {folder}"}

    files: list[str] = []
    if recursive:
        for root, _dirs, names in os.walk(folder):
            for n in names:
                if os.path.splitext(n)[1].lower() in _IMG_EXT:
                    files.append(os.path.join(root, n))
    else:
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and os.path.splitext(n)[1].lower() in _IMG_EXT:
                files.append(p)

    by_hash: dict[str, list[str]] = {}
    errors = 0
    for p in files:
        try:
            by_hash.setdefault(_md5(p), []).append(p)
        except Exception:
            errors += 1

    groups = sorted((v for v in by_hash.values() if len(v) > 1), key=len, reverse=True)
    dup_files = sum(len(g) for g in groups)
    wasted = 0
    for g in groups:
        try:
            wasted += os.path.getsize(g[0]) * (len(g) - 1)
        except Exception:
            pass

    return {
        "ok": True, "folder": folder,
        "total": len(files), "unique": len(by_hash),
        "dup_groups": len(groups), "dup_files": dup_files,
        "extra_copies": dup_files - len(groups),     # kitni "fazool" copies
        "wasted_mb": round(wasted / (1024 * 1024), 1),
        "groups": groups, "errors": errors,
    }


def write_report_csv(result: dict, out_path: str) -> str:
    """Duplicate groups ko CSV mein likho. Returns absolute path (verify-able)."""
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Group ID", "File Name", "Full Path", "Size (KB)", "Copies in Group"])
        for gid, g in enumerate(result.get("groups", []), start=1):
            for p in g:
                try:
                    kb = round(os.path.getsize(p) / 1024, 1)
                except Exception:
                    kb = ""
                w.writerow([f"G{gid}", os.path.basename(p), p, kb, len(g)])
    return out_path
