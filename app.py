"""机械加工厂内部自动报价系统（第二版）。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from pypdf import PdfReader

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "pricing_config.json"
DB_FILE = BASE_DIR / "quotes.db"

DEFAULT_CONFIG = {
    "company_name": "机械加工厂",
    "profit_multiplier": 1.2,
    "materials": {"灰铁": 6.0, "球铁": 7.0, "铸铝": 36.0},
    "densities": {"灰铁": 7.2, "球铁": 7.1, "铸铝": 2.7},  # g/cm³
    "surface_treatments": {"无处理": 0.0, "喷砂": 2.0, "氧化": 7.0, "电泳": 10.0,
                           "黑漆": 0.5, "磷化": 4.0, "喷粉": 3.0, "喷漆": 2.0},
    "machine_rates": {"CNC加工中心": 90.0, "车床": 65.0, "龙门铣": 200.0, "卧式加工中心": 175.0},
}


def save_config(config: dict) -> None:
    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = json.load(file)
    # 兼容第一版参数文件，保留用户已维护的所有价格。
    config.setdefault("densities", DEFAULT_CONFIG["densities"])
    if "machine_rates" not in config:
        old_rate = config.pop("cnc_rate", 60.0)
        config["machine_rates"] = {"CNC加工中心": 90.0, "车床": 65.0,
                                   "龙门铣": 200.0, "卧式加工中心": 175.0}
        save_config(config)
    return config


def init_database() -> None:
    columns = {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT", "quote_date": "TEXT", "customer": "TEXT",
        "product_name": "TEXT", "product_number": "TEXT", "quantity": "INTEGER", "material": "TEXT",
        "weight": "REAL", "cnc_hours": "REAL", "surface_treatment": "TEXT", "packaging_cost": "REAL",
        "material_cost": "REAL", "cnc_cost": "REAL", "surface_cost": "REAL", "total_cost": "REAL",
        "profit_multiplier": "REAL", "final_price": "REAL", "cnc_details": "TEXT",
    }
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS quotes (" + ", ".join(f"{k} {v}" for k, v in columns.items()) + ")")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
        for name, kind in columns.items():
            if name not in existing and name != "id":
                conn.execute(f"ALTER TABLE quotes ADD COLUMN {name} {kind}")


def extract_pdf_info(file_bytes: bytes) -> tuple[dict, str]:
    """从可复制文字的 PDF 图纸提取常见字段；扫描件需后续 OCR 支持。"""
    reader = PdfReader(BytesIO(file_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    result: dict[str, object] = {}
    patterns = {
        "product_number": r"(?:图号|零件号|产品编号|part\s*(?:no\.?|number)?)[：:\s#-]*([A-Za-z0-9_.-]{3,})",
        "weight": r"(?:重量|weight)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:kg|公斤)?",
        "quantity": r"(?:数量|quantity|qty)[：:\s]*([0-9]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            result[key] = float(match.group(1)) if key == "weight" else (int(match.group(1)) if key == "quantity" else match.group(1))
    for material in ["灰铁", "球铁", "铸铝"]:
        if material in text:
            result["material"] = material
            break
    return result, text


def analyze_step(file_bytes: bytes, material: str, config: dict) -> dict:
    """轻量级 STEP 估算：不替代 CAD 内核，结果应由工程师复核。"""
    text = file_bytes.decode("utf-8", errors="ignore")
    point_matches = re.findall(r"CARTESIAN_POINT\s*\([^,]*,\s*\(([-+0-9.Ee]+),\s*([-+0-9.Ee]+),\s*([-+0-9.Ee]+)\)", text)
    points = []
    for x, y, z in point_matches:
        try:
            points.append((float(x), float(y), float(z)))
        except ValueError:
            continue
    entities = len(re.findall(r"^\s*#[0-9]+\s*=", text, flags=re.MULTILINE))
    feature_score = sum(text.upper().count(item) for item in ["CYLINDRICAL_SURFACE", "CONICAL_SURFACE", "TOROIDAL_SURFACE", "B_SPLINE"])
    if len(points) < 2:
        return {"available": False, "message": "未能从该 STEP 文件读取足够的几何点，请手动填写重量和工时。"}
    spans = [max(axis) - min(axis) for axis in zip(*points)]
    volume_cm3 = (spans[0] * spans[1] * spans[2]) / 1000  # STEP 常用毫米单位
    fill_factor = 0.35 if feature_score > 40 else (0.48 if feature_score > 12 else 0.62)
    estimate_weight = volume_cm3 * fill_factor * float(config["densities"].get(material, 7.0)) / 1000
    difficulty = "高" if entities > 3000 or feature_score > 40 else ("中" if entities > 800 or feature_score > 12 else "低")
    return {"available": True, "dimensions": spans, "entities": entities, "difficulty": difficulty,
            "estimated_weight": max(0.0, estimate_weight), "fill_factor": fill_factor}


def calculate(config: dict, material: str, weight: float, machine_hours: dict[str, float],
              treatment: str, packaging_cost: float) -> dict:
    material_cost = weight * config["materials"][material]
    machine_costs = {name: hours * config["machine_rates"][name] for name, hours in machine_hours.items()}
    cnc_cost = sum(machine_costs.values())
    surface_cost = weight * config["surface_treatments"][treatment]
    total_cost = material_cost + cnc_cost + surface_cost + packaging_cost
    return {"material_cost": material_cost, "machine_costs": machine_costs, "cnc_cost": cnc_cost,
            "surface_cost": surface_cost, "total_cost": total_cost,
            "final_price": total_cost * config["profit_multiplier"]}


def pricing_advice(data: dict, costs: dict, config: dict, step_result: dict | None) -> list[str]:
    messages = []
    total_hours = sum(data["machine_hours"].values())
    if total_hours == 0:
        messages.append("未录入加工工时：请工程人员确认是否存在钻孔、攻牙、精加工等机加工工序。")
    if data["quantity"] >= 50 and total_hours > 0:
        messages.append("批量较大：可评估夹具、专用刀具和工序合并，争取降低单件加工时间。")
    if data["treatment"] != "无处理":
        messages.append("表面处理价格按重量估算；报价前请与供应商确认最小起订量、挂具费或遮蔽要求。")
    if step_result and step_result.get("available"):
        messages.append(f"STEP 估算加工难度为“{step_result['difficulty']}”，估算重量仅供前期报价，请以 CAD 体积/实物称重复核。")
    if costs["final_price"] / max(data["quantity"], 1) < 10:
        messages.append("单件报价较低：建议另行确认起订量、管理费和最小订单金额。")
    return messages or ["基础参数完整，可保存报价；建议由工艺工程师复核关键尺寸、公差与加工工时。"]


def save_quote(data: dict, costs: dict, config: dict) -> None:
    total_hours = sum(data["machine_hours"].values())
    values = (datetime.now().strftime("%Y-%m-%d %H:%M"), data["customer"], data["product_name"], data["product_number"],
              data["quantity"], data["material"], data["weight"], total_hours, data["treatment"], data["packaging_cost"],
              costs["material_cost"], costs["cnc_cost"], costs["surface_cost"], costs["total_cost"], config["profit_multiplier"],
              costs["final_price"], json.dumps(data["machine_hours"], ensure_ascii=False))
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""INSERT INTO quotes (quote_date, customer, product_name, product_number, quantity, material, weight,
          cnc_hours, surface_treatment, packaging_cost, material_cost, cnc_cost, surface_cost, total_cost, profit_multiplier,
          final_price, cnc_details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)


def quote_excel(data: dict, costs: dict, config: dict) -> bytes:
    info_rows = [["公司名称", config["company_name"]], ["报价日期", datetime.now().strftime("%Y-%m-%d")],
                 ["客户名称", data["customer"]], ["产品名称", data["product_name"]], ["产品编号", data["product_number"]],
                 ["数量", data["quantity"]], ["材料", data["material"]], ["产品重量 (kg)", data["weight"]],
                 ["表面处理", data["treatment"]]]
    cnc_rows = [[f"{name}（{data['machine_hours'][name]:.2f} 小时）", costs["machine_costs"][name]]
                for name in data["machine_hours"] if data["machine_hours"][name] > 0]
    detail_rows = [["材料成本", costs["material_cost"]], *cnc_rows, ["CNC加工成本合计", costs["cnc_cost"]],
                   ["表面处理成本", costs["surface_cost"]], ["包装成本", data["packaging_cost"]], ["总成本", costs["total_cost"]],
                   ["利润系数", config["profit_multiplier"]], ["最终报价", costs["final_price"]]]
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(info_rows, columns=["项目", "内容"]).to_excel(writer, sheet_name="报价单", index=False, startrow=1)
        pd.DataFrame(detail_rows, columns=["成本项目", "金额（元）"]).to_excel(writer, sheet_name="报价单", index=False, startrow=len(info_rows) + 4)
        sheet = writer.sheets["报价单"]
        sheet["A1"] = f"{config['company_name']} - 报价单"
        sheet.column_dimensions["A"].width, sheet.column_dimensions["B"].width = 26, 28
    return output.getvalue()


def pricing_page(config: dict) -> None:
    st.header("新建报价")
    pdf_result, step_result = None, None
    with st.expander("图纸与模型辅助（可选）"):
        pdf_file = st.file_uploader("上传 PDF 图纸（提取可复制文字中的图号、材料、重量、数量）", type=["pdf"])
        step_file = st.file_uploader("上传 STEP/3D 模型（.step / .stp，估算重量与加工难度）", type=["step", "stp"])
        selected_material = st.session_state.get("material_input", list(config["materials"])[0])
        if pdf_file and st.button("读取 PDF 图纸"):
            try:
                pdf_result, raw_text = extract_pdf_info(pdf_file.getvalue())
                for key, value in pdf_result.items():
                    st.session_state[{"product_number": "product_number_input", "weight": "weight_input", "quantity": "quantity_input", "material": "material_input"}[key]] = value
                st.success("图纸读取完成，已识别的信息会带入下方表单。")
                if not raw_text.strip(): st.warning("该 PDF 未包含可复制文字，扫描图纸需要后续接入 OCR。")
            except Exception as error:
                st.error(f"PDF 读取失败：{error}")
        if step_file and st.button("分析 STEP 模型"):
            step_result = analyze_step(step_file.getvalue(), selected_material, config)
            st.session_state["step_result"] = step_result
            if step_result.get("available"):
                st.session_state["weight_input"] = round(step_result["estimated_weight"], 3)
                st.success("模型分析完成，估算重量已带入下方表单。")
            else: st.warning(step_result["message"])
    step_result = st.session_state.get("step_result", step_result)
    if step_result and step_result.get("available"):
        dims = step_result["dimensions"]
        st.info(f"STEP 初步估算：外接尺寸 {dims[0]:.1f} × {dims[1]:.1f} × {dims[2]:.1f} mm；"
                f"加工难度 {step_result['difficulty']}；估算重量 {step_result['estimated_weight']:.3f} kg。")

    with st.form("quote_form"):
        left, right = st.columns(2)
        with left:
            customer = st.text_input("客户名称", key="customer_input")
            product_name = st.text_input("产品名称", key="product_name_input")
            product_number = st.text_input("产品编号", key="product_number_input")
            quantity = st.number_input("数量", min_value=1, value=st.session_state.get("quantity_input", 1), step=1, key="quantity_input")
        with right:
            material = st.selectbox("材料", list(config["materials"]), key="material_input")
            weight = st.number_input("产品重量（kg）", min_value=0.0, value=float(st.session_state.get("weight_input", 0.0)), step=0.1, key="weight_input")
            treatment = st.selectbox("表面处理", list(config["surface_treatments"]), key="treatment_input")
            packaging_cost = st.number_input("包装费用（元）", min_value=0.0, value=0.0, step=1.0, key="packaging_input")
        st.subheader("CNC 加工工时（小时）")
        machine_cols = st.columns(len(config["machine_rates"]))
        machine_hours = {}
        for col, name in zip(machine_cols, config["machine_rates"]):
            machine_hours[name] = col.number_input(f"{name}\n¥{config['machine_rates'][name]:.0f}/小时", min_value=0.0, value=0.0, step=0.1, key=f"hours_{name}")
        submitted = st.form_submit_button("计算报价", type="primary")
    if submitted:
        if not customer.strip() or not product_name.strip():
            st.error("请至少填写客户名称和产品名称。")
            return
        data = {"customer": customer.strip(), "product_name": product_name.strip(), "product_number": product_number.strip(),
                "quantity": int(quantity), "material": material, "weight": weight, "machine_hours": machine_hours,
                "treatment": treatment, "packaging_cost": packaging_cost}
        st.session_state["quote"] = (data, calculate(config, material, weight, machine_hours, treatment, packaging_cost))
    if "quote" in st.session_state:
        data, costs = st.session_state["quote"]
        st.subheader("报价结果")
        metrics = [("材料成本", costs["material_cost"]), *[(f"{n}成本", v) for n, v in costs["machine_costs"].items() if v],
                   ("CNC加工合计", costs["cnc_cost"]), ("表面处理成本", costs["surface_cost"]),
                   ("包装成本", data["packaging_cost"]), ("总成本", costs["total_cost"]), ("最终报价", costs["final_price"])]
        for start in range(0, len(metrics), 3):
            for col, (label, value) in zip(st.columns(3), metrics[start:start + 3]): col.metric(label, f"¥ {value:,.2f}")
        st.info(f"利润系数：{config['profit_multiplier']:.2f}；整批报价：¥ {costs['final_price']:,.2f}；单件参考：¥ {costs['final_price'] / data['quantity']:,.2f}")
        with st.expander("AI 辅助报价建议", expanded=True):
            for advice in pricing_advice(data, costs, config, step_result): st.write(f"- {advice}")
        a, b = st.columns(2)
        if a.button("保存本次报价", type="primary"):
            save_quote(data, costs, config); st.success("报价记录已保存。")
        b.download_button("导出 Excel 报价单", quote_excel(data, costs, config), file_name=f"报价单_{data['product_name']}.xlsx",
                          mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def history_page() -> None:
    st.header("历史报价")
    with sqlite3.connect(DB_FILE) as conn: df = pd.read_sql_query("SELECT * FROM quotes ORDER BY id DESC", conn)
    if df.empty: st.info("暂未保存报价记录。"); return
    keyword = st.text_input("按客户、产品名称或产品编号查询")
    if keyword: df = df[df[["customer", "product_name", "product_number"]].fillna("").apply(lambda c: c.str.contains(keyword, case=False)).any(axis=1)]
    show = df.rename(columns={"quote_date": "日期", "customer": "客户名称", "product_name": "产品名称", "product_number": "产品编号", "quantity": "数量", "material": "材料", "weight": "重量(kg)", "cnc_hours": "加工时间(h)", "surface_treatment": "表面处理", "total_cost": "总成本", "final_price": "最终报价"})
    st.dataframe(show[["日期", "客户名称", "产品名称", "产品编号", "数量", "材料", "重量(kg)", "加工时间(h)", "表面处理", "总成本", "最终报价"]], use_container_width=True, hide_index=True)


def settings_page(config: dict) -> None:
    st.header("成本参数设置")
    st.caption("此处的参数全部可维护，保存后新报价立即使用。")
    with st.form("settings_form"):
        company_name = st.text_input("公司名称", config["company_name"])
        profit = st.number_input("利润系数", min_value=0.01, value=float(config["profit_multiplier"]), step=0.05)
        st.subheader("材料单价（元/kg）")
        materials = {n: st.number_input(n, min_value=0.0, value=float(v), step=0.5, key=f"m_{n}") for n, v in config["materials"].items()}
        st.subheader("设备工时单价（元/小时）")
        rates = {n: st.number_input(n, min_value=0.0, value=float(v), step=5.0, key=f"r_{n}") for n, v in config["machine_rates"].items()}
        st.subheader("表面处理单价（元/kg）")
        treatments = {n: st.number_input(n, min_value=0.0, value=float(v), step=0.5, key=f"s_{n}") for n, v in config["surface_treatments"].items()}
        if st.form_submit_button("保存参数", type="primary"):
            save_config({"company_name": company_name, "profit_multiplier": profit, "materials": materials,
                         "densities": config["densities"], "machine_rates": rates, "surface_treatments": treatments})
            st.success("参数已保存。")


def main() -> None:
    st.set_page_config(page_title="机械加工自动报价系统", page_icon="⚙️", layout="wide")
    init_database(); config = load_config()
    st.sidebar.title("⚙️ 自动报价系统")
    page = st.sidebar.radio("功能", ["新建报价", "历史报价", "成本参数设置"])
    {"新建报价": pricing_page, "历史报价": lambda _: history_page(), "成本参数设置": settings_page}[page](config)


if __name__ == "__main__": main()
