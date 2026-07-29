from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pandas as pd


def quote_excel(data: dict, result: dict, config: dict) -> bytes:
    info = [["公司名称", config["company_name"]], ["报价日期", datetime.now().strftime("%Y-%m-%d")], ["报价模式", result["quote_mode"]],
            ["客户", data.get("customer", "")], ["产品名称", data.get("product_name", "")], ["产品编号", data.get("product_number", "")],
            ["数量", data.get("quantity", 1)], ["材料", data.get("material", "")], ["成品净重(kg)", data.get("net_weight", 0)], ["铸件计价重量(kg)", data.get("casting_weight", 0)]]
    processes = [[row["工序"], row["推荐设备"], row["推荐时间(h)"], row["判断依据"], row["置信度"], row["类型"]] for row in result["confirmed_rows"]]
    costs = [["单件铸件成本/售价", result["casting_per_unit"]], ["单件设备加工", result["equipment_per_unit"]], ["单件表面处理", result["surface_per_unit"]],
             ["单件包装", result["packaging_per_unit"]], ["一次性费用", result["one_time_cost"]], ["单件成本", result["unit_cost"]], ["单件报价", result["unit_price"]], ["整批成本", result["batch_cost"]], ["整批报价", result["batch_price"]]]
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        pd.DataFrame(info, columns=["项目", "内容"]).to_excel(writer, sheet_name="报价单", index=False, startrow=1)
        start = len(info) + 4
        pd.DataFrame(processes, columns=["工序", "设备", "时间(h)", "判断依据", "置信度", "类型"]).to_excel(writer, sheet_name="报价单", index=False, startrow=start)
        pd.DataFrame(costs, columns=["成本/报价项目", "金额(元)"]).to_excel(writer, sheet_name="报价单", index=False, startrow=start+len(processes)+3)
        ws = writer.sheets["报价单"]; ws["A1"] = f"{config['company_name']} - 报价单"; ws.column_dimensions["A"].width = 30; ws.column_dimensions["B"].width = 28; ws.column_dimensions["D"].width = 42
    return out.getvalue()
