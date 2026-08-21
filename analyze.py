import csv
from datetime import datetime

with open("vtherm_mpc_fan_data_01KNA9CY.csv", "r") as f:
    reader = csv.DictReader(f)
    print("time | live_fan | mpc_fan | err | slope | mpc_stat | window | defrost | idle | mpc_conf")
    last = None
    for row in reader:
        try:
            dt = datetime.fromisoformat(row['timestamp'].replace('T', ' ').split('.')[0])
        except: continue
        
        if dt.month == 4 and dt.day == 6 and 7 <= dt.hour <= 8:
            curr = (row['current_fan'], row['mpc_shadow_fan'], row['mpc_shadow_status'])
            if curr != last:
                print(f"{dt.strftime('%H:%M:%S')} | {row['current_fan']:<8} | {row['mpc_shadow_fan']:<7} | {row['current_error']} | {row['effective_slope']} | {row['mpc_shadow_status']:<9} | {row['is_window_open']} | {row['defrost_active']} | {row['hvac_idle']} | conf:{row['mpc_shadow_confidence']}")
                last = curr
