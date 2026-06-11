"""Test: 5 log ki meeting se tasks extract"""
from app.services.task_extractor import TaskExtractor
from app.core.config import ServerConfig

config = ServerConfig()
extractor = TaskExtractor(config)

meeting = (
    "Ahmed kal tak logo design ready karo urgent hai. "
    "Website ka payment pending hai 50 hazaar. "
    "Mujhe hosting ka access chahiye kal tak. "
    "Hamza invoice bhej do client ko email pe. "
    "Meeting Friday ko 3 baje rakhte hain. "
    "Database backup lena hai aaj raat tak. "
    "Social media posts schedule karne hain next week. "
    "Sara presentation ready karo parson tak board meeting ke liye. "
    "Server upgrade karna hai weekend pe. "
    "Client ko proposal bhejni hai kal subah tak."
)

print("=== 5 Log Ki Meeting - Tasks Test ===")
print()
tasks = extractor.extract_tasks(meeting, "Zoom Meeting")
print(f"Total Tasks: {len(tasks)}")
print()
for i, t in enumerate(tasks):
    title = t.get("title", "?")
    assigned = t.get("assigned_to", "Unassigned")
    priority = t.get("priority", "medium")
    print(f"  {i+1}. {title}")
    print(f"     Assigned: {assigned} | Priority: {priority}")
    print()
