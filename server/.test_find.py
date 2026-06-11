import sys
sys.path.insert(0, ".")
from app.services.laptop_control.files import FileOperations
print("Test find_files('All website backup marks'):")
r = FileOperations.find_files("All website backup marks")
print(f"  Found: {len(r)}")
for x in r[:5]:
    print(f"    {x.get('name')} folder={x.get('is_folder')} path={x.get('path')}")
print("\nTest open_folder('All website backup marks'):")
ok, msg = FileOperations.open_folder("All website backup marks")
print(f"  ok={ok}, msg={msg}")
