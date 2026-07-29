"""单件、整批、成本加利润与直接销售单价报价计算。"""
from __future__ import annotations


def surface_cost(selected: list[dict], net_weight: float, area_m2: float, quantity: int) -> tuple[float, list[dict]]:
    total, details = 0.0, []
    for item in selected:
        if not item.get("启用", False):
            continue
        basis, rate = item.get("计价方式", "按kg"), float(item.get("单价", 0.0))
        if basis == "按kg": amount = net_weight * rate
        elif basis == "按平方米": amount = area_m2 * rate
        elif basis == "按件": amount = rate
        else: amount = float(item.get("手动报价", 0.0))
        amount = max(amount, float(item.get("最低收费", 0.0))) + float(item.get("遮蔽加工面费用", 0.0)) + float(item.get("挂具费用", 0.0))
        if quantity < int(item.get("小批量数量", 0) or 0): amount += float(item.get("小批量附加费", 0.0))
        total += amount; details.append({"名称": item.get("名称"), "单件金额": amount, "计价方式": basis})
    return total, details


def calculate_quote(data: dict, rows: list[dict], config: dict, additional: list[dict], surfaces: list[dict]) -> dict:
    quantity = max(1, int(data.get("quantity", 1)))
    mode = data.get("quote_mode", "成本加利润")
    material = data["material"]; blank_weight = float(data.get("casting_weight", data.get("net_weight", 0)))
    net_weight = float(data.get("net_weight", 0)); area = float(data.get("surface_area_m2", 0))
    rate_map = config["machine_rates"] if mode == "成本加利润" else config.get("direct_machine_rates", config["machine_rates"])
    confirmed_rows = [row for row in rows if bool(row.get("用户确认", False))]
    base_rows = [row for row in confirmed_rows if row.get("类型", "基础") == "基础"]
    extra_rows = [row for row in confirmed_rows if row.get("类型") == "追加"]
    machine_costs = {}
    for row in confirmed_rows:
        machine = row.get("推荐设备", "CNC加工中心")
        rate = float(rate_map.get(machine, config.get("manual_labor_rate", 35.0)))
        machine_costs[machine] = machine_costs.get(machine, 0.0) + float(row.get("推荐时间(h)", 0))*rate
    equipment_per_unit = sum(machine_costs.values())
    raw_material_rate = float(config["materials"][material])
    sale_rate = float(data.get("casting_sales_rate", config.get("casting_sales_prices", {}).get(material, 0.0)))
    casting_per_unit = blank_weight * (raw_material_rate if mode == "成本加利润" else sale_rate)
    per_unit_extra, batch_extra = 0.0, 0.0
    additional_details = []
    for item in additional:
        if not item.get("启用", False): continue
        method, value = item.get("计价方式", "单件费用"), float(item.get("金额", 0.0))
        if method == "按重量": amount = value * blank_weight; per_unit_extra += amount
        elif method == "整批一次性费用": amount = value; batch_extra += amount
        else: amount = value; per_unit_extra += amount
        additional_details.append({"名称": item.get("项目"), "方式": method, "金额": amount})
    surface_per_unit, surface_details = surface_cost(surfaces, net_weight, area, quantity)
    packaging = float(data.get("packaging_cost", 0.0))
    if data.get("packaging_mode") == "整批费用": batch_extra += packaging
        # else intentionally handled per unit below
    packaging_per_unit = packaging if data.get("packaging_mode") != "整批费用" else 0.0
    unit_cost = casting_per_unit + equipment_per_unit + per_unit_extra + surface_per_unit + packaging_per_unit
    multiplier = float(config.get("profit_multiplier", 1.2))
    unit_price = unit_cost * multiplier if mode == "成本加利润" else unit_cost
    batch_cost = unit_cost * quantity + batch_extra
    batch_price = unit_price * quantity + batch_extra
    base_time = sum(float(row.get("推荐时间(h)", 0)) for row in base_rows)
    extra_time = sum(float(row.get("推荐时间(h)", 0)) for row in extra_rows)
    return {"unit_cost": unit_cost, "unit_price": unit_price, "batch_cost": batch_cost, "batch_price": batch_price,
            "one_time_cost": batch_extra, "casting_per_unit": casting_per_unit, "equipment_per_unit": equipment_per_unit,
            "material_rate": raw_material_rate if mode == "成本加利润" else sale_rate,
            "surface_per_unit": surface_per_unit, "packaging_per_unit": packaging_per_unit, "machine_costs": machine_costs,
            "base_time": base_time, "extra_time": extra_time, "surface_details": surface_details,
            "additional_details": additional_details, "confirmed_rows": confirmed_rows, "quote_mode": mode}
