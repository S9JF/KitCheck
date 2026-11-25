# Build kitcheck_station.npj for CiRA CORE 1.9.4, using the format of a project saved by CiRA itself.
#   python3 make_npj.py <kitcheck_snippet.py> <kitcheck_pythonplus.py> <out.npj>
# Flow: ButtonRun -> GstCam -> Python (YOLO11n + QR, only while a scan waits) -> MqttPub (172.17.0.1:1884,
#       kitcheck/station/live) and -> Debug (payload); Python -> ButtonRun closes the loop.
import json
import sys
import uuid

# The "Python" (PythonScript) node runs the script per frame on globals payload/img; CiRA stores the
# same text in data.script and python_code.
snippet = open(sys.argv[1], encoding='utf-8').read()
full = snippet
out = sys.argv[-1]


def nid():
    return '{%s}' % uuid.uuid4()


def node(i, model, x, y):
    return {'id': i, 'model': model, 'ngo': {'comment': {'data': '', 'show': False}},
            'position': {'fix': False, 'x': x, 'y': y}}


def debug(i, x, y):
    return node(i, {'height': 140, 'name': 'Debug', 'output_h': 0, 'output_v': 0, 'page_index': 0,
                    'payload_h': 0, 'payload_v': 0, 'width': 225}, x, y)


cam, py, btn, dbg_out, mq = nid(), nid(), nid(), nid(), nid()
nodes = [
    node(btn, {'name': 'ButtonRun'}, 100, 60),
    node(cam, {'data': {'SonixUSBCamera': {'customPipeline': '! multipartdemux ! jpegdec', 'encodeMode': 'H265',
                                           'frameRate': 3, 'timeout_s': 10,
                                           'url': 'http://172.17.0.1:8082/stream', 'useCustomPipeline': False}},
               'name': 'GstCam ', 'node_enable': True, 'setting': {'name': 'SonixUSBCamera'}}, 100, 220),
    node(py, {'data': {'comment': 'KitCheck YOLO11n + QR', 'py_ver': '1. python3.12', 'script': snippet,
                       'tmp_path': '/home/cira',
                       'widget_state': {'editor_setting': {
                           'autoComplete': {'caseSensitivity': True, 'enable': True, 'threshold': 3},
                           'indent': {'enable': True}, 'theme': 'Material Dark',
                           'whitespace': {'enable': False, 'size': 2}}, 'splt0': 16, 'splt1': 10}},
              'height': 47, 'name': 'PythonScript', 'node_enable': True, 'python_code': full, 'width': 75},
         380, 220),
    debug(dbg_out, 660, 60),
    node(mq, {'mqtt_pub': {'cert_file': '-', 'host': '172.17.0.1', 'is_use_cert': False, 'is_use_ssl': False,
                           'pass': '', 'port': 1884, 'qos': 0, 'send_img': False,
                           'topic': 'kitcheck/station/live', 'user': ''},
              'name': 'MqttPub', 'node_enable': True}, 680, 330),
]


def wire(src, dst):
    return {'in_id': dst, 'in_index': 0, 'out_id': src, 'out_index': 0}


# The loop ButtonRun -> GstCam -> Python -> ButtonRun pulls the next frame only after the script is done, so
# it runs at the script's pace (about 1/s while idle) instead of spinning a CPU core. Press ButtonRun once
# after START to begin. No Debug on the raw camera: drawing frames in the GUI costs CPU.
scene = {'connections': [wire(btn, cam), wire(cam, py), wire(py, btn), wire(py, dbg_out), wire(py, mq)],
         'nodes': nodes,
         'scene_data': {'flow_view_fix': False, 'flow_view_pos_x': -60, 'flow_view_pos_y': -20,
                        'flow_view_zoom_factor': 0.8, 'scene_version': 1}}
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'scene_array': [{'data': json.dumps(scene, indent=4) + '\n', 'name': 'Main'}]}, f, indent=4)
print('wrote', out)
