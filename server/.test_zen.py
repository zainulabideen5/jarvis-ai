import sys
sys.path.insert(0, ".")
from app.services.laptop_control.files import FileOperations

q = "Zenesa Final website"
print(f"\n=== find_files('{q}') ===")
r = FileOperations.find_files(q)
print(f"Found: {len(r)}")
for x in r[:5]:
    try: print(f"  {x.get('name')} folder={x.get('is_folder')} path={x.get('path')}")
    except: pass

print(f"\n=== open_file('{q}') ===")
ok, msg = FileOperations.open_file(q)
print(f"  ok={ok}")
try: print(f"  msg={msg[:200]}")
except: print("  (emoji in msg)")

print(f"\n=== open_folder('{q}') ===")
ok, msg = FileOperations.open_folder(q)
print(f"  ok={ok}")
try: print(f"  msg={msg[:200]}")
except: print("  (emoji in msg)")
