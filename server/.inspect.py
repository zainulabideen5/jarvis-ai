import sys
sys.path.insert(0, ".")
import pywinauto
import pygetwindow as gw
print("Chrome windows scan:")
for w in gw.getAllWindows():
    if not w.title: continue
    tt = w.title.lower()
    if "chrome" not in tt: continue
    print(f"\n=== {w.title} ===")
    try:
        app = pywinauto.Application(backend="uia").connect(title=w.title, timeout=4)
        window = app.top_window()
        tabs = []
        for tab in window.descendants(control_type="TabItem"):
            try:
                nm = tab.element_info.name or ""
                if nm.strip(): tabs.append(nm)
            except Exception: pass
        print(f"Tabs: {tabs}")
    except Exception as e:
        print(f"UIA fail: {e}")
