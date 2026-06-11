import sys
sys.path.insert(0, ".")
from app.services.chat import ChatService
tests = [
    "Zenesa Final website yaha file dekh rhi ha tujay",
    "Zenesa Final website kholo",
    "Zenesa Final website ko khol",
    "Zenesa Final website dikhao",
    "All website backup marks kholo",
    "Resume.pdf kholo",
]
for t in tests:
    r = ChatService._rule_based_file_intent(t)
    print(f"\n{t!r}")
    print(f"  -> {r}")
