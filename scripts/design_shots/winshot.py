import sys, subprocess, Quartz
pid = int(sys.argv[1]); out = sys.argv[2]
for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID):
    if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0 and w.get("kCGWindowName") == "HAL Agent — Pannello":
        subprocess.run(["screencapture", "-x", "-o", "-l", str(w["kCGWindowNumber"]), out]); print("ok", w["kCGWindowNumber"]); break
else:
    print("nessuna finestra")
