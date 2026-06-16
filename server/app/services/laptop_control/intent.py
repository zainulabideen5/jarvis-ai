"""Command intent detector — parse natural language into structured actions."""

from __future__ import annotations

import json
import re

from app.core.llm import LLMClient

from app.core.config import ServerConfig
from app.core.logging import get_logger

log = get_logger(__name__)

INTENT_PROMPT = """Tu ek laptop command parser hai. User Roman Urdu + English mein laptop pe kuch karne ko bola hai. Use structured JSON array mein convert kar.

Available actions:

1. "open_app" — App kholo. Example: "chrome kholo", "calculator open kar"
   params: {"app": "chrome|teams|whatsapp|excel|word|notepad|gmail|youtube|linkedin|spotify|vscode|outlook|powerpoint|calculator|cmd|explorer|..."}

2. "close_app" — App band karo. params: {"app": "..."}

3. "send_teams_message" — Teams DM bhej (text + optional file/document/photo).
   Example text: "Teams pe Subhan ko message bhej hello"
   Example file: "Teams pe Subhan ko logo.png bhejo"
   Example file+caption: "Teams pe Subhan ko resume.pdf bhejo with caption 'mera CV'"
   params: {"recipient": "name", "message": "text or empty", "attachment": "filename or full path or empty"}

4. "send_whatsapp_message" — WhatsApp pe message (text + optional file/document/photo). PREFER phone number (safer).
   Example text: "WhatsApp pe <number> ko <text> bhej"
   Example file: "WhatsApp pe <name> ko <file> bhej"
   Example file+caption: "WhatsApp pe <number> ko <file> bhej caption '<caption>'"
   Example Business: "WhatsApp Business pe <name> ko bhej <text>" → set "business":true
   Example Business: "business whatsapp pe <name> ko <file> bhej" → set "business":true
   Example Behind: "WhatsApp pe <name> ko background mein bhej <text>" → set "behind_mode":true
   Example Behind: "WhatsApp pe <name> ko hidden bhej <text>" → set "behind_mode":true
   Example Behind: "WhatsApp pe <name> ko chupke se bhej <text>" → set "behind_mode":true
   The user can use ANY name or number — extract whatever they typed into "recipient".
   If user explicitly mentions "WhatsApp Business" / "Business WhatsApp" / "WA Business" → set "business":true.
   If user explicitly mentions "background" / "behind" / "chupke" / "hidden" / "invisible" → set "behind_mode":true.
   Otherwise omit those flags (defaults: regular WhatsApp, visible foreground).
   params: {"recipient": "<phone number or name as user typed>", "message": "text or empty", "attachment": "filename or full path or empty", "business": true only if explicit Business mention, "behind_mode": true only if explicit background/behind/hidden mention}

5. "send_email" — Email bhej (multi-recipient + optional attachments + optional from-account).
   to: list of emails or comma-separated. Names ho to system DB se email lookup hoga.
   attachments: list of filenames or paths. Bare filenames laptop pe dhoondhe jaayenge.
   from_account: OPTIONAL sender account hint (only when user explicitly specifies sender).
     Can be:
       - email substring: "ahmed.business@gmail.com" or "ahmed.business"
       - account index: "u/0", "u/1", "0", "1", "first", "second", "2nd", "3rd"
     If user does NOT specify a sender, OMIT this field — Gmail's default (u/0) is used.
   Example single: "ahmed@example.com ko email bhej subject 'Meeting' body 'Kal 5 baje'"
   Example multi:  "ahmed@x.com aur sara@y.com dono ko email bhej..."
   Example file:   "ahmed ko email karo proposal.pdf attached subject 'Proposal'"
   Example from:   "business wale account se ahmed ko email bhejo"
                   → {"to": "ahmed", "subject": "...", "body": "...", "from_account": "business"}
   Example index:  "second account se ye email bhejo"
                   → {"to": "...", ..., "from_account": "1"}   (second = index 1, u/1)
   params: {"to": ["email1", "email2"] or "name", "subject": "...", "body": "...", "attachments": ["file1.pdf"], "from_account": "optional"}

6. "find_file" — File search. Example: "logo file dhondo", "PDF dikhao desktop pe"
   params: {"query": "filename or keyword", "location": "desktop|documents|downloads or null for all"}

7. "open_file" — File kholo ACTUAL APP mein (Notepad, Word, Browser, etc.) — file laptop pe khulegi.
   Use kab: "kholo", "open kar", "chalu kar", "launch kar" — bina "padho/dikhao/summary"
   Example: "note.txt kholo", "resume.pdf kholo", "logo.png kholo"
   params: {"path": "filename or full path"}

7b. "open_folder" — Folder kholo Explorer mein.
    Example: "Downloads folder kholo", "Templates folder pe le ja"
    params: {"name": "folder name", "location": "desktop|documents|downloads or null"}

8. "read_file" — File ka CONTENT chatbot mein dikhao ya summarize karo. File laptop pe NAHI khulegi.
   Use kab: "padho", "content batao", "dikhao kya likha hai", "summary do", "read kar"
   Example: "is PDF ka summary do", "report.txt mein kya likha hai padho"
   params: {"path": "...", "action": "read|summarize"}

9. "create_file" — Nayi text/Word/Excel file bana.
   params: {"type": "txt|docx|xlsx", "name": "filename", "content": "...", "location": "desktop|documents|downloads"}

10. "create_folder" — Naya folder bana. Example: "desktop pe naya folder bana zain naam ka"
    params: {"name": "folder name", "location": "desktop|documents|downloads"}

11. "edit_excel" — Excel mein row add. params: {"file": "...", "action": "add_row", "data": ["...", "..."]}

12. "move_file" — File move. params: {"source": "...", "dest": "..."}

13. "delete_file" — File ya folder delete. Detect kind from user words.
    Example: "zain.txt file delete kar" → kind: "file"
    Example: "test folder delete kar" → kind: "folder"
    params: {"path": "name or full path", "kind": "file|folder|auto"}

14. "screenshot" — Screen pe dekho/analyze. Example: "screen pe kya dikh raha", "current window batao"
    params: {"query": "what to look for"}

15. "system_command" — Volume, lock, shutdown.
    params: {"command": "volume_up|volume_down|volume_mute|lock|sleep|shutdown|restart|wifi_off|wifi_on|cancel_shutdown"}

16. "open_url" — Specific URL/website kholo browser mein.
    Use kab: user URL/link de ya kisi specific website ka naam le ("marriott.com kholo", "yeh site khol", "github.com pe ja")
    params: {"url": "https://..."}

16b. "trello_create_card" — Naya Trello card banao.
    params: {"title": "...", "list": "Doing|Todo|Done|...", "board": "optional board name", "description": "optional", "due": "optional ISO date"}
    Example: "Trello pe Doing list mein 'Fix login bug' card banao"

16c. "trello_move_card" — Card ko alag list mein move karo.
    params: {"card": "card name or partial", "to_list": "Done|Doing|...", "board": "optional"}
    Example: "Trello pe 'login bug' card ko Done mein move kar"

16d. "trello_comment" — Card pe comment add karo.
    params: {"card": "card name", "comment": "text", "board": "optional"}
    Example: "Trello mein 'login bug' card pe comment add: 'kal complete'"

16e. "trello_list" — Cards list karo (board ka, ya specific list ka).
    params: {"board": "optional", "list": "optional list filter"}
    Example: "Trello board pe kya tasks pending hain", "Doing list mein kya hai"

17. "web_search" — Server-side web search. Use SIRF in 2 cases:
    (a) User EXPLICITLY search bole — "google pe search karo X", "X online dhundo", ya
    (b) TRULY LIVE / CURRENT data jo badalta rehta hai — aaj ka price/rate, abhi ka
        mausam, aaj ki news / sports score, stock price, available jobs, kisi shop/
        hotel/restaurant ki current listing/rating.
    ⛔ DO NOT web_search GENERAL KNOWLEDGE — geography ("sabse bada shehar", capitals),
       history, science, definitions, medicine basics, math, how-to, general advice.
       Yeh "chat" action hai — chat AI (strong model, Claude) khud confidently jawab
       deta hai. web_search sirf LIVE/changing data ya EXPLICIT search ke liye.

    ⛔ DO NOT use web_search for questions about USER'S OWN data — meetings, tasks, clients,
    calendar events, transcriptions, memory, history. Phrases like "X meeting mein kya hua",
    "kal wali meeting ke tasks batao", "kitne pending tasks hain", "<naam> client kaun hai",
    "Ahmed ke baare mein batao", "yesterday ka summary" — yeh sab "chat" action hain because
    the chat brain has local DB context injected (meetings, tasks, clients) and will answer
    directly. NEVER web_search someone's name to look them up — that's the chat AI's job
    using the clients DB.

    Results chat mein numbered list format mein dikhenge — user phir "<n> wala kholo" bol kar
    open_url use kar sakta hai.
    params: {"query": "search query text — keep it concise"}

18. "chat" — Sirf casual baat hai, koi laptop action nahi (hello, kaisa hai, thanks).
    params: {}

19. "excel_formula" — Excel mein formula run karke result. SILENT (window nahi dikhegi).
    Example: "Excel mein column D ka sum nikalo file: sales.xlsx"
    params: {"file_path": "...", "sheet": "", "target_cell": "A11", "formula": "=SUM(D:D)"}

20. "excel_append" — Excel mein nayi row add karo. SILENT.
    Example: "Excel sales.xlsx mein row add: Ahmed, 5000, paid"
    params: {"file_path": "...", "sheet": "", "row": ["v1","v2","v3"]}

21. "excel_create" — Naya Excel file banao with headers + rows. SILENT.
    params: {"file_path": "...", "headers": ["Name","Amt"], "rows": [["A",100],["B",200]]}

22. "word_create" — Naya Word document banao. SILENT.
    Example: "Word mein meeting notes likho file: notes.docx"
    params: {"file_path": "...", "content": "text", "title": ""}

23. "word_append" — Existing Word doc mein text add. SILENT.
    params: {"file_path": "...", "text": "..."}

24. "outlook_send" — Outlook desktop se email bhej. SILENT.
    Example: "Outlook se Ahmed ko meeting confirm bhej"
    params: {"to": "email or list", "subject": "", "body": "", "cc": "", "attachments": []}

24a. "gmail_detect_accounts" — Chrome mein logged-in Gmail accounts scan karo.
    Example: "Gmail accounts detect karo", "saari Gmail dhundo Chrome mein"
    params: {}

24b. "gmail_set_label" — Account index ko label assign karo.
    Example: "Account 0 ko Personal label do", "u/1 ko Work bana"
    params: {"index": 0, "label": "Personal", "email": "optional"}

24c. "gmail_list_accounts" — Saari labeled accounts dikhao.
    Example: "Gmail accounts dikhao", "kaunse Gmail set hain"
    params: {}

25. "gmail_send_labeled" — Multi-Gmail accounts mein se label-wise send.
    Example: "Personal Gmail se Ahmed ko bhej meeting kal 3pm"
    Example: "Work Gmail se client@x.com ko proposal bhej"
    params: {"label": "Personal|Work|Client A|...", "to": "...", "subject": "", "body": "", "attachment": ""}

26. "notepad_save" — Notepad mein likh ke save karo.
    Example: "Notepad mein meeting notes likh aur desktop pe save kar"
    params: {"content": "text", "save_path": "C:/path/file.txt"}

27. "calculator_compute" — Windows Calculator se math nikalo.
    Example: "Calculator se 1500 * 7 nikalo"
    params: {"expression": "1500*7"}

28. "settings_open" — Windows Settings panel kholo.
    Example: "Wifi settings kholo", "Bluetooth on karo settings"
    params: {"panel": "wifi|bluetooth|display|sound|apps|battery|update|privacy|main"}

29. "explorer_open" — File Explorer kholo specific path pe.
    Example: "Documents folder kholo", "Desktop kholo Explorer mein"
    params: {"path": "C:/Users/.../Documents"}

30. "native_hotkey" — Keyboard combo press karo (Ctrl+S, Alt+F4, etc).
    Example: "Ctrl+S press kar", "Alt+Tab kar"
    params: {"keys": ["ctrl","s"]}

31. "image_click" — Photoshop/visual apps mein saved button image se click.
    Example: "Photoshop mein new_layer button click kar"
    params: {"template": "photoshop/new_layer", "confidence": 0.85}

CRITICAL RULES:
- Roman Urdu, Hindi, English sab samajh
- Multiple actions ho sakte hain — sab ek array mein do
- Agar message MISSING hai (sirf "Subhan ko bhej" — bina text), to "chat" return kar — galat blank message mat bhej
- Agar clear nahi to "chat" return kar
- SIRF valid JSON array return kar — koi explanation nahi
- Locations: "desktop", "documents", "downloads" — lowercase mein hi rakh

Examples:

User: "chrome kholo"
Output: [{"action":"open_app","params":{"app":"chrome"}}]

User: "desktop pe naya folder bana 'projects' naam ka"
Output: [{"action":"create_folder","params":{"name":"projects","location":"desktop"}}]

User: "Subhan ko Teams pe message bhej — meeting 5 baje"
Output: [{"action":"send_teams_message","params":{"recipient":"Subhan","message":"meeting 5 baje"}}]

User: "Teams pe Ahmed ko logo.png bhejo"
Output: [{"action":"send_teams_message","params":{"recipient":"Ahmed","message":"","attachment":"logo.png"}}]

User: "WhatsApp pe Saif ko resume.pdf bhejo caption 'mera CV'"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"Saif","message":"mera CV","attachment":"resume.pdf"}}]

User: "Subhan ko teams pe contract.docx bhejo with note 'sign karwana'"
Output: [{"action":"send_teams_message","params":{"recipient":"Subhan","message":"sign karwana","attachment":"contract.docx"}}]

PRONOUN vs RECIPIENT (CRITICAL — applies to ANY name):
When user uploads a file via the dashboard paperclip, their message may contain BOTH a pronoun
(for the file) AND a real name (the recipient). NEVER pick the pronoun as recipient.

Pronouns (these are NEVER a recipient — they refer to the attached file or a topic):
  Ais, ais, Is, is, isko, isay, yeh, ye, wo, woh, that, this, ye logo, ye file,
  ye image, ye photo, ye document, yaha (when used like "yaha file hai")

The RECIPIENT is the NAME or NUMBER in the message — could be:
  • Any first name (Ahmed, Sara, Subhan, Hamza, Zain, Saif, Muzzamil, Zaid, Aamir,
    Kashif, Imran, Anum, Mariam, etc — ANY name)
  • Any business/role label that's in the clients DB
  • A phone number (Pakistani: 03xx, +92xx, or 11 digits)
  • An email address

Pattern detection:
  "<file-pronoun> ko <NAME> ka {whatsapp|teams|email} kr/bhej"  → recipient = <NAME>
  "<NAME> ko ye/yeh/is {whatsapp|teams|email} pe bhej"          → recipient = <NAME>
  "{whatsapp|teams|email} pe <NAME> ko ye/is bhejo"             → recipient = <NAME>

Examples (deliberately varied names — pattern matters, not the specific name):

User (with file): "yaha logo ha Ais ko zain ka whatsapp kr da"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"zain","message":""}}]

User (with file): "yeh image Ahmed ko teams pe bhej"
Output: [{"action":"send_teams_message","params":{"recipient":"Ahmed","message":""}}]

User (with file): "is ko Sara ka email kr"
Output: [{"action":"send_email","params":{"to":"Sara","subject":"(no subject)","body":""}}]

User (with file): "yeh contract ha is ko Subhan ka whatsapp pa bhej da"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"Subhan","message":""}}]

User (with file): "Hamza ko ye file bhejo whatsapp pe"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"Hamza","message":""}}]

User (with file): "ye doc Mariam ko teams pe send kar"
Output: [{"action":"send_teams_message","params":{"recipient":"Mariam","message":""}}]

User (with file): "yaha pe pdf ha Kashif ka email kr da"
Output: [{"action":"send_email","params":{"to":"Kashif","subject":"(no subject)","body":""}}]

User (with file, phone number): "ye 03001234567 ko whatsapp pe bhej"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"03001234567","message":""}}]

User (with file, ONLY pronoun, no name AND no recent recipient in context):
"is ko bhej do"
Output: [{"action":"chat","params":{}}]   # ambiguous — fall through to chat to ask for clarification

User: "ahmed@example.com ko email bhej subject 'Meeting' body 'Kal 5 baje aana'"
Output: [{"action":"send_email","params":{"to":"ahmed@example.com","subject":"Meeting","body":"Kal 5 baje aana"}}]

User: "ahmed@x.com aur sara@y.com dono ko email karo proposal.pdf attached subject 'Q4 Proposal' body 'Review kar lijiye'"
Output: [{"action":"send_email","params":{"to":["ahmed@x.com","sara@y.com"],"subject":"Q4 Proposal","body":"Review kar lijiye","attachments":["proposal.pdf"]}}]

User: "Ahmed ko email bhej proposal.pdf"
Output: [{"action":"send_email","params":{"to":"Ahmed","subject":"Proposal","body":"","attachments":["proposal.pdf"]}}]

User: "Trello pe Doing list mein 'Fix login bug' card banao"
Output: [{"action":"trello_create_card","params":{"title":"Fix login bug","list":"Doing"}}]

User: "Trello mein 'login bug' card ko Done mein move kar"
Output: [{"action":"trello_move_card","params":{"card":"login bug","to_list":"Done"}}]

User: "Trello board pe kya pending hai"
Output: [{"action":"trello_list","params":{}}]

User: "'website redesign' card pe comment add: 'kal complete'"
Output: [{"action":"trello_comment","params":{"card":"website redesign","comment":"kal complete"}}]

User: "logo file dhondo"
Output: [{"action":"find_file","params":{"query":"logo"}}]

User: "screen pe dekho kya khula hai"
Output: [{"action":"screenshot","params":{"query":"current window"}}]

User: "note.txt kholo"
Output: [{"action":"open_file","params":{"path":"note.txt"}}]

User: "zaid wala text file kholo Downloads mein"
Output: [{"action":"open_file","params":{"path":"zaid"}}]

User: "Ali ki resume khol do"
Output: [{"action":"open_file","params":{"path":"Ali resume"}}]

User: "report.txt mein kya likha hai"
Output: [{"action":"read_file","params":{"path":"report.txt","action":"read"}}]

User: "resume.pdf ka summary do"
Output: [{"action":"read_file","params":{"path":"resume.pdf","action":"summarize"}}]

User: "yr kaisa hai tu"
Output: [{"action":"chat","params":{}}]

User: "kitne tasks pending hain"
Output: [{"action":"chat","params":{}}]

User: "Muzzamil meeting mein kya hua tha"
Output: [{"action":"chat","params":{}}]

User: "kal wali meeting ke tasks batao"
Output: [{"action":"chat","params":{}}]

User: "Ahmed kaun hai" (a client lookup, local)
Output: [{"action":"chat","params":{}}]

User: "latest meeting summary do"
Output: [{"action":"chat","params":{}}]

User: "https://github.com kholo"
Output: [{"action":"open_url","params":{"url":"https://github.com"}}]

User: "yeh site khol https://www.movenpick.com/karachi"
Output: [{"action":"open_url","params":{"url":"https://www.movenpick.com/karachi"}}]

User: "google pe search kar Defence Karachi best hotel"
Output: [{"action":"web_search","params":{"query":"Defence Karachi best hotel"}}]

User: "iPhone 15 Pro ka price Pakistan mein"
Output: [{"action":"web_search","params":{"query":"iPhone 15 Pro price Pakistan"}}]

User: "Lahore mein car repair shops batao"
Output: [{"action":"web_search","params":{"query":"Lahore car repair shops"}}]

User: "PSL final 2026 score kya hua"
Output: [{"action":"web_search","params":{"query":"PSL final 2026 score"}}]

User: "best laptop 1 lakh ke andar 2026"
Output: [{"action":"web_search","params":{"query":"best laptop under 1 lakh 2026 Pakistan"}}]

User: "panadol ka use kya hota hai"   (general knowledge → chat AI khud jawab de)
Output: [{"action":"chat"}]

User: "Pakistan ka sabse bada shehar kaunsa hai"   (general knowledge → chat)
Output: [{"action":"chat"}]

User: "abhi Karachi mein mausam kaisa hai"
Output: [{"action":"web_search","params":{"query":"Karachi weather right now"}}]

CONTEXT-AWARE PARSING (very important):
The PRIMARY context is "RECENT SEND ACTIONS" below — it's structured data of who/what/where the last messages went. Use it FIRST.
The conversation history is secondary backup context.

Rules:
- Pronouns: "is ko", "ais ko", "aise", "uss ko", "isko", "usko", "wahi", "wo", "same", "isi", "usi", "him", "her" → reuse the recipient from the LAST relevant send action.
- Implicit platform: "phir bhej", "again", "wapas", "dobara", "same se", "us pe", "isi pe" — without specifying whatsapp/teams/email → reuse the platform from the LAST send action.
- If the last recipient was a PHONE NUMBER (is_phone=true), KEEP using that exact phone number — do NOT replace it with a name.
- "Number" / "naam" disambiguation: if user explicitly says "naam se" or gives a different name, override; otherwise inherit.
- If multiple send actions in context and user says "Zaid ko phir bhej" — match Zaid in the recent list and use that platform.
- If RECENT SEND ACTIONS is empty AND the message has only a pronoun, return chat (ask for clarification).

RECENT SEND ACTIONS (most recent first — JSON):
__RECENT_ACTIONS__

CONVERSATION HISTORY (most recent last):
__HISTORY__

Examples (with context):

Recent: [{"platform":"whatsapp","recipient":"+923032316818","is_phone":true,"message_preview":"hello saif"}]
User: "isi ko 'kal aana' bhej"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"+923032316818","message":"kal aana"}}]

Recent: [{"platform":"whatsapp","recipient":"+923032316818","is_phone":true,"message_preview":"hello saif"}]
User: "Ais ko again message kr im coming today"
Output: [{"action":"send_whatsapp_message","params":{"recipient":"+923032316818","message":"im coming today"}}]

Recent: [{"platform":"teams","recipient":"Zaid Moeen","is_phone":false,"message_preview":"hello"}]
User: "wahi ko phir bhej 'kal milte hain'"
Output: [{"action":"send_teams_message","params":{"recipient":"Zaid Moeen","message":"kal milte hain"}}]

Recent: [{"platform":"whatsapp","recipient":"Saif","is_phone":false,"message_preview":"hi"},{"platform":"teams","recipient":"Zaid","is_phone":false,"message_preview":"yo"}]
User: "Zaid ko phir bhej 'salam'"
Output: [{"action":"send_teams_message","params":{"recipient":"Zaid","message":"salam"}}]

Recent: []
User: "isko bhej hello"
Output: [{"action":"chat","params":{}}]

User: "Excel mein sales.xlsx ke column D ka sum nikalo cell E1 mein"
Output: [{"action":"excel_formula","params":{"file_path":"sales.xlsx","sheet":"","target_cell":"E1","formula":"=SUM(D:D)"}}]

User: "Word mein meeting notes likh aur desktop pe notes.docx save kar — content: Action items reviewed"
Output: [{"action":"word_create","params":{"file_path":"desktop/notes.docx","content":"Action items reviewed","title":""}}]

User: "Outlook se ahmed@x.com ko bhej subject 'Meeting' body 'Kal 3pm pe'"
Output: [{"action":"outlook_send","params":{"to":"ahmed@x.com","subject":"Meeting","body":"Kal 3pm pe"}}]

User: "Personal Gmail se Ali ko meeting kal 3pm bhej subject 'sync'"
Output: [{"action":"gmail_send_labeled","params":{"label":"Personal","to":"Ali","subject":"sync","body":"meeting kal 3pm"}}]

User: "Work Gmail se client@x.com ko proposal.pdf bhejo"
Output: [{"action":"gmail_send_labeled","params":{"label":"Work","to":"client@x.com","subject":"","body":"","attachment":"proposal.pdf"}}]

User: "Notepad mein 'meeting summary' likh ke desktop pe save kar summary.txt"
Output: [{"action":"notepad_save","params":{"content":"meeting summary","save_path":"desktop/summary.txt"}}]

User: "Calculator se 1500 * 7 nikalo"
Output: [{"action":"calculator_compute","params":{"expression":"1500*7"}}]

User: "Wifi settings kholo"
Output: [{"action":"settings_open","params":{"panel":"wifi"}}]

User: "Documents folder kholo"
Output: [{"action":"explorer_open","params":{"path":"shell:DocumentsFolder"}}]

# IMPORTANT — File/folder commands:
# User can use ANY folder/file name. Examples below use placeholder names
# (MyFolder, MyFile) — replace with the ACTUAL name from the user's message.
# Words like "kholo", "khol", "dekh", "dikhao", "yaha file/folder" all mean
# OPEN. If no file extension (.pdf/.docx etc.) given, treat as folder.

User: "MyFolder kholo"
Output: [{"action":"open_folder","params":{"name":"MyFolder"}}]

User: "MyFolder ko khol"
Output: [{"action":"open_folder","params":{"name":"MyFolder"}}]

User: "MyFolder dikhao mujhay"
Output: [{"action":"open_folder","params":{"name":"MyFolder"}}]

User: "MyFolder yaha file dekh rhi ha tujay"
Output: [{"action":"open_folder","params":{"name":"MyFolder"}}]

User: "MyFile.pdf kholo"
Output: [{"action":"open_file","params":{"path":"MyFile.pdf"}}]

User: "MyFile.docx find kar"
Output: [{"action":"find_file","params":{"query":"MyFile.docx"}}]

User: "Photoshop mein new_layer button click kar"
Output: [{"action":"image_click","params":{"template":"photoshop/new_layer"}}]

User command: __COMMAND__

Output (JSON array only, no markdown, no explanation):"""


# Deterministic pre-classifier — if a message is OBVIOUSLY a question about the
# user's local data (meetings, tasks, clients, summary), return chat directly
# instead of asking the LLM. Earlier the LLM kept routing "Muzzamil meeting mein
# kya hua" to web_search because it saw a proper-noun-like word, even though
# the prompt forbade it. A keyword guard is more reliable than prompt rules.
_LOCAL_DATA_KEYWORDS = (
    "meeting", "meatings", "meetings",
    "task", "tasks",
    "client", "clients",
    "summary", "transcription", "transcriptions",
    "memory", "yaad",
    "activity", "history",
    "verification",
)
_QUESTION_MARKERS = (
    "?", "kya", "kaun", "kaise", "kahan", "kab", "kyun", "kyu",
    "batao", "batoa", "batao", "dikhao", "bata", "show",
    "kitne", "kitna",
    "kar raha", "what", "who", "how", "when", "where", "why",
    "list", "all",
)


def _is_local_data_question(message: str) -> bool:
    """Returns True if the user is clearly asking about their local DB data."""
    if not message:
        return False
    low = message.lower()
    # Has a local-data noun
    has_local = any(k in low for k in _LOCAL_DATA_KEYWORDS)
    if not has_local:
        return False
    # Has a question/listing marker. Pad with spaces for clean word-boundary match.
    padded = f" {low} "
    has_question = any(
        (f" {q} " in padded if q.isalpha() else q in low)
        for q in _QUESTION_MARKERS
    )
    return has_question


class IntentDetector:
    """Classifies chat messages into laptop actions."""

    def __init__(self, config: ServerConfig):
        self._config = config
        self._client = None

    def _get_client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(self._config)
        return self._client

    def detect(
        self,
        message: str,
        history: list[dict] | None = None,
        recent_actions: list[dict] | None = None,
    ) -> list[dict]:
        """Detect intent. Returns list of actions or [{"action": "chat"}] for normal chat.

        history: recent {"role", "content"} chat turns — used as backup context.
        recent_actions: structured list of recent successful send actions
            ({"platform", "recipient", "is_phone", "message_preview"}) — used as
            PRIMARY context for resolving pronouns ("ais ko", "wahi") and platform
            inheritance ("again", "phir").
        """
        if not message or len(message.strip()) < 2:
            return [{"action": "chat", "params": {}}]

        # Short-circuit: clear questions about local data go straight to chat,
        # bypassing LLM intent. The chat brain has meetings/tasks/clients context.
        if _is_local_data_question(message):
            log.info("intent_local_data_shortcut", message=message[:80])
            return [{"action": "chat", "params": {}}]

        client = self._get_client()
        history_block = self._format_history(history)
        recent_block = self._format_recent_actions(recent_actions)
        prompt = (
            INTENT_PROMPT
            .replace("__RECENT_ACTIONS__", recent_block)
            .replace("__HISTORY__", history_block)
            .replace("__COMMAND__", message[:500])
        )

        # Intent needs a stronger reasoner. Prefer 70b; if its keys are all
        # rate-limited the call will fail and we fall back to the configured
        # groq_model (typically 8b). The LLMClient's own multi-key rotation
        # handles per-model quota across keys; we just pick the model here.
        MODELS_TO_TRY = ["llama-3.3-70b-versatile"]
        if self._config.groq_model and self._config.groq_model not in MODELS_TO_TRY:
            MODELS_TO_TRY.append(self._config.groq_model)

        last_error = None
        for model in MODELS_TO_TRY:
            # One retry per model, only for transient connection errors
            for attempt in range(2):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.0,
                        max_tokens=500,
                        timeout=15.0,
                    )
                    content = response.choices[0].message.content.strip()
                    actions = self._parse_json(content)

                    if not actions:
                        return [{"action": "chat", "params": {}}]

                    valid = []
                    for a in actions:
                        if not isinstance(a, dict) or not a.get("action"):
                            continue
                        if "params" not in a:
                            a["params"] = {}
                        valid.append(a)

                    if not valid:
                        return [{"action": "chat", "params": {}}]

                    log.info("intent_detected", count=len(valid), first=valid[0].get("action"), model=model)
                    return valid

                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    # Retry once on transient connection errors
                    if attempt == 0 and ("connection" in err_str or "timeout" in err_str):
                        import time as _t
                        _t.sleep(1.5)
                        continue
                    # Non-connection error — break out of retry loop. If it's
                    # a quota/model error, the outer loop will try the next
                    # model.
                    break
            # After 2 retries exhausted for this model, log and try next
            log.info("intent_model_failed_trying_next", model=model, error=str(last_error)[:100])

        log.warning("intent_detection_failed", error=str(last_error)[:200])
        return [{"action": "chat", "params": {}}]

    @staticmethod
    def _format_recent_actions(recent: list[dict] | None) -> str:
        """Render recent send actions as a JSON list the LLM can reason over."""
        if not recent:
            return "[]"
        compact = []
        for r in recent[:5]:
            compact.append({
                "platform": r.get("platform", ""),
                "recipient": r.get("recipient", ""),
                "is_phone": bool(r.get("is_phone", False)),
                "message_preview": (r.get("message_preview") or "")[:80],
            })
        return json.dumps(compact, ensure_ascii=False)

    @staticmethod
    def _format_history(history: list[dict] | None) -> str:
        """Format last few turns for the intent prompt. Keeps it short to control token cost."""
        if not history:
            return "(no prior turns)"
        recent = history[-8:]  # Last 4 user/assistant exchanges
        lines = []
        for h in recent:
            role = h.get("role", "")
            content = (h.get("content") or "").strip()
            if not content:
                continue
            # Truncate very long messages — only the gist matters for context
            if len(content) > 240:
                content = content[:240] + "…"
            tag = "User" if role == "user" else "AI"
            lines.append(f"{tag}: {content}")
        return "\n".join(lines) if lines else "(no prior turns)"

    @staticmethod
    def _parse_json(content: str) -> list[dict]:
        """Parse JSON array from LLM response."""
        if "```" in content:
            match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if match:
                content = match.group(1).strip()

        try:
            result = json.loads(content)
            if isinstance(result, list):
                return [a for a in result if isinstance(a, dict) and a.get("action")]
            if isinstance(result, dict) and result.get("action"):
                return [result]
        except json.JSONDecodeError:
            pass

        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list):
                    return [a for a in result if isinstance(a, dict) and a.get("action")]
            except json.JSONDecodeError:
                pass

        # All parse strategies failed — log a preview so we can see what the LLM
        # actually returned and why it didn't match any of our patterns. Without
        # this the intent silently downgrades to "chat" and the bug is invisible.
        log.warning(
            "intent_json_parse_failed",
            preview=(content[:200] + "…") if len(content) > 200 else content,
        )
        return []
