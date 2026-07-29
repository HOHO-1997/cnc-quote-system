import unittest

from config import DEFAULT_CONFIG
from machining_estimator import estimate_operations
from pricing import calculate_quote


def step(dimensions, weight, blank, plane=0.2, groups=None):
    return {"available": True, "dimensions": dimensions, "net_weight": weight, "blank_weight": blank,
            "largest_planar_area_m2": plane, "total_planar_area_m2": plane*2, "cylinder_groups": groups or [], "face_count": 100}


class RegressionTests(unittest.TestCase):
    def test_large_frame_is_gantry_not_lathe(self):
        drawing = {"threaded_count": 150, "drilled_count": 100, "min_tolerance": 0.01, "gd_terms": ["平行度"], "hole_features": [], "requires_turning": False, "extra_sources": []}
        result = estimate_operations(step([2050, 765, 460], 1080, 1320, 0.8), drawing, DEFAULT_CONFIG)
        rows = result["rows"]
        gantry = sum(x["推荐时间(h)"] for x in rows if x["推荐设备"] == "龙门铣")
        self.assertEqual(result["classification"], "大型机架/床身")
        self.assertGreaterEqual(gantry, 48)
        self.assertLessEqual(gantry, 65)
        self.assertFalse(any(x["推荐设备"] == "车床" for x in rows))

    def test_beam_bracket_pair_grinding(self):
        drawing = {"threaded_count": 20, "drilled_count": 16, "min_tolerance": 0.01, "geometric_values": [0.01], "gd_terms": ["平面度"], "pair_height_requirement": True, "explicit_grinding": False, "requires_turning": False, "hole_features": [], "extra_sources": []}
        result = estimate_operations(step([560, 365, 240], 170, 205, 0.16), drawing, DEFAULT_CONFIG)
        grinding = [x for x in result["rows"] if x["推荐设备"] == "磨床"]
        self.assertTrue(grinding)
        self.assertGreater(grinding[0]["推荐时间(h)"], 0.4)
        self.assertLess(grinding[0]["推荐时间(h)"], 0.8)

    def test_small_valve_lathe_and_cnc(self):
        drawing = {"threaded_count": 10, "drilled_count": 8, "thread_diameters": [72], "min_tolerance": 0.02, "gd_terms": [], "hole_features": [], "requires_turning": True, "extra_sources": []}
        groups = [{"area": 1000, "count": 4, "diameters": [72, 50]}]
        result = estimate_operations(step([152, 135, 93], 3.4, 4.1, 0.02, groups), drawing, DEFAULT_CONFIG)
        times = {m: sum(x["推荐时间(h)"] for x in result["rows"] if x["推荐设备"] == m) for m in ["车床", "CNC加工中心"]}
        self.assertEqual(result["classification"], "小型阀体")
        self.assertAlmostEqual(times["车床"], 0.55, places=2)
        self.assertAlmostEqual(times["CNC加工中心"], 0.95, places=2)

    def test_quantity_and_one_time_price(self):
        rows = [{"工序": "CNC", "推荐设备": "CNC加工中心", "推荐时间(h)": 1, "类型": "基础", "用户确认": True}]
        data = {"quantity": 10, "material": "灰铁", "net_weight": 2, "casting_weight": 2.4, "quote_mode": "成本加利润", "packaging_mode": "整批费用", "packaging_cost": 100, "surface_area_m2": 0}
        result = calculate_quote(data, rows, DEFAULT_CONFIG, [{"启用": True, "项目": "编程费", "计价方式": "整批一次性费用", "金额": 200}], [])
        self.assertAlmostEqual(result["batch_price"], result["unit_price"]*10 + 300, places=2)


if __name__ == "__main__":
    unittest.main()
