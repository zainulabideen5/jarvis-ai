import sys
sys.path.insert(0, ".")
from app.services.laptop_control.intent import INTENT_PROMPT
print("Length:", len(INTENT_PROMPT))
print("Has ANY-NAME:", "<ANY-NAME>" in INTENT_PROMPT)
print("Has Zenesa Final:", "Zenesa Final" in INTENT_PROMPT)
