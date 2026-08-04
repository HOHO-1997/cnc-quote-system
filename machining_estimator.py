"""混合式设备识别与工时估算。输出可编辑工序，而不是直接覆盖报价。"""
from __future__ import annotations

from step_analyzer import turning_geometry_confidence


def _row(process: str, equipment: str, hours: float, basis: str, confidence: str, kind: str = "基础", confirmed: bool = True,
         calculation_type: str = "每件", count: int = 1, batch_hours: float = 0.0, tapping_mode: str = "无螺纹加工") -> dict:
    return {"工序": process, "计算类型": calculation_type, "推荐设备": equipment, "数量": count,
            "单件时间(h)": round(max(0.0, hours), 3), "每批时间(h)": round(max(0.0, batch_hours), 3),
            "推荐时间(h)": round(max(0.0, hours), 3), "攻牙方式": tapping_mode, "设备攻牙数量": 0, "人工攻牙数量": 0,
            "人工单孔时间(h)": 0.03, "手动总价(元)": 0.0, "判断依据": basis,
            "置信度": confidence, "类型": kind, "用户确认": confirmed}


def _capability(config: dict, machine: str) -> dict:
    return config.get("machine_capabilities", {}).get(machine, {})


def _fits(step: dict, cap: dict) -> bool:
    """预留压板、夹具与刀具接近空间，不能把刚好塞进行程的零件判为可加工。"""
    dims = sorted(step.get("dimensions", [0, 0, 0]), reverse=True)
    travel = sorted([cap.get("x", 0), cap.get("y", 0), cap.get("z", 0)], reverse=True)
    reserve = [max(30.0, value * 0.05) for value in dims]
    return (all(value + margin <= available for value, margin, available in zip(dims, reserve, travel))
            and step.get("net_weight", 0) * 1.10 <= cap.get("max_weight", 0))


def _annotate(row: dict, setup: str, direction: str, feature: str, tool: str, detail: str) -> dict:
    """将自动工时的来源随工序保存，供确认表和导出表人工复核。"""
    row.update({"装夹编号": setup, "加工方向": direction, "识别特征": feature,
                "刀具类型": tool, "时间计算依据": detail})
    return row


def estimate_operations(step: dict, drawing: dict, config: dict, product_type: str = "自动识别") -> dict:
    if not step or not step.get("available"):
        return {"rows": [], "summary": "未取得可靠 STEP 实体，无法自动估算；请人工填写工时。", "classification": "不确定"}
    dims = step["dimensions"]; max_dim = max(dims); weight = step["net_weight"]
    holes = drawing.get("hole_features", []); threads = drawing.get("threaded_count", 0)
    geometry_turn_conf, turn_evidence = turning_geometry_confidence(step)
    drawing_turn = drawing.get("requires_turning", False)
    thread_diameters = drawing.get("thread_diameters", [])
    large_thread = any(float(value) >= 40 for value in thread_diameters)
    rotating_type = product_type in {"小型阀体", "阀体", "泵体", "回转体", "车削件"}
    drawing_turn = drawing_turn or large_thread or rotating_type
    small_valve = max_dim <= 250 and weight <= 10 and drawing_turn
    large_frame = max_dim >= 1500 and weight >= 500
    # 大型精密板/底座：尺寸接近普通立加极限，且图纸存在精磨或大量孔表。
    # 条件完全来自尺寸、孔特征与精度要求，不依赖图号或文件名。
    large_precision_plate = (max_dim >= 1000 and (drawing.get("explicit_grinding")
                             or drawing.get("grinding_required")
                             or (max_dim >= 1100 and drawing.get("drilled_count", 0) >= 120)))
    # 中等尺寸的精密框架/板件同样不能走普通 CNC 的简化公式。它们常有
    # 正反面、侧向孔和研磨精度面，虽未超过 1000 mm，实际仍需龙门分序。
    medium_precision_plate = (600 <= max_dim < 1000 and
                              (drawing.get("explicit_grinding") or drawing.get("grinding_required")) and
                              (drawing.get("drilled_count", 0) >= 30 or drawing.get("threaded_count", 0) >= 20))
    precision_plate = large_precision_plate or medium_precision_plate
    multi_side = bool(drawing.get("side_feature_count", 0) >= 3 and (
        drawing.get("cross_wall_coaxial") or drawing.get("horizontal_deep_holes") or
        any(term in drawing.get("gd_terms", []) for term in ["位置度", "同轴度"])
    ))
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
    elif large_precision_plate:
        classification, primary = "大型精密板件/多孔铸件", "龙门铣"
        surface_signals = max(8, int(drawing.get("machined_surface_estimate", 0)))
        hole_count = sum(int(item.get("count", 0)) for item in holes)
        thread_count = sum(int(item.get("count", 0)) for item in drawing.get("thread_features", []))
        countersink_count = sum(int(item.get("count", 0)) for item in holes if item.get("countersink") or item.get("counterbore"))
        hole_groups = max(1, len(holes) + len(drawing.get("thread_features", [])))
        # 孔加工由每孔定位/切削、沉头、设备刚性攻牙、换刀与大件风险组成。
        # 螺纹底孔会在下面按规格逐行生成，汇总钻孔中先排除，防止重复计时。
        thread_labels = {str(item.get("label", "")) for item in drawing.get("thread_features", [])}
        ordinary_holes = [item for item in holes if str(item.get("label", "")) not in thread_labels]
        drill_hours = sum(float(item.get("count", 0)) * (0.012 + min(float(item.get("diameter", 0)), 25) * 0.00065)
                          for item in ordinary_holes if not item.get("countersink"))
        sink_hours = countersink_count * 0.010
        tap_hours = sum(float(item.get("count", 0)) * (0.012 + min(float(item.get("nominal_diameter", 0)), 20) * 0.00070) for item in drawing.get("thread_features", []))
        # 攻牙保留为各规格独立工序，供用户逐组改为人工/混合攻牙；此处不再重复计入。
        hole_total = drill_hours + sink_hours + hole_groups * 0.055 + 1.15 + max_dim / 2600
        # 大平面、正反面、侧向面分别独立装夹；面积/精度只作修正，不按 STEP 面数累加。
        rough_top = 3.0 + surface_signals * 0.08 + max_dim / 3000
        finish_top = 2.5 + surface_signals * 0.08 + (0.5 if (drawing.get("min_tolerance") or 1.0) <= 0.05 else 0.2)
        side_hours = 1.8 + 0.08 * len(drawing.get("datum_references", [])) + min(0.8, hole_count / 800)
        setup_hours = 0.85 + max_dim / 2200 + (0.20 if weight >= 100 else 0.0)
        side_machine = "龙门铣" if _capability(config, "龙门铣").get("side_head", False) else "卧式加工中心"
        evidence += [f"最大尺寸 {max_dim:.0f} mm，预留夹具空间后普通 CNC 不满足；选择龙门铣",
                     f"PDF 孔表识别 {hole_count} 个孔加工动作、{thread_count} 个螺纹，按数量逐项计时",
                     "存在精磨/高精度面信号，正反面、侧面和精磨前需分开装夹复核"]
        rows = [
            _annotate(_row("OP10 吊装、压板布置与基准打表", primary, setup_hours, "大型毛坯吊装、垫铁、压板与首面基准", "高"), "OP10", "+Z", "底部基准面和支脚面", "吊具/压板/百分表", f"尺寸 {max_dim:.0f} mm；每次装夹含吊装、清理和打表"),
            _annotate(_row("OP10 正面基准与凸台平面粗精加工", primary, rough_top + finish_top, "加工面数量、平面面积与精度面信号", "中"), "OP10", "+Z", "正面基准、凸台端面和定位面", "面铣刀/立铣刀", f"{surface_signals} 个加工面信号；粗精加工分开走刀"),
            _annotate(_row("OP20 翻面、背部大平面与支脚面加工", primary, setup_hours + rough_top * 0.70 + finish_top * 0.70, "反面加工方向与翻转重新找正", "高"), "OP20", "-Z", "背部大平面、两个支脚加工面", "吊具/面铣刀", "翻面后重新吊装、定位、开粗和精铣"),
            _annotate(_row("OP30 侧面孔系与 M20 特征加工", side_machine, setup_hours * 0.70 + side_hours, "侧面加工方向；角度头可由龙门完成，否则转卧加", "中"), "OP30", "+X/-X", "端部立面、侧面 M20 与侧孔", "角度头/钻头/镗刀", "侧向特征单独装夹，含二次找正与侧孔加工"),
            _annotate(_row("OP10/20 孔系钻削、沉孔与沉头", primary, hole_total, "孔表的数量、孔径、沉头逐项累加；攻牙另列", "高"), "OP10/20", "+Z/-Z", "孔表 A-AC 与独立孔特征", "钻头/沉头刀", f"钻孔 {drill_hours:.2f}h + 沉头 {sink_hours:.2f}h + 换刀/试切/大件修正；攻牙另列"),
            _annotate(_row("OP30 去毛刺、清理与尺寸复核", "人工", 0.80 + hole_count * 0.012, "大量孔口、沉头和精度面保护", "中"), "OP30", "检验", "孔口、精度面和装夹基准", "去毛刺工具/量具", f"{hole_count} 个孔加工动作的孔口处理与抽检"),
        ]
        for row in rows:
            row.update({"特征标签": "A–AC" if "孔系" in row["工序"] else "",
                        "特征类型": "结构化孔表" if "孔系" in row["工序"] else row.get("识别特征", ""),
                        "规格": "孔表多规格钻孔/沉头/刚性攻牙" if "孔系" in row["工序"] else "",
                        "数量来源": "PDF孔特征表" if "孔系" in row["工序"] else "STEP尺寸+PDF图纸",
                        "识别置信度": "高" if "孔系" in row["工序"] else row.get("置信度", "中")})
        programming, fixture = 3.0 + min(1.0, hole_groups * 0.025), 0.0
    elif medium_precision_plate:
        classification, primary = "精密板件/多方向孔系", "龙门铣"
        hole_count = sum(int(item.get("count", 0)) for item in holes)
        thread_count = sum(int(item.get("count", 0)) for item in drawing.get("thread_features", []))
        feature_groups = max(1, len(holes) + len(drawing.get("thread_features", [])))
        side_machine = "龙门铣" if _capability(config, "龙门铣").get("side_head", False) else "卧式加工中心"
        # 孔时间由已识别孔数、规格组、定位/换刀与大件排屑辅助组成；攻牙和
        # 螺纹底孔仍由末尾的逐规格工序单独加入，避免重复计价。
        nonthread_hole_time = 1.20 + hole_count * 0.014 + feature_groups * 0.10
        setup_1 = 1.15 + max_dim / 4200
        setup_2 = 1.05 + max_dim / 4600
        setup_3 = 0.90 + max_dim / 6000
        rough = 2.10 + min(0.85, step.get("total_planar_area_m2", 0.0) * 0.70) + min(0.45, hole_count / 260)
        finish = 3.25 + min(0.80, step.get("largest_planar_area_m2", 0.0) * 0.55) + (0.35 if drawing.get("two_sided_required") else 0.15)
        side_hours = 1.85 + (0.45 if drawing.get("two_sided_required") else 0.0) + min(0.55, thread_count / 180)
        evidence += [
            f"最大尺寸 {max_dim:.0f} mm，预留压板和翻面空间后，正反面采用龙门铣分序加工",
            f"PDF坐标孔表汇总 {hole_count} 个孔动作、{thread_count} 个螺纹；按规格组计算定位、钻削与攻牙",
            "图纸存在研磨/精度面信号，正面、反面、侧面及磨削前需独立装夹和找正",
        ]
        rows = [
            _annotate(_row("OP10 首面吊装、压板与基准打表", primary, setup_1, "首面毛坯基准、压板和打表", "高"), "OP10", "+Z", "首面基准和凸台", "压板/百分表", "第一方向吊装、压板、清理定位面和打表"),
            _annotate(_row("OP10 首面粗铣与半精铣", primary, rough, "加工覆盖面积、铸件余量与断续切削", "中"), "OP10", "+Z", "基准面、圆环和安装凸台", "面铣刀/立铣刀", "按覆盖面积、分区走刀和铸件余量计算"),
            _annotate(_row("OP20 翻面、反面重新找正", primary, setup_2, "反面加工必须翻转并重新建立基准", "高"), "OP20", "-Z", "反面基准和精度面", "吊装/压板/百分表", "翻面、压板、重新打表和定位面清理"),
            _annotate(_row("OP20 反面半精铣与精铣", primary, finish, "反面精度面、圆环和薄壁防变形", "中"), "OP20", "-Z", "反面大平面、台阶与精度面", "面铣刀/立铣刀", "半精后精加工，加入薄壁框架防变形修正"),
            _annotate(_row("OP10/20 孔系钻削、沉孔、沉头与精孔", primary, nonthread_hole_time, "孔数量、规格组、定位移动、换刀和排屑", "高"), "OP10/20", "+Z/-Z", "坐标孔表及视图孔特征", "钻头/沉头刀/铰刀", f"{hole_count} 个孔动作，{feature_groups} 个规格组；攻牙另列"),
            _annotate(_row("OP30 侧面及两面孔加工", side_machine, side_hours, "两面加工文字与侧向特征需独立方向", "中"), "OP30", "+X/-X", "侧面孔、两面孔和端面", "角度头或侧立夹具", "有角度头时龙门完成；否则改由卧加或侧立装夹"),
            _annotate(_row("OP30 去毛刺、精度面保护与最终检验", "人工", 3.20 + min(1.20, hole_count * 0.012), "多孔口去毛刺、精度面遮蔽和尺寸复核", "中"), "OP30", "检验", "孔口与精度面", "去毛刺工具/量具", "孔口、沉头和研磨前后的清理与抽检"),
        ]
        programming, fixture = 2.50 + min(0.50, feature_groups * 0.04), 0.0
    elif small_valve:
        classification, primary = "小型阀体", "CNC加工中心"
        evidence += ["小尺寸阀体", "2D 存在大直径螺纹/密封槽或同轴孔", turn_evidence]
        rows = [_row("车床第一次装夹：端面、内孔、台阶、槽、螺纹", "车床", 0.35, "M40+ 螺纹/密封槽/同轴回转特征", "高"),
                _row("车床翻面：第二端端面和内孔", "车床", 0.20, "两端同轴加工", "高"),
                _row("CNC 法兰面与精孔", primary, 0.35, "顶部/侧面加工方向", "中"),
                _row("CNC 钻孔、侧孔、斜孔", primary, 0.35, f"识别到约 {drawing.get('drilled_count', 0)} 个成组孔", "中"),
                _row("CNC 攻牙、倒角与检测", primary, 0.25, f"识别到约 {threads} 个螺纹", "中")]
        programming, fixture = 1.5, 0.5
    elif drawing.get("slot_candidate", {}).get("candidate") and max_dim <= 800:
        # 非回转铸件：图纸出现加工基准、后加工面和多个剖面时，STEP 应重点复核内部凹槽。
        # 时间由加工面/剖面/孔数量和转序计算，不以该零件的历史报价直接写死。
        classification, primary = "多方向铸件/内部槽加工", "CNC加工中心"
        surface_count = int(drawing.get("machined_surface_estimate", 6))
        section_count = int(drawing.get("section_count", 0))
        hole_groups = len(holes)
        direction_groups = len(step.get("planar_direction_groups", []))
        external_finish = 0.34 + surface_count * 0.040 + min(0.15, hole_groups * 0.022)
        external_rough = 0.25 + surface_count * 0.025
        slot_depth_factor = min(0.22, 0.04 * max(1, section_count))
        slot_rough = 0.33 + surface_count * 0.020 + slot_depth_factor
        slot_finish = 0.18 + surface_count * 0.010 + slot_depth_factor * 0.35
        transfer = 0.10 + 0.02 * max(1, len(drawing.get("datum_references", [])))
        inspection = 0.10 + (0.05 if drawing.get("min_tolerance") and drawing["min_tolerance"] <= 0.2 else 0.0)
        evidence += [f"识别 {surface_count} 个后加工面信号、{section_count} 个剖面和 {len(drawing.get('datum_references', []))} 个基准",
                     f"STEP 平面法向分为 {direction_groups or '待 STEP 引擎确认'} 组刀轴方向",
                     "内部凹槽候选与主要平面不在同一刀轴方向，建议第二台 CNC 独立装夹"]
        rows = [
            _annotate(_row("设备1：外部基准面、凸台与侧面开粗", primary, external_rough,
                           "后加工面/基准/剖面数量推导", "中"), "OP10", "+Z/侧向", "底面、凸台端面、耳板和定位面",
                      "面铣刀/立铣刀", f"{surface_count} 个加工面 × 分层开粗；不含内部槽"),
            _annotate(_row("设备1：外部平面、孔和凸台精加工", primary, external_finish,
                           "加工面、孔组及基准 A-D", "中"), "OP10", "+Z/侧向", "外部平面、凸台、孔和侧面",
                      "面铣刀/钻头/立铣刀", f"{surface_count} 个加工面 + {hole_groups} 组孔的精加工与换刀"),
            _annotate(_row("设备1：单件上下料、夹紧与基准确认", primary, 0.15,
                           "第一方向单件重复装夹", "高"), "OP10", "+Z", "外部基准", "压板/夹具", "上料、夹紧、清理定位面、简易找正"),
            _annotate(_row("转序：设备2重新定位与二次找正", primary, transfer,
                           "内部槽需单独刀轴方向与装夹", "高"), "OP20", "内腔方向", "转序/二次定位", "专用软爪或定位夹具",
                      f"{len(drawing.get('datum_references', []))} 个基准关联的转序定位和测量"),
            _annotate(_row("设备2：内部U形槽分层开粗", primary, slot_rough,
                           "多个剖面＋后加工基准形成的内部槽候选", "中"), "OP20", "内腔方向", "U形槽底、两侧壁和内圆角",
                      "长伸出立铣刀", f"{section_count} 个剖面信号；按槽深分层、长径比降速及排屑预留"),
            _annotate(_row("设备2：内部U形槽底/侧壁/圆角精加工", primary, slot_finish,
                           "内槽独立精加工，不能并入普通平面铣", "中"), "OP20", "内腔方向", "槽底、侧壁、内部圆弧",
                      "长颈球刀/立铣刀", "槽底与侧壁精加工、避让、空行程和机内测量"),
            _annotate(_row("设备2：下机检验与去毛刺", "人工", inspection,
                           "多基准转序后的尺寸复核", "中"), "OP20", "检验", "基准相关加工面", "量具/去毛刺工具",
                      "转序后基准尺寸复核、槽口去毛刺"),
        ]
        programming, fixture = 1.6 + section_count * 0.15, 0.35 + surface_count * 0.02
    else:
        # 多方向箱体优先卧加；不能因普通立加 Y 轴略不足就先被龙门分支抢走。
        if hmc_fit and (multi_side or product_type == "箱体/多方向孔系"):
            classification, primary = "箱体/多方向孔系", "卧式加工中心"; evidence.append("多侧孔/镗孔和位置度，同一装夹有优势")
        elif not cnc_fit and gantry_fit:
            classification, primary = "大型铣削件", "龙门铣"; evidence.append("超过普通 CNC 行程或承重")
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
        if drawing_turn and (geometry_turn_conf >= 0.45 or large_thread or rotating_type):
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
        rows.append(_row("两件配对磨削", "磨床", pair_time/2, f"每对约 {pair_time:.1f} h；两件等高交付", "高", "基础", True,
                         calculation_type="每对产品", count=2, batch_hours=pair_time))
        grinding_reason = f"两件等高：每对磨削 {pair_time:.1f} h，单件分摊 {pair_time/2:.2f} h"
    elif drawing.get("explicit_grinding") or (flat_high and 150 <= max_dim <= 800) or (drawing.get("roughness") and min(drawing["roughness"]) <= 0.8):
        if _fits(step, _capability(config, "磨床")) or precision_plate:
            # 大件精磨不能按普通小平面面积公式压成几分钟；长度、精度面数量与重新找正共同决定。
            h = (min(10.0, max(6.0, 2.6 + max_dim / 420 + max_plane * 1.5))
                 if large_precision_plate else (min(5.0, max(3.0, 1.8 + max_dim / 650 + max_plane * 1.2)) if medium_precision_plate else max(0.4, 0.3 + max_plane*2.0 + (0.2 if flat_high else 0))))
            basis = "图纸明确精磨；长精度面与 0.01 级平面/平行度需大型平面磨削" if large_precision_plate else ("图纸明确要求研磨，精度面需重新找正并按面积和长度平面磨削" if medium_precision_plate else "明确磨削或大平面 0.01 级精度/Ra0.8")
            rows.append(_annotate(_row("关键平面精磨", "磨床", h, basis, "高", "基础", True), "OP40", "+Z/-Z", "阴影精度面/长导轨面", "平面磨砂轮", f"长度 {max_dim:.0f} mm、精度面重新找正和余量磨削"))
            grinding_reason = "关键平面精度超出常规精铣稳定能力，建议磨床"
        else:
            grinding_reason = "存在高精度磨削信号，但尺寸/承重可能超过普通磨床；需人工确认精密龙门或外协大型磨床"
    # 首件找正与每件上下料分开，防止首次工作被重复乘数量。
    loading_rows = []
    for row in rows:
        # 仅拆分纯“装夹找正”工序；车床首装那一行还含端面/内孔/螺纹切削，不能整行误算为一次性准备。
        if "装夹找正" in row["工序"] and "上下料" not in row["工序"]:
            first_hours = row["单件时间(h)"]
            row["工序"] = "首件夹具安装及找正"
            row["计算类型"], row["每批时间(h)"], row["单件时间(h)"], row["推荐时间(h)"] = "每批一次", first_hours, 0.0, 0.0
            load_hours = 0.25 if primary == "龙门铣" else 0.10
            loading_rows.append(_row("单件上下料、夹紧与定位面清理", primary, load_hours, "每件重复上料、夹紧、简单找正和下料", "中"))
        if "攻牙" in row["工序"]:
            row["工序"], row["单件时间(h)"], row["推荐时间(h)"] = "倒角", 0.08, 0.08
            loading_rows.append(_row("机内测量", row["推荐设备"], 0.07, "关键尺寸机内复测", "中"))
            loading_rows.append(_row("下机检验与去毛刺", "人工", 0.10, "下机外观、尺寸与去毛刺", "中"))
    rows.extend(loading_rows)
    rows.insert(0, _row("CNC编程、首件工艺准备与试切", primary, 0.0, "编程、首件对刀、试切和程序验证", "中", "基础", True,
                        calculation_type="每批一次", batch_hours=programming))
    # 螺纹组必须逐行显示。大型孔表件默认推荐设备刚性攻牙，但仍允许改成人工/混合。
    for group in drawing.get("thread_groups", []):
        diameter, count = group["直径"], group["数量"]
        device_time = 0.012 if diameter <= 6 else (0.018 if diameter <= 12 else 0.030)
        # 底孔加工独立为设备工序；无论后续选人工还是设备攻牙，底孔仍需 CNC/龙门/卧加完成。
        drill_time = (0.010 if diameter <= 6 else (0.015 if diameter <= 12 else 0.025)) * count
        rows.append(_row(f"{group['规格']} 螺纹底孔", primary, drill_time, f"图纸识别 {group['规格']}，共 {count} 个；底孔由设备钻削", "中", "基础", True,
                         calculation_type="每件", count=count))
        default_mode = "设备刚性攻牙" if precision_plate else "待确认"
        rows.append(_row(f"{group['规格']} 螺纹加工", primary, device_time,
                         f"图纸识别 {group['规格']}，共 {count} 个；" + ("默认设备刚性攻牙，可逐组修改" if precision_plate else "请确认攻牙方式"),
                         "高" if precision_plate else "中", "基础", precision_plate,
                         calculation_type="每件", count=count, tapping_mode=default_mode))
    return {"rows": rows, "classification": classification, "evidence": evidence, "programming_hours": programming,
            "fixture_hours": fixture, "grinding_assessment": grinding_reason, "turning_geometry_confidence": geometry_turn_conf}
