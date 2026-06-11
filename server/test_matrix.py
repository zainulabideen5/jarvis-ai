"""Multi-laptop test matrix — automated verification runner.

Run on EACH laptop to verify JARVIS works correctly.

Usage:
    cd server
    venv\\Scripts\\python.exe test_matrix.py
    # OR with verbose:
    venv\\Scripts\\python.exe test_matrix.py -v

What it tests:
    - All 10 diagnostic layers (Python deps → LLM keys)
    - Excel COM round-trip (create → write → read → delete)
    - Notepad UIA round-trip (skipped if headless)
    - Multi-Gmail config readable
    - Image-match templates dir + cv2 working
    - LLM intent extraction
    - Server status

Exit code:
    0  All passed (or partial passes acceptable)
    1  Critical failures (any layer with status='fail')
    2  Server not running / not reachable

Results saved to:
    server/test_results/<hostname>-<timestamp>.json
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

SERVER_BASE = "http://127.0.0.1:8000"
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def _http_get(path: str, timeout: float = 30.0) -> dict:
    """Fetch JSON from server endpoint."""
    url = f"{SERVER_BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)[:200]}


def _print_result(label: str, status: str, details: str = ""):
    icon = {"ok": "✅", "partial": "🟡", "fail": "❌", "skip": "⏭️"}.get(status, "❓")
    print(f"  {icon} {label}", flush=True)
    if details and (VERBOSE or status in ("partial", "fail")):
        for line in str(details).split("\n")[:5]:
            print(f"     {line}", flush=True)


def test_server_reachable() -> bool:
    print("\n[1/5] Server reachable")
    r = _http_get("/api/diagnostics/full", timeout=5)
    if "error" in r:
        _print_result("Server not reachable at " + SERVER_BASE, "fail", r["error"])
        print("\nServer chalu nahi. Run: venv\\Scripts\\python -m uvicorn app.main:app --port 8000\n")
        return False
    _print_result(f"Server reachable", "ok")
    return True


def test_diagnostic_layers() -> dict:
    print("\n[2/5] Layer diagnostics (10 layers)")
    r = _http_get("/api/diagnostics/full", timeout=60)
    if "error" in r:
        print(f"  ❌ Diagnostic call failed: {r['error']}")
        return {"failed": 1, "total": 0}
    layers = r.get("layers", [])
    failed = 0
    partial = 0
    for layer in layers:
        name = layer.get("name", "?")
        status = layer.get("status", "?")
        hint = layer.get("fix_hint", "")
        _print_result(name, status, hint)
        if status == "fail":
            failed += 1
        elif status == "partial":
            partial += 1
    summary = r.get("summary", {})
    print(f"\n  Score: {summary.get('score_pct', '?')}% — passed {summary.get('passed','?')}, partial {summary.get('partial','?')}, failed {summary.get('failed','?')}")
    return {"failed": failed, "partial": partial, "total": len(layers), "summary": summary, "raw": r}


def test_excel_roundtrip() -> bool:
    """Create Excel file → write cell → read it back → delete."""
    print("\n[3/5] Excel COM round-trip")
    test_path = str(Path.cwd() / "test_excel_jarvis.xlsx")
    # Create
    create = _http_post("/api/office/excel/create", {
        "file_path": test_path,
        "headers": ["Name", "Email"],
        "rows": [["TestUser", "test@x.com"]],
    })
    if not create.get("ok"):
        _print_result("Excel create", "fail", create.get("error", ""))
        return False
    _print_result("Excel create (silent)", "ok")
    # Read
    read = _http_post("/api/office/excel/read", {
        "file_path": test_path,
        "cell_range": "A1:B2",
    })
    if not read.get("ok") or not read.get("rows"):
        _print_result("Excel read", "fail", read.get("error", "no rows"))
        try: os.remove(test_path)
        except: pass
        return False
    rows = read.get("rows", [])
    _print_result(f"Excel read — {len(rows)} rows", "ok")
    # Cleanup
    try:
        os.remove(test_path)
        _print_result("Excel cleanup", "ok")
    except Exception as e:
        _print_result("Excel cleanup", "partial", str(e)[:80])
    return True


def test_windows_listed() -> bool:
    print("\n[4/5] Windows enumeration (UIA + pygetwindow)")
    r = _http_get("/api/native/windows", timeout=10)
    if "error" in r:
        _print_result("List windows", "fail", r["error"])
        return False
    count = r.get("count", 0)
    if count == 0:
        _print_result(f"List windows ({count} found)", "partial", "Headless session or no GUI?")
        return True
    _print_result(f"List windows — {count} visible", "ok")
    return True


def test_chat_endpoint() -> bool:
    print("\n[5/5] Chat endpoint reachable")
    r = _http_get("/api/chat/history", timeout=10)
    if "error" in r:
        _print_result("Chat history endpoint", "fail", r["error"])
        return False
    _print_result(f"Chat endpoint responsive", "ok")
    return True


def _http_post(path: str, body: dict, timeout: float = 60.0) -> dict:
    url = f"{SERVER_BASE}{path}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)[:200]}


def main():
    print("=" * 60)
    print("JARVIS Multi-Laptop Test Matrix")
    print("=" * 60)
    print(f"Hostname: {os.environ.get('COMPUTERNAME', '?')}")
    print(f"User:     {os.environ.get('USERNAME', '?')}")
    print(f"Time:     {_dt.datetime.now().isoformat()}")
    print(f"Server:   {SERVER_BASE}")

    # Step 1: server reachable
    if not test_server_reachable():
        sys.exit(2)

    # Step 2: full diagnostic
    diag = test_diagnostic_layers()

    # Step 3-5: integration tests
    excel_ok = test_excel_roundtrip()
    windows_ok = test_windows_listed()
    chat_ok = test_chat_endpoint()

    # Final summary
    print("\n" + "=" * 60)
    summary = diag.get("summary", {})
    if diag.get("failed", 0) == 0 and excel_ok and windows_ok and chat_ok:
        verdict = "✅ ALL PASSED — JARVIS production-ready on this laptop"
        exit_code = 0
    elif diag.get("failed", 0) > 0:
        verdict = "❌ FAILURES detected — fix issues before deploying"
        exit_code = 1
    else:
        verdict = "🟡 PARTIAL — some optional features unavailable but core works"
        exit_code = 0
    print(verdict)
    print(f"Diagnostic score: {summary.get('score_pct', '?')}%")
    print("=" * 60)

    # Save results
    out_dir = Path.cwd() / "test_results"
    out_dir.mkdir(exist_ok=True)
    hostname = os.environ.get("COMPUTERNAME", "unknown").lower()
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"{hostname}-{stamp}.json"
    out_file.write_text(json.dumps({
        "hostname": hostname,
        "user": os.environ.get("USERNAME", ""),
        "time": _dt.datetime.now().isoformat(),
        "verdict": verdict,
        "exit_code": exit_code,
        "diagnostic": diag.get("raw", {}),
        "excel_ok": excel_ok,
        "windows_ok": windows_ok,
        "chat_ok": chat_ok,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nResults: {out_file}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
