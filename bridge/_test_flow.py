"""水槽ブリッジ動作テスト用スクリプト

zone_state.json を 10 秒間操作して、
  approach -> (5秒滞在) -> speak -> leave -> idle
のフローを再現する。ブラウザを開いた状態で実行すると魚の状態が変わる。

使い方:
  ssh pi-main 'cd ~/Documents/fish_ai_realtime && .venv/bin/python ~/Documents/aquarium/bridge/_test_flow.py'
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/mine/Documents/fish_ai_realtime")
import zone_state

print("[test] 主人が水槽の前に立つ (cam_0 zone=zone_003 を 6 秒間更新)")
t0 = time.time()
while time.time() - t0 < 6:
    zone_state.update_node_zone(
        "cam_0", "zone_003", person=True, center_x=100, center_y=100, confidence=0.9
    )
    time.sleep(0.5)

print("[test] 顔認識成功 (recognized_person_id = 001)")
zone_state.update_recognized_person("001")
t1 = time.time()
while time.time() - t1 < 2:
    zone_state.update_node_zone(
        "cam_0", "zone_003", person=True, center_x=100, center_y=100, confidence=0.9
    )
    time.sleep(0.5)

print("[test] 退出 (active_zone を null に)")
zone_state.update_node_zone("cam_0", None)
zone_state.update_recognized_person(None)
time.sleep(2)
print("[test] done")
