import unittest

from config import DEFAULT_CONFIG
from export import quote_excel
from machining_estimator import estimate_operations
from pricing import calculate_quote
from drawing_analyzer import analyze_drawing, normalize_engineering_text, extract_fields
from step_analyzer import group_coaxial_cylinders


def step(dimensions, weight, blank, plane=0.2, groups=None):
    return {"available": True, "dimensions": dimensions, "net_weight": weight, "blank_weight": blank,
            "largest_planar_area_m2": plane, "total_planar_area_m2": plane*2, "cylinder_groups": groups or [], "face_count": 100}


class RegressionTests(unittest.TestCase):
    def test_engineering_annotation_normalization_separates_threads_and_holes(self):
        text = "M 7 2 X 1 . 5 - 6 H\n4 - \u00d8 7 . 1 0 \u901a\n2 - \u00d8 2 . 4 +0.05/-0.00 \u6df14.5\n\u00d8 2 1 +0.01/-0.03\n\u00b10.000"
        normalized = normalize_engineering_text(text)
        drawing = analyze_drawing(text)
        self.assertIn("M72\u00d71.5-6H", normalized)
        self.assertEqual(drawing["thread_groups"][0]["\u89c4\u683c"], "M72\u00d71.5-6H")
        self.assertEqual(drawing["threaded_count"], 1)
        holes = {round(item["diameter"], 2): item for item in drawing["hole_features"]}
        self.assertEqual(holes[7.1]["count"], 4)
        self.assertTrue(holes[7.1]["through"])
        self.assertEqual(holes[2.4]["count"], 2)
        self.assertFalse(holes[2.4]["through"])
        self.assertNotIn(0.0, drawing["dimensional_tolerances"])

    def test_valve_pdf_extracted_diameter_symbol_and_spaced_digits(self):
        # 真实 PyPDF 文本形态：使用 ∅，并把直径数字和公差拆到多行。
        text = "M 7 2 X1.5 - 6 H\n4 - \u2205 7 .1 0 \u00b1 0 .1 0 \u901a\n2 - \u2205 2.4 + 0.0 5\n- 0.0 0 4 .5\n\u2205 21 + 0.01\n- 0.03"
        drawing = analyze_drawing(text)
        self.assertEqual(drawing["thread_groups"], [{"\u89c4\u683c": "M72\u00d71.5-6H", "\u6570\u91cf": 1, "\u76f4\u5f84": 72.0}])
        features = {round(item["diameter"], 2): item for item in drawing["hole_features"]}
        self.assertEqual(features[7.1]["count"], 4)
        self.assertTrue(features[7.1]["through"])
        self.assertEqual(features[2.4]["count"], 2)

    def test_large_frame_plain_hole_and_thread_annotations(self):
        # 大型机架图纸常不用 Ø，而写成 6×33通、2×24贯穿；螺纹深度与规格之间有空格。
        text = "8 x M5 10\n9 x M6 12\n4 x M30 50\n12 x M8 16\n12 x M12 24\n6X 33\u901a\n2X 20\u8d2f\u7a7f\n8x 23 \u8d2f\u7a7f\n2X 24 \u8d2f\u7a7f\n24 x M10 24"
        drawing = analyze_drawing(text)
        threads = {(item["\u89c4\u683c"], item["\u6570\u91cf"]) for item in drawing["thread_groups"]}
        holes = {(item["count"], item["diameter"], item["through"]) for item in drawing["hole_features"]}
        self.assertIn(("M5", 8), threads)
        self.assertIn(("M30", 4), threads)
        self.assertIn(("M10", 24), threads)
        self.assertIn((6, 33.0, True), holes)
        self.assertIn((2, 20.0, True), holes)
        self.assertIn((8, 23.0, True), holes)
        self.assertIn((2, 24.0, True), holes)

    def test_note_numbers_are_not_threads_and_u_slot_is_separate_setup(self):
        text = "MATERIAL SPECIFICATIONS\nM1. MATERIAL: GRADE 80-55-6\nM2. FINISH: E-COAT\nM3. MANUFACTURING PROCESS: SAND CAST\nM4. CLEANLINESS\nM5. NO CHANGES\nMACHINING DATUM TARGETS\nPOST MACHINED\nSECTION A-A\nSECTION B-B\n12.25 \u00b10.202X THRU\n11.0 \u00b10.22X THRU"
        drawing = analyze_drawing(text)
        self.assertEqual(drawing["threaded_count"], 0)
        self.assertEqual(drawing["rejected_thread_notes"], ["M1", "M2", "M3", "M4", "M5"])
        self.assertTrue(drawing["slot_candidate"]["candidate"])
        self.assertGreaterEqual(drawing["drilled_count"], 4)
        mock_step = step([153, 96, 79], 1.354, 1.55, 0.08)
        result = estimate_operations(mock_step, drawing, DEFAULT_CONFIG)
        rows = result["rows"]
        machining_rows = [row for row in rows if row["\u63a8\u8350\u8bbe\u5907"] == "CNC\u52a0\u5de5\u4e2d\u5fc3"]
        machine_time = sum(row["\u63a8\u8350\u65f6\u95f4(h)"] for row in machining_rows)
        self.assertTrue(any("U\u5f62\u69fd" in row["\u5de5\u5e8f"] and row.get("\u88c5\u5939\u7f16\u53f7") == "OP20" for row in rows))
        self.assertEqual({row.get("\u88c5\u5939\u7f16\u53f7") for row in rows if row.get("\u88c5\u5939\u7f16\u53f7")}, {"OP10", "OP20"})
        self.assertGreaterEqual(machine_time, 2.3)
        self.assertLessEqual(machine_time, 2.7)

    def test_coaxial_grouping_allows_different_axis_origins(self):
        records = [
            {"radius": 36, "area": 100, "direction": (0, 0, 1), "origin": (0, 0, 0)},
            {"radius": 25, "area": 90, "direction": (0, 0, -1), "origin": (0, 0, 30)},
            {"radius": 20, "area": 80, "direction": (0.001, 0, 1), "origin": (0.2, 0, 70)},
            {"radius": 4, "area": 10, "direction": (1, 0, 0), "origin": (50, 0, 0)},
        ]
        groups = group_coaxial_cylinders(records)
        self.assertEqual(groups[0]["count"], 3)
        self.assertIn(72.0, groups[0]["diameters"])

    def test_valve_route_has_lathe_and_no_forced_hmc(self):
        drawing = {"threaded_count": 1, "drilled_count": 6, "thread_diameters": [72], "min_tolerance": 0.02,
                   "gd_terms": [], "hole_features": [{"diameter": 7.1, "count": 4, "through": True}, {"diameter": 2.4, "count": 2, "through": False}],
                   "requires_turning": True, "extra_sources": []}
        groups = [{"area": 100, "count": 4, "diameters": [72, 69.6, 64, 50]}]
        result = estimate_operations(step([152, 135, 93], 3.4, 4.1, 0.02, groups), drawing, DEFAULT_CONFIG, product_type="\u5c0f\u578b\u9600\u4f53")
        lathe = sum(row["\u63a8\u8350\u65f6\u95f4(h)"] for row in result["rows"] if row["\u63a8\u8350\u8bbe\u5907"] == "\u8f66\u5e8a")
        cnc = sum(row["\u63a8\u8350\u65f6\u95f4(h)"] for row in result["rows"] if row["\u63a8\u8350\u8bbe\u5907"] == "CNC\u52a0\u5de5\u4e2d\u5fc3")
        self.assertGreaterEqual(lathe, 0.55); self.assertLessEqual(lathe, 0.9)
        self.assertGreaterEqual(cnc, 0.7); self.assertLessEqual(cnc, 1.0)
        self.assertFalse(any(row["\u63a8\u8350\u8bbe\u5907"] == "\u5367\u5f0f\u52a0\u5de5\u4e2d\u5fc3" for row in result["rows"]))

    def test_box_multiside_features_prefer_hmc(self):
        drawing = {"threaded_count": 0, "drilled_count": 18, "side_feature_count": 3, "cross_wall_coaxial": True,
                   "horizontal_deep_holes": True, "gd_terms": ["\u4f4d\u7f6e\u5ea6"], "hole_features": [], "extra_sources": []}
        result = estimate_operations(step([600, 500, 400], 120, 145, 0.2), drawing, DEFAULT_CONFIG, product_type="\u7bb1\u4f53/\u591a\u65b9\u5411\u5b54\u7cfb")
        self.assertEqual(result["classification"], "\u7bb1\u4f53/\u591a\u65b9\u5411\u5b54\u7cfb")
        self.assertTrue(any(row["\u63a8\u8350\u8bbe\u5907"] == "\u5367\u5f0f\u52a0\u5de5\u4e2d\u5fc3" for row in result["rows"]))
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
        self.assertAlmostEqual(result["other_per_unit"], 0.0)
        # 折扣仅作用于金额，真实设备/人工工时在 1 件和 100 件时按数量线性增长。
        one_piece = calculate_quote({**data, "quantity": 1}, rows, DEFAULT_CONFIG, [], [], include_tiers=False)
        self.assertAlmostEqual(result["tapping_labor_hours_per_unit"], one_piece["tapping_labor_hours_per_unit"])

    def test_enabled_base_operations_and_manual_tapping_are_all_billed(self):
        rows = [
            {"工序": "粗铣", "计算类型": "每件", "推荐设备": "CNC加工中心", "单件时间(h)": 0.372, "启用": True, "用户确认": False},
            {"工序": "精铣", "计算类型": "每件", "推荐设备": "CNC加工中心", "单件时间(h)": 1.056, "启用": True, "用户确认": False},
            {"工序": "钻孔", "计算类型": "每件", "推荐设备": "CNC加工中心", "单件时间(h)": 0.15, "启用": True, "用户确认": False},
            {"工序": "倒角", "计算类型": "每件", "推荐设备": "CNC加工中心", "单件时间(h)": 0.08, "启用": True, "用户确认": False},
            {"工序": "M4 螺纹加工", "计算类型": "每件", "推荐设备": "人工工位", "攻牙设备": "CNC加工中心", "数量": 30,
             "单件时间(h)": 0.012, "人工单孔时间(h)": 0.03, "攻牙方式": "人工攻牙", "启用": True, "用户确认": False},
        ]
        data = {"quantity": 1, "material": "灰铁", "net_weight": 1, "casting_weight": 1.2, "quote_mode": "成本加利润", "packaging_mode": "单件费用"}
        result = calculate_quote(data, rows, DEFAULT_CONFIG, [], [])
        self.assertEqual(result["enabled_operation_count"], 5)
        self.assertEqual(result["final_billed_operation_count"], 5)
        self.assertAlmostEqual(result["equipment_per_unit"], (0.372 + 1.056 + 0.15 + 0.08) * 90, places=2)
        self.assertAlmostEqual(result["tapping_labor_per_unit"], 0.90 * 35, places=2)
        self.assertAlmostEqual(result["raw_processing_per_unit"], 180.72, places=2)

    def test_structured_hole_table_and_large_precision_plate_route(self):
        text = """标签 大小 数量
A Ø6.80 19.75
M8-6H 16 25
B Ø5 15
M6-6H 12 12
J Ø4.20 12.40
Ø6.20×90° 132
AC
Ø2.50 9.50
M3-6H 8
Ø3.05×90° 近端
8
D
2×Ø17.5 47.5
M20-6H 40
TITLE: DWG NO.
源鑫豐（香港）科技有限公司
TEC PHOTOELECTRICITY CO., LIMITED
X200AS-002-501 X200AS-002
阴影部分为精度面，要求精磨"""
        drawing = analyze_drawing(text)
        table = {row["label"]: row for row in drawing["hole_table_rows"]}
        self.assertEqual(table["A"]["quantity"], 25)
        self.assertEqual(table["J"]["quantity"], 132)
        self.assertEqual(table["AC"]["quantity"], 8)
        self.assertEqual(drawing["threaded_count"], 47)
        self.assertTrue(drawing["grinding_required"])
        fields = extract_fields(text, "fallback-name.pdf")
        self.assertEqual(fields["company_name"], "源鑫豐（香港）科技有限公司")
        self.assertEqual(fields["english_company_name"], "TEC PHOTOELECTRICITY CO., LIMITED")
        self.assertEqual(fields["product_name"], "X200AS-002")
        self.assertEqual(fields["drawing_number"], "X200AS-002-501")
        result = estimate_operations(step([1150, 770, 80], 57, 68, 0.85), drawing, DEFAULT_CONFIG)
        quote = calculate_quote({"quantity": 1, "material": "灰铁", "net_weight": 57, "casting_weight": 68,
                                 "quote_mode": "成本加利润", "packaging_mode": "单件费用"}, result["rows"], DEFAULT_CONFIG, [], [])
        gantry = sum(row["整批设备时间(h)"] for row in quote["operation_schedules"] if row["设备"] == "龙门铣")
        grinding = sum(row["整批设备时间(h)"] for row in quote["operation_schedules"] if row["设备"] == "磨床")
        self.assertEqual(result["classification"], "大型精密板件/多孔铸件")
        tapping = [row for row in result["rows"] if "螺纹加工" in row["工序"]]
        self.assertTrue(tapping)
        self.assertTrue(all(row["攻牙方式"] == "设备刚性攻牙" for row in tapping))
        self.assertTrue(all(row["推荐设备"] == "龙门铣" for row in tapping))
        self.assertGreaterEqual(gantry, 28)
        self.assertLessEqual(gantry, 36)
        self.assertGreaterEqual(grinding, 6)
        self.assertLessEqual(grinding, 10)

    def test_coordinate_hole_table_inherits_specs_and_precision_plate_route(self):
        # Coordinate-table rows carry X/Y only.  The M specification is a
        # merged cell and must be inherited by the following label rows.
        text = """13×Ø2.50 7.50
M3-6H 6
10×Ø6.80 19.75
M8-6H 16
阴影部分为精度面，要求研磨；两面加工
标签 ×位置 Y位置 大小
B1 10 36
3.30 10.10
M4-6H 8
B2 10 543
B3 30 16
Y1 91 526.50 M3-6H 6
2.50 7.50
Y2 91 576.50 M3-6H 6
2.50 7.50
AA1 119.50 55 4.20 12.40
M5-6H 10
AA2 119.50 445 4.20 12.40
M5-6H 10
H1 297 509.50 M6-6H 12
5 15
H2 327 509.50 M6-6H 12
5 15
"""
        drawing = analyze_drawing(text)
        counts = {item["规格"]: item["数量"] for item in drawing["thread_groups"]}
        self.assertEqual(counts["M4-6H"], 3)
        self.assertEqual(counts["M3-6H"], 15)  # Y1/Y2 plus 13-view call-out
        self.assertEqual(counts["M5-6H"], 2)
        self.assertEqual(counts["M6-6H"], 2)
        self.assertEqual(counts["M8-6H"], 10)
        self.assertTrue(drawing["two_sided_required"])
        self.assertTrue(drawing["grinding_required"])

        # The routing trigger is size + precision/grinding + hole volume, not
        # a file name.  It produces separate face/flip/side/grinding routes.
        drawing["drilled_count"] = 80
        result = estimate_operations(step([774, 640, 45], 75, 90, 0.30), drawing, DEFAULT_CONFIG)
        rows = result["rows"]
        setups = {row.get("装夹编号") for row in rows if row.get("装夹编号")}
        self.assertTrue({"OP10", "OP20", "OP30"}.issubset(setups))
        self.assertTrue(any(row["推荐设备"] == "磨床" for row in rows))
        self.assertTrue(all(row["攻牙方式"] == "设备刚性攻牙" for row in rows if "螺纹加工" in row["工序"]))
        quote = calculate_quote({"quantity": 1, "material": "灰铁", "net_weight": 75, "casting_weight": 90,
                                 "quote_mode": "成本加利润", "packaging_mode": "单件费用"}, rows, DEFAULT_CONFIG, [], [])
        self.assertGreaterEqual(quote["batch_equipment_time"], 18)
        self.assertLessEqual(quote["batch_equipment_time"], 25)
        self.assertGreaterEqual(quote["equipment_per_unit"], 3300)
        self.assertLessEqual(quote["equipment_per_unit"], 4300)
        self.assertTrue(quote["amount_validation"]["valid"])


if __name__ == "__main__":
    unittest.main()
