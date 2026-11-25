# CiRA CORE 1.9.4 (Linux, Docker) on the cloud GPU VM: KitCheck's whole AI side as one CiRA flow.
#   python make_npj_cira_ai.py   -> kitcheck_cira_ai.npj
#
# 1. Vision   ButtonRun -> GstCam (tray camera / virtual tray, http://172.17.0.1:8082/stream)
#             -> Python+ "KitCheck AI" (YOLO11 on the T4 + QR, posts results to KitCheck) -> ButtonRun (loop)
#             Python+ -> Debug (picture with boxes) and -> MqttPub kitcheck/cira/result (result for anyone)
# 2. Scale    MqttSub kitcheck/sim/scale (the /sim page's scale via KitCheck) -> Debug
# 3. Alerts   MqttSub kitcheck/event (KitCheck: borrow, return, round stopped, staff action)
#             -> Python+ "LINE notify" (push to subscribed chats) -> Debug
# 4. Chatbot  LineBot (service name /kitcheck_line) -> ROS service opened by Python+ "LINE chat"
#             (ถาด / ยืม / สมัคร / เลิก) -> reply text + tray picture
#
# MQTT user/password, LINE channel token/secret and the ngrok token are left EMPTY: type them in the nodes'
# settings in CiRA (and LINE_TOKEN into /home/cira/kitcheck_model/line.env for the notify node).
import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BROKER = "100.124.5.71"  # saer (Mosquitto) over Tailscale


def src(name):
    return open(os.path.join(HERE, name), encoding="utf-8").read()


def loader(name):
    """Node code that runs /home/cira/kitcheck_model/<name>: change the file, no editing inside CiRA."""
    return ("#preparation here\n# KitCheck: the code lives in /home/cira/kitcheck_model/%s\n"
            "exec(compile(open('/home/cira/kitcheck_model/%s', encoding='utf-8').read(), '%s', 'exec'))\n"
            % (name, name, name))


def nid():
    return '{%s}' % uuid.uuid4()


def node(i, model, x, y, comment=""):
    return {'id': i, 'model': model, 'ngo': {'comment': {'data': comment, 'show': bool(comment)}},
            'position': {'fix': False, 'x': x, 'y': y}}


EDITOR = {'autoComplete': {'caseSensitivity': True, 'enable': True, 'threshold': 3}, 'indent': {'enable': True},
          'theme': 'Material Dark', 'whitespace': {'enable': False, 'size': 2}}


def pyplus(caption, code, use_image=True):
    return {'data': {'auto_start': True, 'caption': caption, 'code': code, 'editor_setting': EDITOR,
                     'icon': ':/cira_bernoulli_python_plus/model/python_plus.png', 'not_use_image': not use_image,
                     'python_path': '/usr/bin/python3'}, 'name': 'Python+', 'node_enable': True}


def mqtt(kind, topic, send_img=False):
    m = {'cert_file': '-', 'host': BROKER, 'is_use_cert': False, 'is_use_ssl': False, 'pass': '', 'port': 1883,
         'qos': 0, 'topic': topic, 'user': ''}
    if kind == 'pub':
        m['send_img'] = send_img
    return {'mqtt_' + kind: m, 'name': 'MqttPub' if kind == 'pub' else 'MqttSub', 'node_enable': True}


def debug(page=0):
    return {'height': 140, 'name': 'Debug', 'output_h': 0, 'output_v': 0, 'page_index': page, 'payload_h': 0,
            'payload_v': 0, 'width': 225}


ids = {k: nid() for k in ('btn', 'cam', 'ai', 'dbg_ai', 'pub', 'sub_scale', 'dbg_scale', 'sub_ev', 'notify',
                          'dbg_ev', 'line', 'chat')}
nodes = [
    # 1. vision
    node(ids['btn'], {'name': 'ButtonRun'}, 60, 40, '1. ตรวจภาพ: กด START แล้วกดปุ่มนี้ 1 ครั้ง'),
    node(ids['cam'], {'data': {'TrayCamera': {'customPipeline': '! multipartdemux ! jpegdec', 'encodeMode': 'H265',
                                              'frameRate': 15, 'timeout_s': 10,
                                              'url': 'http://172.17.0.1:8082/stream', 'useCustomPipeline': False}},
                      'name': 'GstCam ', 'node_enable': True, 'setting': {'name': 'TrayCamera'}}, 60, 190),
    node(ids['ai'], pyplus('KitCheck AI (YOLO11 GPU + QR)', src('kitcheck_pyplus.py')), 300, 190),
    node(ids['dbg_ai'], debug(2), 560, 20),
    node(ids['pub'], mqtt('pub', 'kitcheck/cira/result'), 560, 230, 'ผล AI ออก MQTT'),
    # 2. scale
    node(ids['sub_scale'], mqtt('sub', 'kitcheck/sim/scale'), 60, 400, '2. ตาชั่ง (จาก /sim ผ่าน KitCheck)'),
    node(ids['dbg_scale'], debug(0), 300, 380),
    # 3. alerts -> LINE
    node(ids['sub_ev'], mqtt('sub', 'kitcheck/event'), 60, 600, '3. เหตุการณ์ KitCheck -> LINE'),
    node(ids['notify'], pyplus('LINE notify', loader('kitcheck_line_notify.py'), use_image=False), 300, 600),
    node(ids['dbg_ev'], debug(0), 560, 560),
    # 4. LINE chatbot
    node(ids['line'], {'data': {'line_msg_secret': '', 'line_msg_token': '', 'ngrok_auth_token': '',
                                'ngrok_local_port': 3000, 'ngrok_region': 'jp', 'python_path': '/usr/bin/python3',
                                'ros_service_name': '/kitcheck_line', 'working_dir': '/home/cira/linebot'},
                       'height': 52, 'name': 'LineBot', 'node_enable': True, 'width': 75}, 60, 800,
         '4. แชตบอท LINE: ถาด / ยืม / สมัคร'),
    node(ids['chat'], pyplus('LINE chat', loader('kitcheck_line_chat.py')), 300, 800),
]


def wire(a, b):
    return {'in_id': ids[b], 'in_index': 0, 'out_id': ids[a], 'out_index': 0}


scene = {'connections': [wire('btn', 'cam'), wire('cam', 'ai'), wire('ai', 'btn'), wire('ai', 'dbg_ai'),
                         wire('ai', 'pub'),
                         wire('sub_scale', 'dbg_scale'),
                         wire('sub_ev', 'notify'), wire('notify', 'dbg_ev'),
                         wire('line', 'chat'), wire('chat', 'line')],
         'nodes': nodes,
         'scene_data': {'flow_view_fix': False, 'flow_view_pos_x': -40, 'flow_view_pos_y': -20,
                        'flow_view_zoom_factor': 0.7, 'scene_version': 1}}
out = os.path.join(HERE, "kitcheck_cira_ai.npj")
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'scene_array': [{'data': json.dumps(scene, indent=4, ensure_ascii=False) + '\n', 'name': 'Main'}]},
              f, indent=4, ensure_ascii=False)
print('wrote', out, len(nodes), 'nodes')
