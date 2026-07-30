from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pandas as pd


def quote_excel(data: dict, result: dict, config: dict) -> bytes:
    info = [["公司名称", config["company_name"]], ["报价日期", datetime.now().strftime("%Y-%m-%d")], ["报价模式", result["quote_mode"]],
            ["客户", data.get("customer", "")], ["产品名称", data.get("product_name", "")], ["产品编号", data.get("product_number", "")],
            ["数量", data.get("quantity", 1)], ["材料", data.get("material", "")], ["成品净重(kg)", data.get("net_weight", 0)], ["铸件计价重量(kg)", data.get("casting_weight", 0)]]
    processes = [[item.get("工序"), item.get("计算类型"), item.get("设备"), item.get("数量"),
                  item.get("单件时间(h)"), item.get("每批时间(h)"), item.get("整批设备时间(h)"),
                  item.get("整批人工时间(h)"), item.get("单价(元/h)"), item.get("整批金额(元)"), item.get("判断依据")]
                 for item in result.get("operation_schedules", [])]
    costs = [["单件铸件成本/售价", result["casting_per_unit"]], ["单件设备加工", result["equipment_per_unit"]], ["单件表面处理", result["surface_per_unit"]],
             ["单件包装", result["packaging_per_unit"]], ["一次性费用", result["one_time_cost"]], ["单件成本", result["unit_cost"]], ["单件报价", result["unit_price"]], ["整批成本", result["batch_cost"]], ["整批报价", result["batch_price"]]]
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        pd.DataFrame(info, columns=["项目", "内容"]).to_excel(writer, sheet_name="报价单", index=False, startrow=1)
        start = len(info) + 4
        pd.DataFrame(processes, columns=["工序", "计算类型", "设备", "数量", "单件时间(h)", "每批时间(h)", "整批设备时间(h)", "整批人工时间(h)", "单价(元/h)", "整批金额(元)", "判断依据"]).to_excel(writer, sheet_name="报价单", index=False, startrow=start)
        pd.DataFrame(costs, columns=["成本/报价项目", "金额(元)"]).to_excel(writer, sheet_name="报价单", index=False, startrow=start+len(processes)+3)
        ws = writer.sheets["报价单"]; ws["A1"] = f"{config['company_name']} - 报价单"; ws.column_dimensions["A"].width = 30; ws.column_dimensions["B"].width = 28; ws.column_dimensions["D"].width = 42
    return out.getvalue()


# 覆盖旧版导出布局：结果页现已区分打样、批量和一次性费用。
def quote_excel(data: dict, result: dict, config: dict) -> bytes:
    info = [["公司名称", config["company_name"]], ["报价日期", datetime.now().strftime("%Y-%m-%d")],
            ["客户", data.get("customer", "")], ["产品名称", data.get("product_name", "")],
            ["本次批量数量", data.get("quantity", 1)], ["打样数量", result.get("sample_quantity", 1)]]
    costs = [["打样成本", result.get("sample_cost", 0)], ["打样单价", result.get("sample_unit_price", 0)],
             ["批量平均单件成本", result["unit_cost"]], ["批量单价", result["unit_price"]],
             ["整批成本", result["batch_cost"]], ["整批报价", result["batch_price"]],
             ["单件材料成本", result["casting_per_unit"]], ["单件设备加工费", result["equipment_per_unit"]],
             ["单件人工成本", result.get("labor_per_unit", 0)], ["一次性费用（整批）", result["one_time_cost"]]]
    processes = [{"工序": item.get("工序"), "执行方式": item.get("执行方式"), "计算类型": item.get("计算类型"),
                  "设备/人工": item.get("设备"), "数量": item.get("数量"), "单孔时间(h)": item.get("单孔时间(h)"),
                  "单件时间(h)": item.get("单件时间(h)"), "整批设备时间(h)": item.get("整批设备时间(h)"),
                  "整批人工时间(h)": item.get("整批人工时间(h)"), "整批金额(元)": item.get("整批金额(元)"),
                  "判断依据": item.get("判断依据")} for item in result.get("operation_schedules", [])]
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        sheet = "报价单"
        pd.DataFrame(info, columns=["项目", "内容"]).to_excel(writer, sheet_name=sheet, index=False, startrow=1)
        cost_start = len(info) + 4
        pd.DataFrame(costs, columns=["成本/报价项目", "金额(元)"]).to_excel(writer, sheet_name=sheet, index=False, startrow=cost_start)
        process_start = cost_start + len(costs) + 3
        pd.DataFrame(processes).to_excel(writer, sheet_name=sheet, index=False, startrow=process_start)
        tier_start = process_start + len(processes) + 3
        pd.DataFrame(result.get("tier_results", [])).to_excel(writer, sheet_name=sheet, index=False, startrow=tier_start)
        ws = writer.sheets[sheet]
        ws["A1"] = f"{config['company_name']} - 报价单"
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 28
    return out.getvalue()
