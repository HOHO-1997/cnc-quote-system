"""单件、整批与工序类型（每件/每批/每对）的报价计算。"""
from __future__ import annotations

import math


def _number(value: object, default: float = 0.0) -> float:
    """编辑表格的空白单元格会变成 NaN，报价时按 0 处理。"""
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def surface_cost(selected: list[dict], net_weight: float, area_m2: float, quantity: int) -> tuple[float, list[dict]]:
    total, details = 0.0, []
    for item in selected:
        if not item.get("启用", False): continue
        basis, rate = item.get("计价方式", "按kg"), float(item.get("单价", 0.0))
        if basis == "按kg": amount = net_weight * rate
        elif basis == "按平方米": amount = area_m2 * rate
        elif basis == "按件": amount = rate
        else: amount = float(item.get("手动报价", 0.0))
        amount = max(amount, float(item.get("最低收费", 0.0))) + float(item.get("遮蔽加工面费用", 0.0)) + float(item.get("挂具费用", 0.0))
        if quantity < int(item.get("小批量数量", 0) or 0): amount += float(item.get("小批量附加费", 0.0))
        total += amount; details.append({"名称": item.get("名称"), "单件金额": amount, "计价方式": basis})
    return total, details


def _is_efficiency_eligible(process: str) -> bool:
    """批量效率只能作用于重复辅助动作，绝不降低材料、切削、钻孔或攻牙。"""
    return any(word in str(process) for word in ["上下料", "夹紧", "定位面", "换刀", "机内测量", "下机检验", "去毛刺", "倒角"])


def _processing_discount(config: dict, quantity: int) -> float:
    """按数量获取加工费折扣；真实工时不会被此函数修改。"""
    for item in config.get("batch_processing_discounts", []):
        if int(item.get("最小数量", 1)) <= quantity <= int(item.get("最大数量", 999999)):
            return min(1.0, max(0.0, _number(item.get("加工费系数", 1.0), 1.0)))
    return 1.0


def _operation_schedule(row: dict, product_quantity: int, rate_map: dict, manual_rate: float, efficiency: float = 1.0) -> dict:
    """把一条已确认工序展开为整批时间/金额，攻牙的设备与人工可拆分。"""
    kind = row.get("计算类型", "每件")
    equipment = row.get("推荐设备", "CNC加工中心")
    tapping_machine = row.get("攻牙设备", equipment)
    unit_h = _number(row.get("单件时间(h)", row.get("推荐时间(h)", 0.0)))
    batch_h = _number(row.get("每批时间(h)", 0.0))
    operation_count = max(1, int(_number(row.get("数量", 1), 1)))
    mode = row.get("攻牙方式", "无螺纹加工")
    is_tapping = "螺纹加工" in str(row.get("工序", ""))
    # 真实工时永远不因批量报价折扣而改变；efficiency 参数为旧数据兼容保留。
    adjusted_unit_h = unit_h
    equipment_h = labor_h = 0.0
    if is_tapping:
        # 单件时间为单孔设备攻牙时间；人工单孔时间可单独维护。
        if mode == "设备刚性攻牙": equipment_h = unit_h * operation_count * product_quantity
        elif mode == "人工攻牙": labor_h = _number(row.get("人工单孔时间(h)", 0.03), 0.03) * operation_count * product_quantity
        elif mode == "混合攻牙":
            device_count = max(0, int(_number(row.get("设备攻牙数量", 0)))); manual_count = max(0, int(_number(row.get("人工攻牙数量", 0))))
            equipment_h = unit_h * device_count * product_quantity
            labor_h = _number(row.get("人工单孔时间(h)", 0.03), 0.03) * manual_count * product_quantity
        # “待确认”或“无螺纹加工”不进入报价。
    elif kind == "每件": equipment_h = adjusted_unit_h * product_quantity
    elif kind == "每批一次": equipment_h = batch_h or unit_h
    elif kind == "每对产品": equipment_h = math.ceil(product_quantity / 2) * (batch_h or unit_h * 2)
    elif kind == "手动总价": pass
    manual_total = _number(row.get("手动总价(元)", 0.0)) if kind == "手动总价" else 0.0
    billing_equipment = tapping_machine if is_tapping and mode != "人工攻牙" else equipment
    equipment_amount = equipment_h * float(rate_map.get(billing_equipment, manual_rate))
    labor_amount = labor_h * manual_rate
    # 人工攻牙必须在报价表中明确显示“人工工位”，不能误导为 CNC 占机。
    display_equipment = "人工工位" if is_tapping and mode == "人工攻牙" else billing_equipment
    return {"工序": row.get("工序"), "计算类型": kind, "执行方式": mode if is_tapping else "设备加工", "设备": display_equipment, "数量": operation_count, "单孔时间(h)": unit_h if is_tapping else 0.0, "单件时间(h)": adjusted_unit_h if not is_tapping else unit_h * operation_count,
            "每批时间(h)": batch_h, "整批设备时间(h)": equipment_h, "整批人工时间(h)": labor_h,
            "单价(元/h)": manual_rate if display_equipment == "人工工位" else float(rate_map.get(billing_equipment, manual_rate)), "人工单价(元/h)": manual_rate,
            "整批金额(元)": equipment_amount + labor_amount + manual_total, "设备金额(元)": equipment_amount,
            "人工金额(元)": labor_amount, "手动总价(元)": manual_total, "判断依据": row.get("判断依据", "")}


def calculate_quote(data: dict, rows: list[dict], config: dict, additional: list[dict], surfaces: list[dict], include_tiers: bool = True) -> dict:
    quantity = max(1, int(data.get("quantity", 1))); mode = data.get("quote_mode", "成本加利润")
    material = data["material"]; blank_weight = float(data.get("casting_weight", data.get("net_weight", 0))); net_weight = float(data.get("net_weight", 0))
    rate_map = config["machine_rates"] if mode == "成本加利润" else config.get("direct_machine_rates", config["machine_rates"])
    manual_rate = float(config.get("manual_labor_rate", 35.0))
    # rows 已经来自“确认带入报价”后的 confirmed_operations；不再因旧复选框二次过滤。
    # 仅精度/检验等明确标为未启用的追加工序不进入报价。
    confirmed_rows = [row for row in rows if bool(row.get("启用", True))]
    processing_discount = _processing_discount(config, quantity)
    schedules = []
    for row in confirmed_rows:
        # 混合攻牙拆成两条可见工序：设备部分和人工部分分别进入各自成本。
        is_tapping = "螺纹加工" in str(row.get("工序", ""))
        if is_tapping and row.get("攻牙方式") == "混合攻牙":
            for mode, count, suffix in [("设备刚性攻牙", row.get("设备攻牙数量", 0), "（设备刚性攻牙）"),
                                        ("人工攻牙", row.get("人工攻牙数量", 0), "（人工攻牙）")]:
                if int(_number(count, 0)) <= 0:
                    continue
                split = {**row, "工序": str(row.get("工序", "")) + suffix, "攻牙方式": mode, "数量": int(_number(count, 0))}
                schedules.append(_operation_schedule(split, quantity, rate_map, manual_rate))
        else:
            schedules.append(_operation_schedule(row, quantity, rate_map, manual_rate))
    equipment_batch = sum(item["设备金额(元)"] for item in schedules); labor_batch = sum(item["人工金额(元)"] for item in schedules)
    manual_total_batch = sum(item["手动总价(元)"] for item in schedules)
    raw_rate = float(config["materials"][material]); sale_rate = float(data.get("casting_sales_rate", config.get("casting_sales_prices", {}).get(material, 0.0)))
    material_rate = raw_rate if mode == "成本加利润" else sale_rate
    casting_per_unit = blank_weight * material_rate
    per_unit_extra = batch_extra = 0.0; additional_details = []
    for item in additional:
        if not item.get("启用", False): continue
        method, value = item.get("计价方式", "单件费用"), float(item.get("金额", 0.0))
        if method == "按重量": amount = value * blank_weight; per_unit_extra += amount
        elif method == "整批一次性费用": amount = value; batch_extra += amount
        else: amount = value; per_unit_extra += amount
        additional_details.append({"名称": item.get("项目"), "方式": method, "金额": amount})
    surface_per_unit, surface_details = surface_cost(surfaces, net_weight, float(data.get("surface_area_m2", 0)), quantity)
    packaging = float(data.get("packaging_cost", 0.0)); packaging_per_unit = packaging if data.get("packaging_mode") != "整批费用" else 0.0
    if data.get("packaging_mode") == "整批费用": batch_extra += packaging
    recurring_per_unit = casting_per_unit + per_unit_extra + surface_per_unit + packaging_per_unit
    one_time_schedules = [s for s in schedules if s["计算类型"] in {"每批一次", "手动总价"}]
    one_time_operation_cost = sum(s["整批金额(元)"] for s in one_time_schedules)
    one_time_equipment = sum(s["设备金额(元)"] for s in one_time_schedules)
    one_time_labor = sum(s["人工金额(元)"] for s in one_time_schedules)
    repeated_equipment_batch = equipment_batch - one_time_equipment
    repeated_labor_batch = labor_batch - one_time_labor
    repeated_processing_batch = repeated_equipment_batch + repeated_labor_batch
    discounted_processing_batch = repeated_processing_batch * processing_discount
    # 只有每件重复加工费参加折扣，材料与所有一次性费用不参加。
    batch_cost = recurring_per_unit * quantity + discounted_processing_batch + one_time_operation_cost + batch_extra
    unit_cost = batch_cost / quantity
    multiplier = float(config.get("profit_multiplier", 1.2)); batch_price = batch_cost * multiplier if mode == "成本加利润" else batch_cost
    unit_price = batch_price / quantity
    base_schedules = [s for s, row in zip(schedules, confirmed_rows) if row.get("类型", "基础") == "基础"]
    extra_schedules = [s for s, row in zip(schedules, confirmed_rows) if row.get("类型") == "追加"]
    pure_cutting = sum(s["整批设备时间(h)"] for s in schedules if not any(x in s["工序"] for x in ["装夹", "上下料", "编程", "试切"])) / quantity
    loading = sum(s["整批设备时间(h)"] for s in schedules if "上下料" in s["工序"]) / quantity
    batch_prepare = sum(s["整批设备时间(h)"] for s in schedules if s["计算类型"] == "每批一次")
    pair_warning = any(s["计算类型"] == "每对产品" for s in schedules) and quantity % 2 == 1
    repeated_per_unit = recurring_per_unit + repeated_processing_batch / quantity
    tapping_labor_batch = sum(s["人工金额(元)"] for s in schedules if s.get("执行方式") == "人工攻牙")
    tapping_labor_hours = sum(s["整批人工时间(h)"] for s in schedules if s.get("执行方式") == "人工攻牙")
    result = {"unit_cost": unit_cost, "unit_price": unit_price, "batch_cost": batch_cost, "batch_price": batch_price, "one_time_cost": batch_extra + one_time_operation_cost,
            "additional_one_time_cost": batch_extra, "operation_one_time_cost": one_time_operation_cost,
            "repeated_per_unit_cost": repeated_per_unit, "one_time_per_unit": (batch_extra + one_time_operation_cost) / quantity,
            "processing_discount": processing_discount, "nonprocessing_per_unit": recurring_per_unit, "raw_processing_per_unit": repeated_processing_batch / quantity,
            "discounted_processing_per_unit": discounted_processing_batch / quantity,
            "tapping_labor_per_unit": tapping_labor_batch / quantity, "tapping_labor_hours_per_unit": tapping_labor_hours / quantity,
            "other_labor_per_unit": max(0.0, labor_batch / quantity - tapping_labor_batch / quantity),
            "casting_per_unit": casting_per_unit, "material_rate": material_rate, "equipment_per_unit": equipment_batch / quantity,
            "labor_per_unit": labor_batch / quantity, "surface_per_unit": surface_per_unit, "packaging_per_unit": packaging_per_unit,
            "operation_schedules": schedules, "base_time": sum(s["整批设备时间(h)"] + s["整批人工时间(h)"] for s in base_schedules) / quantity,
            "extra_time": sum(s["整批设备时间(h)"] + s["整批人工时间(h)"] for s in extra_schedules) / quantity,
            "pure_cutting_time": pure_cutting, "loading_time": loading, "batch_preparation_time": batch_prepare,
            "batch_equipment_time": sum(s["整批设备时间(h)"] for s in schedules), "pair_warning": pair_warning,
            "surface_details": surface_details, "additional_details": additional_details, "confirmed_rows": confirmed_rows, "quote_mode": mode,
            "input_operation_count": len(rows), "enabled_operation_count": len(confirmed_rows), "final_billed_operation_count": len(schedules),
            "batch_efficiency": 1.0}
    if include_tiers:
        sample_quantity = max(1, int(_number(data.get("sample_quantity", 1), 1)))
        sample_data = {**data, "quantity": sample_quantity}
        sample = calculate_quote(sample_data, rows, config, additional, surfaces, include_tiers=False)
        result.update({"sample_quantity": sample_quantity, "sample_cost": sample["batch_cost"], "sample_unit_price": sample["unit_price"]})
        tiers = data.get("tier_rows") or [{"数量": n} for n in [1, 5, 10, 50, 100]]
        tier_results = []
        for tier in tiers:
            tier_quantity = max(1, int(_number(tier.get("数量", 1), 1)))
            tier_result = calculate_quote({**data, "quantity": tier_quantity}, rows, config, additional, surfaces, include_tiers=False)
            tier_results.append({"数量": tier_quantity, "加工费折扣": f"{tier_result['processing_discount']:.0%}",
                                 "原单件加工费": tier_result["raw_processing_per_unit"], "优惠后加工费": tier_result["discounted_processing_per_unit"],
                                 "一次性费用分摊": tier_result["one_time_per_unit"], "其他单件成本": recurring_per_unit,
                                 "批量单价": tier_result["unit_price"], "整批报价": tier_result["batch_price"]})
        result["tier_results"] = tier_results
    return result
