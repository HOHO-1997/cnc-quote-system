"""混合式设备识别与工时估算。输出可编辑工序，而不是直接覆盖报价。"""
from __future__ import annotations

from step_analyzer import turning_geometry_confidence


def _row(process: str, equipment: str, hours: float, basis: str, confidence: str, kind: str = "基础", confirmed: bool = True) -> dict:
    return {"工序": process, "推荐设备": equipment, "推荐时间(h)": round(max(0.0, hours), 2), "判断依据": basis,
            "置信度": confidence, "类型": kind, "用户确认": confirmed}


def _capability(config: dict, machine: str) -> dict:
    return config.get("machine_capabilities", {}).get(machine, {})


def _fits(step: dict, cap: dict) -> bool:
    dims = sorted(step.get("dimensions", [0, 0, 0]), reverse=True)
    travel = sorted([cap.get("x", 0), cap.get("y", 0), cap.get("z", 0)], reverse=True)
    return all(a <= b for a, b in zip(dims, travel)) and step.get("net_weight", 0) <= cap.get("max_weight", 0)


def estimate_operations(step: dict, drawing: dict, config: dict, product_type: str = "自动识别") -> dict:
    if not step or not step.get("available"):
        return {"rows": [], "summary": "未取得可靠 STEP 实体，无法自动估算；请人工填写工时。", "classification": "不确定"}
    dims = step["dimensions"]; max_dim = max(dims); weight = step["net_weight"]
    holes = drawing.get("hole_features", []); threads = drawing.get("threaded_count", 0)
    geometry_turn_conf, turn_evidence = turning_geometry_confidence(step)
    drawing_turn = drawing.get("requires_turning", False)
    small_valve = max_dim <= 250 and weight <= 10 and drawing_turn
    large_frame = max_dim >= 1500 and weight >= 500
    multi_side = drawing.get("drilled_count", 0) >= 8 and any(term in drawing.get("gd_terms", []) for term in ["位置度", "同轴度"])
    cnc_fit = _fits(step, _capability(config, "CNC加工中心"))
    hmc_fit = _fits(step, _capability(config, "卧式加工中心"))
    gantry_fit = _fits(step, _capability(config, "龙门铣"))
    rows: list[dict] = []
    evidence: list[str] = []
    if large_frame:
        classification, primary = "大型机架/床身", "龙门铣"
        evidence.append(f"最大尺寸 {max_dim:.0f} mm、净重 {weight:.0f} kg，超过普通 CNC 能力")
        # 按用户校准样本：批量 48-60 h。去除量只作小幅修正，不能用面数累加。
        removal_kg = max(0.0, step["blank_weight"] - weight)
        rough = 10.0 + min(4.0, removal_kg / 120)
        finish = 10.0 + (2.0 if drawing.get("min_tolerance") and drawing["min_tolerance"] <= 0.01 else 0.5)
        holes_time = min(12.0, 4.0 + drawing.get("drilled_count", 0)*0.02 + threads*0.025)
        rows = [_row("单件装夹找正", primary, 5.0, "大型铸件多基准找正", "高"),
                _row("粗加工主要基准与平面", primary, rough, "毛坯局部余量与大平面", "中"),
                _row("精加工导轨/安装面", primary, finish, "关键尺寸与形位要求", "中"),
                _row("多侧面及端面加工", primary, 8.0, "大型框架多方向加工", "中"),
                _row("钻孔、扩孔、镗孔", primary, holes_time, "2D 孔系与螺纹统计", "中"),
                _row("换刀、机内测量、去毛刺", primary, 5.0, "大型件检测与辅助", "中")]
        programming, fixture = 12.0, 4.0
    elif small_valve:
        classification, primary = "小型阀体", "CNC加工中心"
        evidence += ["小尺寸阀体", "2D 存在大直径螺纹/密封槽或同轴孔", turn_evidence]
        rows = [_row("车床第一次装夹：端面、内孔、台阶、槽、螺纹", "车床", 0.35, "M40+ 螺纹/密封槽/同轴回转特征", "高"),
                _row("车床翻面：第二端端面和内孔", "车床", 0.20, "两端同轴加工", "高"),
                _row("CNC 法兰面与精孔", primary, 0.35, "顶部/侧面加工方向", "中"),
                _row("CNC 钻孔、侧孔、斜孔", primary, 0.35, f"识别到约 {drawing.get('drilled_count', 0)} 个成组孔", "中"),
                _row("CNC 攻牙、倒角与检测", primary, 0.25, f"识别到约 {threads} 个螺纹", "中")]
        programming, fixture = 1.5, 0.5
    else:
        if not cnc_fit and gantry_fit:
            classification, primary = "大型铣削件", "龙门铣"; evidence.append("超过普通 CNC 行程或承重")
        elif hmc_fit and (multi_side or (max_dim > 800 and not gantry_fit)):
            classification, primary = "箱体/多方向孔系", "卧式加工中心"; evidence.append("多侧孔/镗孔和位置度，同一装夹有优势")
        else:
            classification, primary = "通用铸件/机加工件", "CNC加工中心"; evidence.append("尺寸与重量在普通 CNC 能力范围")
        plane_area = step.get("total_planar_area_m2", 0.0)
        removal = max(0.0, step["blank_weight"] - weight)
        setup = 0.45 + (0.25 if max_dim > 500 else 0)
        rough = max(0.25, min(4.0, removal / (8 if primary == "CNC加工中心" else 14)))
        finish = max(0.25, min(3.0, plane_area*3.0 + (0.4 if drawing.get("min_tolerance") and drawing["min_tolerance"] <= 0.05 else 0)))
        hole_time = min(3.0, 0.15 + sum(item["count"]*(0.015 + item["diameter"]*0.0008) for item in holes))
        tap_time = min(2.0, threads*0.018)
        rows = [_row("装夹找正", primary, setup, "加工方向与零件尺寸", "中"),
                _row("粗铣/粗加工", primary, rough, "毛坯余量与材料", "低"),
                _row("精铣平面和轮廓", primary, finish, "实际平面面积与精度", "低"),
                _row("钻孔/扩孔/铰孔/镗孔", primary, hole_time, "孔数量、直径与深度", "中"),
                _row("设备刚性攻牙、倒角和测量", primary, tap_time + 0.15, "螺纹数量与辅助时间", "中")]
        programming, fixture = 2.0, 0.0
        # 只有强的同轴回转证据加车床；局部侧孔不触发。
        if drawing_turn and geometry_turn_conf >= 0.55:
            rows.append(_row("车削同轴孔/外圆/槽", "车床", 0.6, turn_evidence, "中"))
    # 图纸追加项默认不确认，避免悄悄进入报价。
    for source in drawing.get("extra_sources", []):
        rows.append(_row("精度/检验追加：" + source["source"], source["recommended_equipment"], source["hours"], source["source"], source["confidence"], "追加", False))
    # 磨床：配对等高按一对时间除以二；大件是否可上磨床取决于设备能力。
    max_plane = step.get("largest_planar_area_m2", 0.0)
    flat_high = any(v <= 0.01 for v in drawing.get("geometric_values", [])) and any(x in drawing.get("gd_terms", []) for x in ["平面度", "平行度"])
    grinding_reason = "未建议磨床"
    if drawing.get("pair_height_requirement"):
        pair_time = max(0.6, 0.55 + max_plane*2.0 + (0.2 if drawing.get("min_tolerance") and drawing["min_tolerance"] <= 0.01 else 0))
        rows.append(_row("两件配对磨削（按单件分摊）", "磨床", pair_time/2, f"每对约 {pair_time:.1f} h；两件等高交付", "高", "基础", True))
        grinding_reason = f"两件等高：每对磨削 {pair_time:.1f} h，单件分摊 {pair_time/2:.2f} h"
    elif drawing.get("explicit_grinding") or (flat_high and 150 <= max_dim <= 800) or (drawing.get("roughness") and min(drawing["roughness"]) <= 0.8):
        if _fits(step, _capability(config, "磨床")):
            h = max(0.4, 0.3 + max_plane*2.0 + (0.2 if flat_high else 0))
            rows.append(_row("关键平面磨削", "磨床", h, "明确磨削或大平面 0.01 级精度/Ra0.8", "中", "基础", True))
            grinding_reason = "关键平面精度超出常规精铣稳定能力，建议磨床"
        else:
            grinding_reason = "存在高精度磨削信号，但尺寸/承重可能超过普通磨床；需人工确认精密龙门或外协大型磨床"
    return {"rows": rows, "classification": classification, "evidence": evidence, "programming_hours": programming,
            "fixture_hours": fixture, "grinding_assessment": grinding_reason, "turning_geometry_confidence": geometry_turn_conf}
