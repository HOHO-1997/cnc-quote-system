"""机械加工自动报价系统：自动识别只作为工程师复核起点。"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from config import load_config, save_config
from database import calibration, history, init_database, save_quote, update_actual
from drawing_analyzer import analyze_drawing, extract_dxf, extract_pdf
from export import quote_excel
from machining_estimator import estimate_operations
from pricing import calculate_quote
from step_analyzer import analyze_step

ADDITIONAL_ITEMS = ["退火", "人工时效", "去应力处理", "水压测试", "密封测试", "材质报告", "第三方检测", "三坐标检测", "专用检具", "防锈油", "清洗", "装配", "模具费", "夹具费", "编程费", "首件检测费", "运输费"]


def init_state() -> None:
    st.session_state.setdefault("analysis_rows", [])
    st.session_state.setdefault("drawing", {})
    st.session_state.setdefault("step", {})


def _surface_rows(config: dict) -> list[dict]:
    return [{"启用": False, "名称": name, "计价方式": item.get("basis", "按kg"), "单价": item.get("rate", 0.0), "最低收费": item.get("minimum", 0.0),
             "遮蔽加工面费用": 0.0, "挂具费用": 0.0, "小批量数量": 0, "小批量附加费": 0.0, "手动报价": 0.0} for name, item in config["surface_treatments"].items()]


def _additional_rows(drawing: dict) -> list[dict]:
    detected = set(drawing.get("heat_treatments", []) + drawing.get("tests", []))
    return [{"启用": item in detected, "项目": item, "计价方式": "按重量" if item in {"退火", "人工时效", "去应力处理"} else "整批一次性费用", "金额": 2.0 if item == "退火" else 0.0} for item in ADDITIONAL_ITEMS]


def drawing_and_step_panel(config: dict) -> None:
    with st.expander("图纸与模型分析（先读取 PDF/DXF，再分析 STEP）", expanded=True):
        col1, col2 = st.columns(2)
        with col1: pdf = st.file_uploader("PDF 图纸", type=["pdf"])
        with col2: dxf = st.file_uploader("DXF 图纸", type=["dxf"])
        step_file = st.file_uploader("STEP 3D 模型（.step/.stp）", type=["step", "stp"])
        if pdf and st.button("读取 PDF 图纸"):
            try:
                fields, text, used_ocr = extract_pdf(pdf.getvalue(), pdf.name)
                st.session_state["drawing"] = analyze_drawing(text)
                st.session_state["fields"] = fields
                st.success("PDF 已解析" + ("（已启用 OCR）" if used_ocr else ""))
            except Exception as e: st.error(f"PDF 读取失败：{e}")
        if dxf and st.button("读取 DXF 图纸"):
            try:
                fields, text = extract_dxf(dxf.getvalue(), dxf.name)
                st.session_state["drawing"] = analyze_drawing(text); st.session_state["fields"] = fields; st.success("DXF 已解析")
            except Exception as e: st.error(str(e))
        fields = st.session_state.get("fields", {})
        material = fields.get("material", st.session_state.get("material", "灰铁"))
        if step_file and st.button("分析 STEP 并生成待确认工序"):
            step = analyze_step(step_file.getvalue(), material, config)
            st.session_state["step"] = step
            if step.get("available"):
                estimate = estimate_operations(step, st.session_state.get("drawing", {}), config)
                st.session_state["estimate"] = estimate; st.session_state["analysis_rows"] = estimate["rows"]
                st.success("已生成自动识别结果，请在下方确认后带入报价。")
            else: st.warning(step.get("message"))
    drawing, step = st.session_state.get("drawing", {}), st.session_state.get("step", {})
    if drawing:
        st.caption(f"图纸识别：螺纹约 {drawing.get('threaded_count', 0)} 个；成组孔约 {drawing.get('drilled_count', 0)} 个；材料/热处理/测试信号均需复核。")
    if step.get("available"):
        dims = step["dimensions"]
        st.info(f"STEP 成品净重 {step['net_weight']:.2f} kg；外形 {dims[0]:.0f} × {dims[1]:.0f} × {dims[2]:.0f} mm；最大平面 {step['largest_planar_area_m2']:.3f} m²。")


def confirmation_panel(config: dict) -> None:
    estimate = st.session_state.get("estimate")
    st.subheader("自动识别结果确认表")
    st.caption("基础工序已勾选，但只有点击“确认带入报价”才会进入成本；精度/检验追加工时默认不勾选。")
    if estimate:
        st.write(f"产品类型：{estimate['classification']}；判断依据：{'；'.join(estimate['evidence']) or '几何信息不足'}")
        st.write(f"磨床评估：{estimate['grinding_assessment']}")
    else:
        st.info("尚未完成可靠自动识别。可在下表人工新增工序，系统不会编造工时。")
        if not st.session_state["analysis_rows"]:
            st.session_state["analysis_rows"] = [{"工序": "", "计算类型": "每件", "推荐设备": "CNC加工中心", "数量": 1,
                                                 "单件时间(h)": 0.0, "每批时间(h)": 0.0, "推荐时间(h)": 0.0,
                                                 "攻牙方式": "无螺纹加工", "设备攻牙数量": 0, "人工攻牙数量": 0,
                                                 "人工单孔时间(h)": 0.03, "手动总价(元)": 0.0,
                                                 "判断依据": "人工录入", "置信度": "人工", "类型": "基础", "用户确认": False}]
    edited = st.data_editor(pd.DataFrame(st.session_state["analysis_rows"]), use_container_width=True, hide_index=True,
                            num_rows="dynamic", column_config={
                                "推荐设备": st.column_config.SelectboxColumn(options=list(config["machine_rates"])),
                                "计算类型": st.column_config.SelectboxColumn(options=["每件", "每批一次", "每对产品", "手动总价"]),
                                "攻牙方式": st.column_config.SelectboxColumn(options=["待确认", "无螺纹加工", "设备刚性攻牙", "人工攻牙", "混合攻牙"]),
                                "用户确认": st.column_config.CheckboxColumn()}, key="operation_editor")
    if st.button("确认带入报价", type="primary"):
        st.session_state["analysis_rows"] = edited.to_dict("records")
        st.session_state["operations_confirmed"] = True
        unresolved = edited[(edited["工序"].astype(str).str.contains("螺纹加工")) & (edited["攻牙方式"] == "待确认")]
        if not unresolved.empty: st.warning("存在待确认的螺纹组，未计入报价；请选择设备刚性攻牙、人工攻牙或混合攻牙后再确认。")
        st.success("确认完成。可在下方计算报价。")


def pricing_page(config: dict) -> None:
    st.header("新建报价")
    drawing_and_step_panel(config); confirmation_panel(config)
    fields = st.session_state.get("fields", {}); step = st.session_state.get("step", {}); drawing = st.session_state.get("drawing", {})
    default_material = fields.get("material", "灰铁")
    net_default = step.get("net_weight", fields.get("weight", 0.0))
    blank_default = step.get("blank_weight", net_default)
    with st.form("quote_form"):
        a, b, c = st.columns(3)
        with a:
            customer = st.text_input("客户名称", value=str(fields.get("customer", "")))
            product_name = st.text_input("产品名称", value=str(fields.get("product_name", "")))
            product_number = st.text_input("产品编号", value=str(fields.get("product_number", "")))
        with b:
            quantity = st.number_input("数量（单件参数按此数量汇总）", min_value=1, value=int(fields.get("quantity", 1)), step=1)
            product_type = st.selectbox("产品类型", ["自动识别", "大型机架/床身", "箱体/多方向孔系", "横梁支架", "小型阀体", "其他"])
            fixture_count = st.number_input("装夹次数（复核用）", min_value=0, value=0, step=1)
        with c:
            material = st.selectbox("材料", list(config["materials"]), index=list(config["materials"]).index(default_material) if default_material in config["materials"] else 0)
            net_weight = st.number_input("成品净重（kg）", min_value=0.0, value=float(net_default), step=0.01)
            casting_weight = st.number_input("毛坯计价重量（kg）", min_value=0.0, value=float(blank_default), step=0.01)
        quote_mode = st.radio("报价模式", ["成本加利润", "直接销售单价"], horizontal=True)
        sale_rate = st.number_input("铸件销售单价（元/kg，仅直接销售模式使用）", min_value=0.0, value=float(config["casting_sales_prices"].get(material, 0.0)), step=0.1)
        packaging_mode = st.radio("包装费", ["单件费用", "整批费用"], horizontal=True)
        packaging_cost = st.number_input("包装金额（元）", min_value=0.0, value=0.0, step=1.0)
        st.subheader("多选表面处理")
        surfaces = st.data_editor(pd.DataFrame(_surface_rows(config)), use_container_width=True, hide_index=True, key="surface_editor")
        st.subheader("其他成本项目")
        additional = st.data_editor(pd.DataFrame(_additional_rows(drawing)), use_container_width=True, hide_index=True, key="additional_editor")
        submitted = st.form_submit_button("计算报价", type="primary")
    if submitted:
        if not customer or not product_name: st.error("请填写客户名称和产品名称。"); return
        rows = st.session_state.get("analysis_rows", [])
        if not rows: st.warning("尚未确认自动工序，请手动新增工序或先分析 STEP/PDF。")
        data = {"customer": customer, "product_name": product_name, "product_number": product_number, "quantity": quantity, "product_type": product_type,
                "fixture_count": fixture_count, "material": material, "net_weight": net_weight, "casting_weight": casting_weight, "quote_mode": quote_mode,
                "casting_sales_rate": sale_rate, "packaging_mode": packaging_mode, "packaging_cost": packaging_cost, "surface_area_m2": step.get("total_planar_area_m2", 0.0), "surfaces": surfaces.to_dict("records")}
        result = calculate_quote(data, rows, config, additional.to_dict("records"), surfaces.to_dict("records"))
        st.session_state["quote"] = (data, result, additional.to_dict("records"))
    if "quote" not in st.session_state: return
    data, result, _ = st.session_state["quote"]
    st.subheader("报价结果")
    st.caption(f"基础工时 {result['base_time']:.2f} h；精度/检验追加工时 {result['extra_time']:.2f} h；未确认追加项不会计入报价。")
    cal = calibration(data["product_type"])
    formula_time = result["base_time"] + result["extra_time"]
    st.caption(f"公式估算 {formula_time:.2f} h；历史相似产品修正 {formula_time * cal['factor']:.2f} h（系数 {cal['factor']:.2f}）；最终仍以已确认工序为准。")
    for cols in [[("单件成本", result["unit_cost"]), ("单件报价", result["unit_price"]), ("一次性费用", result["one_time_cost"])], [("整批成本", result["batch_cost"]), ("整批报价", result["batch_price"]), ("单件分摊加工费", result["equipment_per_unit"])]]:
        for col, (label, value) in zip(st.columns(3), cols): col.metric(label, f"¥ {value:,.2f}")
    for col, (label, value) in zip(st.columns(5), [
        ("单件纯切削时间", result["pure_cutting_time"]),
        ("单件上下料装夹", result["loading_time"]),
        ("单件设备占机时间", result["batch_equipment_time"] / max(1, data["quantity"])),
        ("整批一次性准备", result["batch_preparation_time"]),
        ("整批设备总时间", result["batch_equipment_time"]),
    ]): col.metric(label, f"{value:.2f} h")
    if result.get("pair_warning"):
        st.warning("本批数量为奇数，存在按对产品工序；最后一件没有匹配件，不能承诺成对等高交付。")
    st.subheader("单件成本明细")
    material_label = "铸件成本" if result["quote_mode"] == "成本加利润" else "铸件直接销售价"
    material_detail = pd.DataFrame([{
        "项目": material_label, "计算方式": f"{data['casting_weight']:.2f} kg × ¥{result['material_rate']:.2f}/kg",
        "单件金额（元）": round(result["casting_per_unit"], 2), "说明": "毛坯计价重量"}])
    st.dataframe(material_detail, use_container_width=True, hide_index=True)
    process_details = [{
        "工序名称": item["工序"], "计算类型": item["计算类型"], "设备": item["设备"], "数量": item["数量"],
        "单件时间(h)": item["单件时间(h)"], "每批时间(h)": item["每批时间(h)"],
        "整批总时间(h)": round(item["整批设备时间(h)"] + item["整批人工时间(h)"], 3),
        "单价(元/h)": item["单价(元/h)"], "整批金额(元)": round(item["整批金额(元)"], 2),
        "判断依据": item["判断依据"],
    } for item in result.get("operation_schedules", [])]
    st.subheader("加工费明细（按批次计算）")
    st.dataframe(pd.DataFrame(process_details), use_container_width=True, hide_index=True)
    other_details = [{"项目": "表面处理合计", "单件金额（元）": round(result["surface_per_unit"], 2), "说明": "多选处理、最低收费及附加费"},
                     {"项目": "包装", "单件金额（元）": round(result["packaging_per_unit"], 2), "说明": data["packaging_mode"]},
                     {"项目": "整批一次性工序", "单件金额（元）": round(result["operation_one_time_cost"], 2), "说明": "编程、首件准备、首次找正等"},
                     {"项目": "其他整批一次性费用", "单件金额（元）": round(result["additional_one_time_cost"], 2), "说明": "夹具、模具、运输、首件检测等"}]
    for item in result.get("additional_details", []):
        other_details.append({"项目": item["名称"], "单件金额（元）": round(item["金额"], 2), "说明": item["方式"]})
    st.subheader("表面处理、其他费用与一次性费用")
    st.dataframe(pd.DataFrame(other_details), use_container_width=True, hide_index=True)
    st.subheader("已确认工序与识别依据")
    st.dataframe(pd.DataFrame(result["confirmed_rows"]), use_container_width=True, hide_index=True)
    left, right = st.columns(2)
    if left.button("保存报价", type="primary"): save_quote(data, result, config); st.success("已保存历史报价。")
    right.download_button("导出 Excel 报价单", quote_excel(data, result, config), file_name=f"报价单_{data['product_name']}.xlsx")
    st.caption(f"历史相似产品修正系数：{cal['factor']:.2f}（只供工程师复核，不自动改写公式时间）。")
    if cal["samples"]: st.dataframe(pd.DataFrame(cal["samples"]), hide_index=True)


def history_page() -> None:
    st.header("历史报价与实际工时校准")
    df = history()
    if df.empty: st.info("暂无历史报价。"); return
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.subheader("录入实际生产数据")
    record_id = st.number_input("报价记录 ID", min_value=1, value=int(df.iloc[0]["id"]), step=1)
    cols = st.columns(4)
    actual = {"actual_cnc": cols[0].number_input("实际 CNC(h)", min_value=0.0), "actual_lathe": cols[1].number_input("实际车床(h)", min_value=0.0), "actual_gantry": cols[2].number_input("实际龙门(h)", min_value=0.0), "actual_grinding": cols[3].number_input("实际磨床(h)", min_value=0.0)}
    actual["actual_labor"] = st.number_input("实际人工(h)", min_value=0.0)
    actual["actual_total_cost"] = st.number_input("实际总成本（元）", min_value=0.0)
    actual["production_note"] = st.text_input("备注")
    if st.button("保存实际数据"): update_actual(int(record_id), actual); st.success("实际数据已保存，用于同类产品校准。")


def settings_page(config: dict) -> None:
    st.header("成本参数与设备能力设置")
    with st.form("settings"):
        company = st.text_input("公司名称", config["company_name"]); profit = st.number_input("利润系数", min_value=0.01, value=float(config["profit_multiplier"]), step=0.05)
        st.subheader("材料成本、销售单价、密度与毛坯系数")
        material_rows = []
        for name in config["materials"]:
            material_rows.append({"材料": name, "成本价": config["materials"][name], "销售价": config["casting_sales_prices"].get(name, 0), "密度": config["densities"][name], "毛坯系数": config["casting_blank_factors"][name]})
        material_editor = st.data_editor(pd.DataFrame(material_rows), hide_index=True, use_container_width=True)
        st.subheader("设备小时成本/直接报价与能力")
        machine_rows = []
        for name, rate in config["machine_rates"].items():
            cap = config.get("machine_capabilities", {}).get(name, {})
            machine_rows.append({"设备": name, "成本价/h": rate, "直接报价/h": config.get("direct_machine_rates", {}).get(name, rate), "X": cap.get("x", 0), "Y": cap.get("y", 0), "Z": cap.get("z", 0), "承重": cap.get("max_weight", 0), "侧铣头": cap.get("side_head", False), "五面加工": cap.get("five_axis", False)})
        machine_editor = st.data_editor(pd.DataFrame(machine_rows), hide_index=True, use_container_width=True)
        st.subheader("表面处理参数")
        surface_rows = [{"名称": name, "默认单价": item.get("rate", 0.0), "默认计价方式": item.get("basis", "按kg"), "最低收费": item.get("minimum", 0.0)} for name, item in config["surface_treatments"].items()]
        surface_editor = st.data_editor(pd.DataFrame(surface_rows), hide_index=True, use_container_width=True, num_rows="dynamic")
        if st.form_submit_button("保存参数", type="primary"):
            config["company_name"], config["profit_multiplier"] = company, profit
            for row in material_editor.to_dict("records"):
                n = row["材料"]; config["materials"][n] = float(row["成本价"]); config["casting_sales_prices"][n] = float(row["销售价"]); config["densities"][n] = float(row["密度"]); config["casting_blank_factors"][n] = float(row["毛坯系数"])
            for row in machine_editor.to_dict("records"):
                n = row["设备"]; config["machine_rates"][n] = float(row["成本价/h"]); config["direct_machine_rates"][n] = float(row["直接报价/h"]); config.setdefault("machine_capabilities", {})[n] = {"x": float(row["X"]), "y": float(row["Y"]), "z": float(row["Z"]), "max_weight": float(row["承重"]), "side_head": bool(row["侧铣头"]), "five_axis": bool(row["五面加工"]), "table_x": float(row["X"]), "table_y": float(row["Y"])}
            config["surface_treatments"] = {row["名称"]: {"rate": float(row["默认单价"]), "basis": row["默认计价方式"], "minimum": float(row["最低收费"])} for row in surface_editor.to_dict("records") if str(row["名称"]).strip()}
            save_config(config); st.success("参数已保存。")


def main() -> None:
    st.set_page_config(page_title="机械加工自动报价系统", page_icon="⚙️", layout="wide")
    init_database(); config = load_config(); init_state()
    st.sidebar.title("⚙️ 自动报价系统")
    page = st.sidebar.radio("功能", ["新建报价", "历史报价", "成本参数设置"])
    if page == "新建报价": pricing_page(config)
    elif page == "历史报价": history_page()
    else: settings_page(config)


if __name__ == "__main__": main()
