# CiRA CORE 1.9.4 on Windows, on the same PC as KitCheck (run_local.py):
#   python make_npj_win.py   -> kitcheck_script_node_win.py + kitcheck_station_win.npj (open it in CiRA)
# Flow: ButtonRun -> GstCam (http://127.0.0.1:8082/stream, KitCheck's camera relay) -> Python (YOLO11n + QR,
#       only while a scan waits; posts each result to http://127.0.0.1:8080/api/station/live) -> ButtonRun.
# Same script as on the server (kitcheck_script_node.py) with Windows paths; no MQTT node needed.
import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(os.path.expanduser("~"), "Documents", "KitCheck_AI", "cira_tools_yolo11n").replace("\\", "/")
KITCHECK = "http://127.0.0.1:8080"

src = open(os.path.join(HERE, "kitcheck_script_node.py"), encoding="utf-8").read()
for old, new in (("'http://172.17.0.1:8080/api/station/wanted'", repr(KITCHECK + "/api/station/wanted")),
                 ("'/home/cira/Models/cira_tools_yolo11n'", repr(MODEL_DIR)),
                 ("n.opt.num_threads = 2", "n.opt.num_threads = 4")):
    assert old in src, old
    src = src.replace(old, new)
# Hand each result to KitCheck over HTTP (on the server this went through the MqttPub node).
src = src.replace("payload['kitcheck'] = STATUS\n", """payload['kitcheck'] = STATUS
try:
\tbody = {k: payload[k] for k in ('detections', 'duration', 'ts', 'image_w', 'image_h') if k in payload}
\treq = urllib.request.Request(%r, json.dumps(body).encode(), {'Content-Type': 'application/json'})
\turllib.request.urlopen(req, timeout=2).close()
except Exception as e:
\tSTATUS['post_error'] = repr(e)[:200]
""" % (KITCHECK + "/api/station/live"))
src = src.replace("# ===================== KitCheck: YOLO11n + QR", "# ===================== KitCheck (Windows, same PC): YOLO11n + QR", 1)
with open(os.path.join(HERE, "kitcheck_script_node_win.py"), "w", encoding="utf-8") as f:
    f.write(src)


def nid():
    return '{%s}' % uuid.uuid4()


def node(i, model, x, y):
    return {'id': i, 'model': model, 'ngo': {'comment': {'data': '', 'show': False}},
            'position': {'fix': False, 'x': x, 'y': y}}


cam, py, btn, dbg = nid(), nid(), nid(), nid()
nodes = [
    node(btn, {'name': 'ButtonRun'}, 100, 60),
    node(cam, {'data': {'KitCheckCamera': {'customPipeline': '! multipartdemux ! jpegdec', 'encodeMode': 'H265',
                                           'frameRate': 5, 'timeout_s': 10,
                                           'url': 'http://127.0.0.1:8082/stream', 'useCustomPipeline': False}},
               'name': 'GstCam ', 'node_enable': True, 'setting': {'name': 'KitCheckCamera'}}, 100, 220),
    node(py, {'data': {'comment': 'KitCheck YOLO11n + QR', 'py_ver': '1. python3.12', 'script': src,
                       'tmp_path': os.path.join(os.path.expanduser("~"), "Documents", "KitCheck_AI").replace("\\", "/"),
                       'widget_state': {'editor_setting': {
                           'autoComplete': {'caseSensitivity': True, 'enable': True, 'threshold': 3},
                           'indent': {'enable': True}, 'theme': 'Material Dark',
                           'whitespace': {'enable': False, 'size': 2}}, 'splt0': 16, 'splt1': 10}},
              'height': 47, 'name': 'PythonScript', 'node_enable': True, 'python_code': src, 'width': 75},
         380, 220),
    node(dbg, {'height': 140, 'name': 'Debug', 'output_h': 0, 'output_v': 0, 'page_index': 0,
               'payload_h': 0, 'payload_v': 0, 'width': 225}, 660, 60),
]


def wire(src_id, dst):
    return {'in_id': dst, 'in_index': 0, 'out_id': src_id, 'out_index': 0}


scene = {'connections': [wire(btn, cam), wire(cam, py), wire(py, btn), wire(py, dbg)],
         'nodes': nodes,
         'scene_data': {'flow_view_fix': False, 'flow_view_pos_x': -60, 'flow_view_pos_y': -20,
                        'flow_view_zoom_factor': 0.8, 'scene_version': 1}}
out = os.path.join(HERE, "kitcheck_station_win.npj")
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'scene_array': [{'data': json.dumps(scene, indent=4) + '\n', 'name': 'Main'}]}, f, indent=4)
print('wrote', out)
