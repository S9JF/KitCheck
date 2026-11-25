# CiRA CORE 1.9.4 (Linux, Docker) on the cloud GPU VM:
#   python make_npj_gpu.py   -> kitcheck_station_gpu.npj
# Flow: ButtonRun -> GstCam (http://172.17.0.1:8083/stream: the laptop camera with gpu_station.py's real-time
#       YOLO boxes drawn in) -> Python (kitcheck_script_node_gpu.py: newest result into the payload) -> ButtonRun,
#       and Python -> Debug. Press ButtonRun once after START. Detection itself never depends on this flow.
import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "kitcheck_script_node_gpu.py"), encoding="utf-8").read()


def nid():
    return '{%s}' % uuid.uuid4()


def node(i, model, x, y):
    return {'id': i, 'model': model, 'ngo': {'comment': {'data': '', 'show': False}},
            'position': {'fix': False, 'x': x, 'y': y}}


cam, py, btn, dbg = nid(), nid(), nid(), nid()
nodes = [
    node(btn, {'name': 'ButtonRun'}, 100, 60),
    node(cam, {'data': {'LaptopCamera': {'customPipeline': '! multipartdemux ! jpegdec', 'encodeMode': 'H265',
                                         'frameRate': 10, 'timeout_s': 10,
                                         'url': 'http://172.17.0.1:8083/stream', 'useCustomPipeline': False}},
               'name': 'GstCam ', 'node_enable': True, 'setting': {'name': 'LaptopCamera'}}, 100, 220),
    node(py, {'data': {'comment': 'KitCheck: real-time YOLO on T4 GPU', 'py_ver': '1. python3.12', 'script': src,
                       'tmp_path': '/home/cira',
                       'widget_state': {'editor_setting': {
                           'autoComplete': {'caseSensitivity': True, 'enable': True, 'threshold': 3},
                           'indent': {'enable': True}, 'theme': 'Material Dark',
                           'whitespace': {'enable': False, 'size': 2}}, 'splt0': 16, 'splt1': 10}},
              'height': 47, 'name': 'PythonScript', 'node_enable': True, 'python_code': src, 'width': 75},
         380, 220),
    node(dbg, {'height': 140, 'name': 'Debug', 'output_h': 0, 'output_v': 0, 'page_index': 0,
               'payload_h': 0, 'payload_v': 0, 'width': 225}, 660, 60),
]


def wire(a, b):
    return {'in_id': b, 'in_index': 0, 'out_id': a, 'out_index': 0}


scene = {'connections': [wire(btn, cam), wire(cam, py), wire(py, btn), wire(py, dbg)],
         'nodes': nodes,
         'scene_data': {'flow_view_fix': False, 'flow_view_pos_x': -60, 'flow_view_pos_y': -20,
                        'flow_view_zoom_factor': 0.8, 'scene_version': 1}}
out = os.path.join(HERE, "kitcheck_station_gpu.npj")
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'scene_array': [{'data': json.dumps(scene, indent=4) + '\n', 'name': 'Main'}]}, f, indent=4)
print('wrote', out)
