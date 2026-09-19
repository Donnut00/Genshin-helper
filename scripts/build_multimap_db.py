"""
Fetch and bundle waypoints for all Teyvat regions and submaps from HoYoLAB OBC API:
- Map 2: Bản Đồ Tương Tác Teyvat (Mondstadt, Liyue, Inazuma, Sumeru, Fontaine, Natlan)
- Map 7: Enkanomiya
- Map 9: Vực Đá Sâu - Khu Mỏ Ngầm (The Chasm: Underground Mines)
- Map 34: Biển Kỷ Nguyên Cũ (Sea of Bygone Eras)
- Map 36: Núi Thánh Viễn Cổ (Ancient Holy Mountain)
- Map 37: Thần Điện Không Gian (Temple of Space)
- Map 40: Sương Nguyệt / Lăng Mộ Bạch Dương Tuyết (Frostmoon / Nod-Krai)
"""
import os
import json
import urllib.request
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("BuildMultiMapDB")

MAP_CONFIGS = [
    {"id": 2, "name": "Bản Đồ Tương Tác Teyvat", "priority": 1},
    {"id": 40, "name": "Sương Nguyệt (Lăng Mộ Bạch Dương Tuyết)", "priority": 2},
    {"id": 9, "name": "Vực Đá Sâu - Khu Mỏ Ngầm", "priority": 3},
    {"id": 7, "name": "Enkanomiya", "priority": 4},
    {"id": 34, "name": "Biển Kỷ Nguyên Cũ", "priority": 5},
    {"id": 36, "name": "Núi Thánh Viễn Cổ", "priority": 6},
    {"id": 37, "name": "Thần Điện Không Gian", "priority": 7},
]

# Labels to include: 2 (Statue of The Seven), 3 (Teleport Waypoint), 154 (Domain/Bí Cảnh)
ALLOWED_LABELS = {2, 3, 154}

def fetch_map_waypoints(map_id: int):
    url = f"https://sg-public-api.hoyolab.com/common/map_user/ys_obc/v1/map/point/list?map_id={map_id}&app_sn=ys_obc&lang=vi-vn"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        point_list = data.get("data", {}).get("point_list", [])
        wps = [
            {
                "id": p["id"],
                "label_id": p.get("label_id"),
                "x_pos": p.get("x_pos"),
                "y_pos": p.get("y_pos"),
                "area_id": p.get("area_id", 0),
                "map_id": map_id
            }
            for p in point_list
            if p.get("label_id") in ALLOWED_LABELS
        ]
        return wps

def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target_db_path = os.path.join(base_dir, "src", "waypoints_db.json")

    multimap_db = {
        "version": "2.0.0",
        "maps": {}
    }

    total_wps = 0
    for cfg in MAP_CONFIGS:
        mid = cfg["id"]
        mname = cfg["name"]
        logger.info(f"Downloading waypoints for Map {mid} ({mname})...")
        try:
            wps = fetch_map_waypoints(mid)
            multimap_db["maps"][str(mid)] = {
                "id": mid,
                "name": mname,
                "priority": cfg["priority"],
                "count": len(wps),
                "waypoints": wps
            }
            total_wps += len(wps)
            logger.info(f"  -> Map {mid} saved {len(wps)} waypoints.")
        except Exception as e:
            logger.error(f"Failed to fetch Map {mid}: {e}")

    with open(target_db_path, "w", encoding="utf-8") as f:
        json.dump(multimap_db, f, ensure_ascii=False, indent=2)

    logger.info(f"SUCCESS: Saved {len(multimap_db['maps'])} maps ({total_wps} total waypoints) to {target_db_path}")

if __name__ == "__main__":
    main()
