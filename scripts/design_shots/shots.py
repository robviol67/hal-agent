import os, sys, subprocess, tkinter as tk
from tkinter import ttk
S, OUT = sys.argv[1], sys.argv[2]
orig = tk.Tk.mainloop
def find(w, cls):
    out = []
    for c in w.winfo_children():
        if isinstance(c, cls): out.append(c)
        out += find(c, cls)
    return out
def ml(self, n=0):
    nbs = find(self, ttk.Notebook)
    top = nbs[0]; inner = [n for n in nbs[1:] if n.winfo_toplevel() is self][0]
    plan = [("01 Giano - File (elenco)", 0, 0, None), ("02 Giano - File (albero)", 0, 0, "albero"),
            ("03 Giano - File da decidere selezionato", 0, 0, "decidi"),
            ("04 Giano - Spazio", 0, 1, None), ("05 Giano - Attività", 0, 2, None), ("06 Giano - Impostazioni", 0, 3, None)]
    for i, t in enumerate(top.tabs()[1:], start=1):
        plan.append((f"{6 + i:02d} {top.tab(t, 'text')}", i, None, None))
    def step(k):
        if k >= len(plan): self.destroy(); return
        name, ti, si, extra = plan[k]
        top.select(ti)
        if si is not None: inner.select(si)
        tvs = [t for t in find(inner.nametowidget(inner.tabs()[0]), ttk.Treeview)]
        if extra == "albero":
            for r in find(self, ttk.Radiobutton):
                if r.cget("text") == "Albero": r.invoke()
        if extra == "decidi" and tvs:
            tv = tvs[0]
            def walk(p=""):
                for c in tv.get_children(p):
                    yield c; yield from walk(c)
            for c in walk():
                if "decidi" in tv.item(c, "tags") and not c.startswith("d:"):
                    p = tv.parent(c)
                    while p: tv.item(p, open=True); p = tv.parent(p)
                    tv.selection_set(c); tv.see(c); break
        self.after(1000, lambda: (self.update(), subprocess.run([sys.executable, S + "/winshot.py", str(os.getpid()), f"{OUT}/{name}.png"]), step(k + 1)))
    self.after(2500, lambda: step(0))
    orig(self, n)
tk.Tk.mainloop = ml
from hal_agent import panel_window
panel_window.open_panel()
