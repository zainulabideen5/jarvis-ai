import sys, asyncio
sys.path.insert(0, ".")
from app.services.laptop_control import LaptopController
from app.core.config import ServerConfig
ctrl = LaptopController(ServerConfig())
# Test various param combinations the LLM might produce
tests = [
    {"action": "open_folder", "params": {"name": "All website backup marks"}},
    {"action": "open_folder", "params": {"name": "All website backup marks", "location": "desktop"}},
    {"action": "open_folder", "params": {"path": "All website backup marks"}},
    {"action": "find_file", "params": {"query": "All website backup marks"}},
]
for t in tests:
    print(f"\nTest: {t}")
    try:
        ok, msg = ctrl._dispatch(t["action"], t["params"])
        try:
            print(f"  RESULT: ok={ok}")
            print(f"  MSG: {msg[:200] if isinstance(msg, str) else msg}")
        except UnicodeEncodeError:
            print(f"  RESULT: ok={ok} (msg has emoji)")
    except Exception as e:
        print(f"  EXCEPTION: {e}")
