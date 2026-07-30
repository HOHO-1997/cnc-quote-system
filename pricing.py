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


def _operation_schedule(row: dict, product_quantity: int, rate_map: dict, manual_rate: float) -> dict:
    """把一条已确认工序展开为整批时间/金额，攻牙的设备与人工可拆分。"""
    kind = row.get("计算类型", "每件")
    equipment = row.get("推荐设备", "CNC加工中心")
    unit_h = _number(row.get("单件时间(h)", row.get("推荐时间(h)", 0.0)))
    batch_h = _number(row.get("每批时间(h)", 0.0))
    operation_count = max(1, int(_number(row.get("数量", 1), 1)))
    mode = row.get("攻牙方式", "无螺纹加工")
    is_tapping = "螺纹加工" in str(row.get("工序", ""))
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
    elif kind == "每件": equipment_h = unit_h * product_quantity
    elif kind == "每批一次": equipment_h = batch_h or unit_h
    elif kind == "每对产品": equipment_h = math.ceil(product_quantity / 2) * (batch_h or unit_h * 2)
    elif kind == "手动总价": pass
    manual_total = _number(row.get("手动总价(元)", 0.0)) if kind == "手动总价" else 0.0
    equipment_amount = equipment_h * float(rate_map.get(equipment, manual_rate))
    labor_amount = labor_h * manual_rate
    return {"工序": row.get("工序"), "计算类型": kind, "设备": equipment, "数量": operation_count, "单件时间(h)": unit_h,
            "每批时间(h)": batch_h, "整批设备时间(h)": equipment_h, "整批人工时间(h)": labor_h,
            "单价(元/h)": float(rate_map.get(equipment, manual_rate)), "人工单价(元/h)": manual_rate,
            "整批金额(元)": equipment_amount + labor_amount + manual_total, "设备金额(元)": equipment_amount,
            "人工金额(元)": labor_amount, "手动总价(元)": manual_total, "判断依据": row.get("判断依据", "")}


def calculate_quote(data: dict, rows: list[dict], config: dict, additional: list[dict], surfaces: list[dict]) -> dict:
    quantity = max(1, int(data.get("quantity", 1))); mode = data.get("quote_mode", "成本加利润")
    material = data["material"]; blank_weight = float(data.get("casting_weight", data.get("net_weight", 0))); net_weight = float(data.get("net_weight", 0))
    rate_map = config["machine_rates"] if mode == "成本加利润" else config.get("direct_machine_rates", config["machine_rates"])
    manual_rate = float(config.get("manual_labor_rate", 35.0))
    confirmed_rows = [row for row in rows if bool(row.get("用户确认", False))]
    schedules = [_operation_schedule(row, quantity, rate_map, manual_rate) for row in confirmed_rows]
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
    operation_batch = equipment_batch + labor_batch + manual_total_batch
    batch_cost = recurring_per_unit * quantity + operation_batch + batch_extra
    unit_cost = batch_cost / quantity
    multiplier = float(config.get("profit_multiplier", 1.2)); batch_price = batch_cost * multiplier if mode == "成本加利润" else batch_cost
    unit_price = batch_price / quantity
    base_schedules = [s for s, row in zip(schedules, confirmed_rows) if row.get("类型", "基础") == "基础"]
    extra_schedules = [s for s, row in zip(schedules, confirmed_rows) if row.get("类型") == "追加"]
    pure_cutting = sum(s["整批设备时间(h)"] for s in schedules if not any(x in s["工序"] for x in ["装夹", "上下料", "编程", "试切"])) / quantity
    loading = sum(s["整批设备时间(h)"] for s in schedules if "上下料" in s["工序"]) / quantity
    batch_prepare = sum(s["整批设备时间(h)"] for s in schedules if s["计算类型"] == "每批一次")
    one_time_operation_cost = sum(s["整批金额(元)"] for s in schedules if s["计算类型"] in {"每批一次", "手动总价"})
    pair_warning = any(s["计算类型"] == "每对产品" for s in schedules) and quantity % 2 == 1
    return {"unit_cost": unit_cost, "unit_price": unit_price, "batch_cost": batch_cost, "batch_price": batch_price, "one_time_cost": batch_extra + one_time_operation_cost,
            "additional_one_time_cost": batch_extra, "operation_one_time_cost": one_time_operation_cost,
            "casting_per_unit": casting_per_unit, "material_rate": material_rate, "equipment_per_unit": equipment_batch / quantity,
            "labor_per_unit": labor_batch / quantity, "surface_per_unit": surface_per_unit, "packaging_per_unit": packaging_per_unit,
            "operation_schedules": schedules, "base_time": sum(s["整批设备时间(h)"] + s["整批人工时间(h)"] for s in base_schedules) / quantity,
            "extra_time": sum(s["整批设备时间(h)"] + s["整批人工时间(h)"] for s in extra_schedules) / quantity,
            "pure_cutting_time": pure_cutting, "loading_time": loading, "batch_preparation_time": batch_prepare,
            "batch_equipment_time": sum(s["整批设备时间(h)"] for s in schedules), "pair_warning": pair_warning,
            "surface_details": surface_details, "additional_details": additional_details, "confirmed_rows": confirmed_rows, "quote_mode": mode}
