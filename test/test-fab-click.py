import sys, os
sys.path.insert(0, "/Users/louizen/Documents/aiagent/WorkBuddy/2026-10-29-15-22-56/qwenwork-account-switcher")
import tkinter as tk
import fab

root = tk.Tk()
f = fab.Fab(root)
picked = []
f.switch_to = lambda name, active: picked.append((name, active))   # 不真的切
f.open_panel()
root.update()

def descendants(w):
    out = []
    for c in w.winfo_children():
        out.append(c)
        out += descendants(c)
    return out

rows = [w for w in descendants(f.panel) if isinstance(w, tk.Frame) and w.cget("cursor") == "pointinghand"]
print("账号行数:", len(rows))
missing = 0
for row in rows:
    widgets = [row] + descendants(row)
    for w in widgets:
        if not w.bind("<Button-1>"):
            missing += 1
            print("  ✗ 无点击绑定:", w.__class__.__name__, w.winfo_name())
    print(f"  行元素 {len(widgets)} 个，全部有绑定: {missing == 0}")
print("结论:", "✓ 点任何位置都可切换" if missing == 0 else f"✗ {missing} 个控件点不动")
root.destroy()
