"""Point the deployed flow's "LINE chat" / "LINE notify" Python+ nodes at files in ~/kitcheck_model, so later
changes need only a file copy + restart instead of editing code inside CiRA. Prints no node settings.
  sudo python3 patch_line_nodes.py /var/lib/docker/volumes/ciracore-home/_data/kitcheck_cira_ai.npj"""
import json
import shutil
import sys
import time

LOADER = ("#preparation here\n"
          "# KitCheck %s: the code lives in /home/cira/kitcheck_model/%s (edit there, then reopen/START the flow)\n"
          "exec(compile(open('/home/cira/kitcheck_model/%s', encoding='utf-8').read(), '%s', 'exec'))\n")
FILES = {"LINE chat": "kitcheck_line_chat.py", "LINE notify": "kitcheck_line_notify.py"}

path = sys.argv[1]
shutil.copy2(path, path + ".bak_%s" % time.strftime("%H%M%S"))
doc = json.load(open(path, encoding="utf-8"))
done = []
for sc in doc["scene_array"]:
    scene = json.loads(sc["data"])
    for n in scene["nodes"]:
        d = n.get("model", {}).get("data")
        if isinstance(d, dict) and d.get("caption") in FILES:
            f = FILES[d["caption"]]
            d["code"] = LOADER % (d["caption"], f, f, f)
            done.append(d["caption"])
    sc["data"] = json.dumps(scene, indent=4, ensure_ascii=False) + "\n"
json.dump(doc, open(path, "w", encoding="utf-8"), indent=4, ensure_ascii=False)
print("patched:", done)
