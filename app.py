"""机械加工厂内部自动报价系统（Streamlit）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "pricing_config.json"
DB_FILE = BASE_DIR / "quotes.db"

DEFAULT_CONFIG = {
    "company_name": "机械加工厂",
    "profit_multiplier": 1.2,
    "materials": {"灰铁": 6.0, "球铁": 7.0, "铸铝": 36.0},
    "surface_treatments": {
        "无处理": 0.0, "喷砂": 2.0, "氧化": 7.0, "电泳": 10.0,
        "黑漆": 0.5, "磷化": 4.0, "喷粉": 3.0, "喷漆": 2.0,
    },
    "cnc_rate": 60.0,
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_config(config: dict) -> None:
    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


def init_database() -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_date TEXT NOT NULL, customer TEXT, product_name TEXT,
                product_number TEXT, quantity INTEGER, material TEXT,
                weight REAL, cnc_hours REAL, surface_treatment TEXT,
                packaging_cost REAL, material_cost REAL, cnc_cost REAL,
                surface_cost REAL, total_cost REAL, profit_multiplier REAL,
                final_price REAL
            )
        """)


def calculate(config: dict, material: str, weight: float, cnc_hours: float,
              treatment: str, packaging_cost: float) -> dict:
    material_cost = weight * config["materials"][material]
    cnc_cost = cnc_hours * config["cnc_rate"]
    surface_cost = weight * config["surface_treatments"][treatment]
    total_cost = material_cost + cnc_cost + surface_cost + packaging_cost
    final_price = total_cost * config["profit_multiplier"]
    return {
        "material_cost": material_cost, "cnc_cost": cnc_cost,
        "surface_cost": surface_cost, "total_cost": total_cost,
        "final_price": final_price,
    }


def save_quote(data: dict, costs: dict, config: dict) -> None:
    fields = (
        datetime.now().strftime("%Y-%m-%d %H:%M"), data["customer"], data["product_name"],
        data["product_number"], data["quantity"], data["material"], data["weight"],
        data["cnc_hours"], data["treatment"], data["packaging_cost"],
        costs["material_cost"], costs["cnc_cost"], costs["surface_cost"], costs["total_cost"],
        config["profit_multiplier"], costs["final_price"],
    )
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""INSERT INTO quotes
            (quote_date, customer, product_name, product_number, quantity, material,
             weight, cnc_hours, surface_treatment, packaging_cost, material_cost,
             cnc_cost, surface_cost, total_cost, profit_multiplier, final_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", fields)


def quote_excel(data: dict, costs: dict, config: dict) -> bytes:
    info = pd.DataFrame([
        ["公司名称", config["company_name"]], ["报价日期", datetime.now().strftime("%Y-%m-%d")],
        ["客户名称", data["customer"]], ["产品名称", data["product_name"]],
        ["产品编号", data["product_number"]], ["数量", data["quantity"]],
        ["材料", data["material"]], ["产品重量 (kg)", data["weight"]],
        ["CNC加工时间 (小时)", data["cnc_hours"]], ["表面处理", data["treatment"]],
    ], columns=["项目", "内容"])
    detail = pd.DataFrame([
        ["材料成本", costs["material_cost"]], ["CNC加工成本", costs["cnc_cost"]],
        ["表面处理成本", costs["surface_cost"]], ["包装成本", data["packaging_cost"]],
        ["总成本", costs["total_cost"]], ["利润系数", config["profit_multiplier"]],
        ["最终报价", costs["final_price"]],
    ], columns=["成本项目", "金额（元）"])
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        info.to_excel(writer, sheet_name="报价单", index=False, startrow=1)
        detail.to_excel(writer, sheet_name="报价单", index=False, startrow=len(info) + 4)
        sheet = writer.sheets["报价单"]
        sheet["A1"] = f"{config['company_name']} - 报价单"
        sheet.column_dimensions["A"].width = 24
        sheet.column_dimensions["B"].width = 28
    return output.getvalue()


def pricing_page(config: dict) -> None:
    st.header("新建报价")
    with st.form("quote_form"):
        left, right = st.columns(2)
        with left:
            customer = st.text_input("客户名称")
            product_name = st.text_input("产品名称")
            product_number = st.text_input("产品编号")
            quantity = st.number_input("数量", min_value=1, value=1, step=1)
        with right:
            material = st.selectbox("材料", list(config["materials"]))
            weight = st.number_input("产品重量（kg）", min_value=0.0, value=0.0, step=0.1)
            cnc_hours = st.number_input("CNC加工时间（小时）", min_value=0.0, value=0.0, step=0.1)
            treatment = st.selectbox("表面处理", list(config["surface_treatments"]))
            packaging_cost = st.number_input("包装费用（元）", min_value=0.0, value=0.0, step=1.0)
        submitted = st.form_submit_button("计算报价", type="primary")

    if submitted:
        if not customer.strip() or not product_name.strip():
            st.error("请至少填写客户名称和产品名称。")
            return
        data = {"customer": customer.strip(), "product_name": product_name.strip(),
                "product_number": product_number.strip(), "quantity": int(quantity),
                "material": material, "weight": weight, "cnc_hours": cnc_hours,
                "treatment": treatment, "packaging_cost": packaging_cost}
        st.session_state["quote"] = (data, calculate(config, **{k: data[k] for k in ["material", "weight", "cnc_hours"]}, treatment=treatment, packaging_cost=packaging_cost))

    if "quote" in st.session_state:
        data, costs = st.session_state["quote"]
        st.subheader("报价结果")
        cols = st.columns(3)
        labels = [("材料成本", costs["material_cost"]), ("CNC加工成本", costs["cnc_cost"]),
                  ("表面处理成本", costs["surface_cost"]), ("包装成本", data["packaging_cost"]),
                  ("总成本", costs["total_cost"]), ("最终报价", costs["final_price"])]
        for i, (label, value) in enumerate(labels):
            cols[i % 3].metric(label, f"¥ {value:,.2f}")
        st.info(f"利润系数：{config['profit_multiplier']:.2f}")
        a, b = st.columns(2)
        with a:
            if st.button("保存本次报价", type="primary"):
                save_quote(data, costs, config)
                st.success("报价记录已保存。")
        with b:
            st.download_button("导出 Excel 报价单", quote_excel(data, costs, config),
                               file_name=f"报价单_{data['product_name']}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def history_page() -> None:
    st.header("历史报价")
    with sqlite3.connect(DB_FILE) as conn:
        df = pd.read_sql_query("SELECT * FROM quotes ORDER BY id DESC", conn)
    if df.empty:
        st.info("暂未保存报价记录。")
        return
    keyword = st.text_input("按客户、产品名称或产品编号查询")
    if keyword:
        mask = df[["customer", "product_name", "product_number"]].fillna("").apply(
            lambda col: col.str.contains(keyword, case=False, na=False)).any(axis=1)
        df = df[mask]
    display = df.rename(columns={"quote_date": "日期", "customer": "客户名称", "product_name": "产品名称",
        "product_number": "产品编号", "quantity": "数量", "material": "材料", "weight": "重量(kg)",
        "cnc_hours": "加工时间(h)", "surface_treatment": "表面处理", "total_cost": "总成本", "final_price": "最终报价"})
    st.dataframe(display[["日期", "客户名称", "产品名称", "产品编号", "数量", "材料", "重量(kg)", "加工时间(h)", "表面处理", "总成本", "最终报价"]], use_container_width=True, hide_index=True)


def settings_page(config: dict) -> None:
    st.header("成本参数设置")
    st.caption("修改后点击保存，新建报价会立即使用新参数。")
    with st.form("settings_form"):
        company_name = st.text_input("公司名称", config["company_name"])
        profit = st.number_input("利润系数", min_value=0.01, value=float(config["profit_multiplier"]), step=0.05)
        cnc_rate = st.number_input("CNC加工单价（元/小时）", min_value=0.0, value=float(config["cnc_rate"]), step=1.0)
        st.subheader("材料单价（元/kg）")
        materials = {name: st.number_input(name, min_value=0.0, value=float(rate), step=0.5, key=f"m_{name}") for name, rate in config["materials"].items()}
        st.subheader("表面处理单价（元/kg）")
        treatments = {name: st.number_input(name, min_value=0.0, value=float(rate), step=0.5, key=f"s_{name}") for name, rate in config["surface_treatments"].items()}
        if st.form_submit_button("保存参数", type="primary"):
            save_config({"company_name": company_name, "profit_multiplier": profit, "cnc_rate": cnc_rate,
                         "materials": materials, "surface_treatments": treatments})
            st.success("参数已保存，请在侧边栏重新打开“新建报价”使用最新参数。")


def main() -> None:
    st.set_page_config(page_title="机械加工自动报价系统", page_icon="⚙️", layout="wide")
    init_database()
    config = load_config()
    st.sidebar.title("⚙️ 自动报价系统")
    page = st.sidebar.radio("功能", ["新建报价", "历史报价", "成本参数设置"])
    if page == "新建报价":
        pricing_page(config)
    elif page == "历史报价":
        history_page()
    else:
        settings_page(config)


if __name__ == "__main__":
    main()
