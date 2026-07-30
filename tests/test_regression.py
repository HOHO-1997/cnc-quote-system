import unittest

from config import DEFAULT_CONFIG
from export import quote_excel
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
        quote = calculate_quote({"quantity": 1, "material": "球铁", "net_weight": 1080, "casting_weight": 1320, "quote_mode": "成本加利润", "packaging_mode": "单件费用"}, rows, DEFAULT_CONFIG, [], [])
        gantry = quote["batch_equipment_time"]
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
        # 螺纹方式默认“待确认”，因此自动基础时间不把攻牙悄悄计入报价。
        self.assertAlmostEqual(times["CNC加工中心"], 0.85, places=2)

    def test_quantity_and_one_time_price(self):
        rows = [{"工序": "CNC", "推荐设备": "CNC加工中心", "推荐时间(h)": 1, "类型": "基础", "用户确认": True}]
        data = {"quantity": 10, "material": "灰铁", "net_weight": 2, "casting_weight": 2.4, "quote_mode": "成本加利润", "packaging_mode": "整批费用", "packaging_cost": 100, "surface_area_m2": 0}
        result = calculate_quote(data, rows, DEFAULT_CONFIG, [{"启用": True, "项目": "编程费", "计价方式": "整批一次性费用", "金额": 200}], [])
        # 单件报价已经含整批一次性费用的分摊，不能再次加 300。
        self.assertAlmostEqual(result["batch_price"], result["unit_price"]*10, places=2)
        self.assertAlmostEqual(result["one_time_cost"], 300, places=2)

    def test_batch_pair_and_tapping_calculation(self):
        rows = [
            {"工序": "首件找正", "计算类型": "每批一次", "推荐设备": "CNC加工中心", "单件时间(h)": 0, "每批时间(h)": 0.4, "用户确认": True},
            {"工序": "单件上下料", "计算类型": "每件", "推荐设备": "CNC加工中心", "单件时间(h)": 0.1, "用户确认": True},
            {"工序": "两件配对磨削", "计算类型": "每对产品", "推荐设备": "磨床", "单件时间(h)": 0.4, "每批时间(h)": 0.8, "用户确认": True},
            {"工序": "M4 螺纹加工", "计算类型": "每件", "推荐设备": "CNC加工中心", "数量": 36, "单件时间(h)": 0.012, "攻牙方式": "混合攻牙", "设备攻牙数量": 30, "人工攻牙数量": 6, "人工单孔时间(h)": 0.03, "用户确认": True},
        ]
        data = {"quantity": 3, "material": "灰铁", "net_weight": 1, "casting_weight": 1.2, "quote_mode": "成本加利润", "packaging_mode": "单件费用"}
        result = calculate_quote(data, rows, DEFAULT_CONFIG, [], [])
        schedules = {x["工序"]: x for x in result["operation_schedules"]}
        self.assertAlmostEqual(schedules["首件找正"]["整批设备时间(h)"], 0.4)
        self.assertAlmostEqual(schedules["两件配对磨削"]["整批设备时间(h)"], 1.6)
        self.assertEqual(schedules["M4 螺纹加工（设备刚性攻牙）"]["设备"], "CNC加工中心")
        self.assertEqual(schedules["M4 螺纹加工（人工攻牙）"]["设备"], "人工工位")
        self.assertAlmostEqual(schedules["M4 螺纹加工（设备刚性攻牙）"]["整批设备时间(h)"], 1.08)
        self.assertAlmostEqual(schedules["M4 螺纹加工（人工攻牙）"]["整批人工时间(h)"], 0.54)
        self.assertTrue(result["pair_warning"])

    def test_100_piece_one_time_fee_is_not_multiplied(self):
        rows = [{"工序": "纯切削", "计算类型": "每件", "推荐设备": "CNC加工中心", "单件时间(h)": 1, "用户确认": True}]
        data = {"quantity": 100, "sample_quantity": 1, "tier_rows": [{"数量": 100, "批量效率系数": 0.93}],
                "material": "灰铁", "net_weight": 1, "casting_weight": 1.2, "quote_mode": "成本加利润", "packaging_mode": "单件费用"}
        result = calculate_quote(data, rows, DEFAULT_CONFIG, [{"启用": True, "项目": "首件检测费", "计价方式": "整批一次性费用", "金额": 243}], [])
        self.assertAlmostEqual(result["additional_one_time_cost"], 243)
        self.assertAlmostEqual(result["one_time_per_unit"], 2.43)
        self.assertAlmostEqual(result["batch_cost"], (result["nonprocessing_per_unit"] + result["discounted_processing_per_unit"]) * 100 + 243)
        self.assertGreater(result["sample_unit_price"], result["unit_price"])
        self.assertGreater(len(quote_excel(data, result, DEFAULT_CONFIG)), 1000)

    def test_manual_tapping_58_holes_is_labor_not_cnc(self):
        groups = [("M4-A", 18), ("M4-B", 8), ("M4-C", 12), ("M10", 8), ("M4-D", 12)]
        rows = []
        for name, count in groups:
            rows.append({"工序": f"{name} 螺纹底孔", "计算类型": "每件", "推荐设备": "CNC加工中心", "数量": count,
                         "单件时间(h)": count * 0.01, "用户确认": True})
            rows.append({"工序": f"{name} 螺纹加工", "计算类型": "每件", "推荐设备": "CNC加工中心", "攻牙设备": "CNC加工中心",
                         "数量": count, "单件时间(h)": 0.012, "人工单孔时间(h)": 0.03, "攻牙方式": "人工攻牙", "用户确认": False})
        data = {"quantity": 100, "sample_quantity": 1, "tier_rows": [{"数量": 100}], "material": "灰铁", "net_weight": 1,
                "casting_weight": 1.2, "quote_mode": "成本加利润", "packaging_mode": "单件费用"}
        result = calculate_quote(data, rows, DEFAULT_CONFIG, [{"启用": True, "项目": "首件检测费", "计价方式": "整批一次性费用", "金额": 243}], [])
        tapping = [x for x in result["operation_schedules"] if "螺纹加工" in x["工序"]]
        bottom_holes = [x for x in result["operation_schedules"] if "螺纹底孔" in x["工序"]]
        self.assertAlmostEqual(result["tapping_labor_hours_per_unit"], 1.74)
        self.assertAlmostEqual(result["tapping_labor_per_unit"], 60.90)
        self.assertAlmostEqual(sum(x["人工金额(元)"] for x in tapping), 6090.0)
        self.assertTrue(all(x["设备"] == "人工工位" and x["整批设备时间(h)"] == 0 for x in tapping))
        self.assertGreater(sum(x["整批设备时间(h)"] for x in bottom_holes), 0)
        self.assertAlmostEqual(result["processing_discount"], 0.85)
        self.assertAlmostEqual(result["one_time_per_unit"], 2.43)
        self.assertAlmostEqual(result["casting_per_unit"], 7.20)  # 材料没有参与 85% 加工费折扣
        # 折扣仅作用于金额，真实设备/人工工时在 1 件和 100 件时按数量线性增长。
        one_piece = calculate_quote({**data, "quantity": 1}, rows, DEFAULT_CONFIG, [], [], include_tiers=False)
        self.assertAlmostEqual(result["tapping_labor_hours_per_unit"], one_piece["tapping_labor_hours_per_unit"])


if __name__ == "__main__":
    unittest.main()
