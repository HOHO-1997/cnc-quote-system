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
    st.session_state.setdefault("confirmed_operations", [])
    st.session_state.setdefault("drawing", {})
    st.session_state.setdefault("step", {})
    st.session_state.setdefault("tier_rows", [{"数量": 1}, {"数量": 5}, {"数量": 10}, {"数量": 50}, {"数量": 100}])


def _surface_rows(config: dict) -> list[dict]:
    return [{"启用": False, "名称": name, "计价方式": item.get("basis", "按kg"), "单价": item.get("rate", 0.0), "最低收费": item.get("minimum", 0.0),
             "遮蔽加工面费用": 0.0, "挂具费用": 0.0, "小批量数量": 0, "小批量附加费": 0.0, "手动报价": 0.0} for name, item in config["surface_treatments"].items()]


def _additional_rows(drawing: dict) -> list[dict]:
    detected = set(drawing.get("heat_treatments", []) + drawing.get("tests", []))
    return [{"启用": item in detected, "项目": item, "计价方式": "按重量" if item in {"退火", "人工时效", "去应力处理"} else "整批一次性费用", "金额": 2.0 if item == "退火" else 0.0} for item in ADDITIONAL_ITEMS]


def drawing_and_step_panel(config: dict, product_type: str = "自动识别") -> None:
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
                estimate = estimate_operations(step, st.session_state.get("drawing", {}), config, product_type=product_type)
                st.session_state["estimate"] = estimate; st.session_state["analysis_rows"] = estimate["rows"]; st.session_state["confirmed_operations"] = []
                # 新图纸/模型必须丢弃旧 data_editor 缓存，否则可能把上一张图的局部行带入本次确认。
                st.session_state.pop("operation_editor", None)
                st.session_state.pop("quote", None)
                st.success("已生成自动识别结果，请在下方确认后带入报价。")
            else: st.warning(step.get("message"))
    drawing, step = st.session_state.get("drawing", {}), st.session_state.get("step", {})
    if drawing:
        st.caption(f"图纸识别：螺纹约 {drawing.get('threaded_count', 0)} 个；成组孔约 {drawing.get('drilled_count', 0)} 个；材料/热处理/测试信号均需复核。")
    if step.get("available"):
        dims = step["dimensions"]
        st.info(f"STEP 成品净重 {step['net_weight']:.2f} kg；外形 {dims[0]:.0f} × {dims[1]:.0f} × {dims[2]:.0f} mm；最大平面 {step['largest_planar_area_m2']:.3f} m²。")
        if st.button("按当前产品类型重新生成工序"):
            estimate = estimate_operations(step, drawing, config, product_type=product_type)
            st.session_state["estimate"] = estimate
            st.session_state["analysis_rows"] = estimate["rows"]
            st.session_state["confirmed_operations"] = []
            st.session_state.pop("operation_editor", None)
            st.session_state.pop("quote", None)
            st.success("已按当前产品类型重新生成工序，请在确认表复核后带入报价。")


def _normalize_and_validate_operations(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """确认后的工序唯一写入 confirmed_operations，并把攻牙数量规范化。"""
    normalized, errors = [], []
    for index, raw in enumerate(rows, start=1):
        row = dict(raw)
        process = str(row.get("工序", ""))
        if "螺纹加工" not in process:
            # 只有精度/检验“追加”工序仍允许通过复选框选择是否启用；基础工序随确认按钮带入。
            if row.get("类型") == "追加":
                row["启用"] = bool(row.get("启用", row.get("用户确认", False)))
                row["用户确认"] = row["启用"]
            else:
                row["启用"] = True
                row["用户确认"] = True
            normalized.append(row)
            continue
        total = max(0, int(float(row.get("数量", 0) or 0)))
        mode = row.get("攻牙方式", "待确认")
        original_machine = row.get("攻牙设备") or row.get("推荐设备", "CNC加工中心")
        row["攻牙设备"] = original_machine
        if mode == "设备刚性攻牙":
            row["设备攻牙数量"], row["人工攻牙数量"], row["推荐设备"] = total, 0, original_machine
            row["用户确认"], row["启用"] = True, True
        elif mode == "人工攻牙":
            row["设备攻牙数量"], row["人工攻牙数量"], row["推荐设备"] = 0, total, "人工工位"
            row["用户确认"], row["启用"] = True, True
        elif mode == "无螺纹加工":
            row["设备攻牙数量"], row["人工攻牙数量"] = 0, 0
            row["用户确认"], row["启用"] = True, True
        elif mode == "混合攻牙":
            device = int(float(row.get("设备攻牙数量", 0) or 0)); manual = int(float(row.get("人工攻牙数量", 0) or 0))
            if device + manual != total:
                errors.append(f"第 {index} 行 {process}：设备攻牙数量与人工攻牙数量之和必须等于 {total}。")
            if device < 0 or manual < 0:
                errors.append(f"第 {index} 行 {process}：攻牙数量不能为负数。")
            if not errors or not any(process in error for error in errors):
                row["用户确认"], row["启用"] = True, True
        else:
            errors.append(f"第 {index} 行 {process}：请选择攻牙方式。")
        if mode == "人工攻牙" and (total <= 0 or float(row.get("人工单孔时间(h)", 0) or 0) <= 0):
            errors.append(f"第 {index} 行 {process}：人工攻牙数量和单孔时间必须大于 0。")
        normalized.append(row)
    return normalized, errors


def _restore_missing_base_operations(confirmed: list[dict], identified: list[dict]) -> tuple[list[dict], bool]:
    """防止 data_editor 缓存仅保存攻牙行而遗漏自动识别的基础设备工序。"""
    has_base = any(row.get("类型") != "追加" and "螺纹加工" not in str(row.get("工序", "")) for row in confirmed)
    source_base = [row for row in identified if row.get("类型") != "追加" and "螺纹加工" not in str(row.get("工序", ""))]
    if has_base or not source_base:
        return confirmed, False
    restored, _ = _normalize_and_validate_operations(source_base)
    return [*confirmed, *restored], True


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
    editor_rows = []
    for raw in st.session_state["analysis_rows"]:
        row = dict(raw)
        row["启用"] = bool(row.get("启用", row.get("用户确认", False))) if row.get("类型") == "追加" else True
        row.pop("用户确认", None)  # 不再让螺纹行额外漏勾“用户确认”而被报价过滤
        editor_rows.append(row)
    edited = st.data_editor(pd.DataFrame(editor_rows), use_container_width=True, hide_index=True,
                            num_rows="dynamic", column_config={
                                "推荐设备": st.column_config.SelectboxColumn(options=[*list(config["machine_rates"]), "人工工位"]),
                                "计算类型": st.column_config.SelectboxColumn(options=["每件", "每批一次", "每对产品", "手动总价"]),
                                "攻牙方式": st.column_config.SelectboxColumn(options=["待确认", "无螺纹加工", "设备刚性攻牙", "人工攻牙", "混合攻牙"]),
                                "启用": st.column_config.CheckboxColumn(help="基础工序和有效攻牙方式会自动启用；仅追加工序可关闭。")}, key="operation_editor")
    # 编辑后但尚未确认时，旧报价立即作废，避免用户误把修改前的攻牙方式拿去报价。
    edited_rows, edited_errors = _normalize_and_validate_operations(edited.to_dict("records"))
    if st.session_state.get("confirmed_operations") and not edited_errors and edited_rows != st.session_state["confirmed_operations"]:
        st.session_state.pop("quote", None)
        st.info("工序表已修改，请点击“确认带入报价”后重新计算。")
    if st.button("确认带入报价", type="primary"):
        normalized, errors = _normalize_and_validate_operations(edited.to_dict("records"))
        if errors:
            for error in errors: st.error(error)
            st.stop()
        # 这是报价的唯一数据源。后续重新计算不再读取自动推荐的 analysis_rows。
        st.session_state["analysis_rows"] = normalized
        st.session_state["confirmed_operations"] = normalized
        st.session_state["operations_confirmed"] = True
        st.session_state.pop("quote", None)
        st.success("确认完成，已更新报价数据源。请在下方重新计算报价。")


def pricing_page(config: dict) -> None:
    st.header("新建报价")
    selected_product_type = st.selectbox("产品类型（用于设备推荐与生成工序）", ["自动识别", "大型机架/床身", "箱体/多方向孔系", "横梁支架", "小型阀体", "阀体", "泵体", "回转体", "车削件", "其他"], key="analysis_product_type")
    drawing_and_step_panel(config, selected_product_type); confirmation_panel(config)
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
            sample_quantity = st.number_input("打样数量", min_value=1, value=1, step=1, help="打样单价会把全部一次性费用按此数量分摊。")
            product_type = selected_product_type
            st.caption(f"当前产品类型：{product_type}（如需改变设备推荐，请在图纸分析前修改）")
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
        st.subheader("阶梯批量报价")
        st.caption("折扣由参数设置中的数量范围自动匹配，只作用于客户报价中的每件加工费；不改变真实工时，也不降低材料、表处或一次性费用。")
        tier_editor = st.data_editor(pd.DataFrame([{"数量": row.get("数量", 1)} for row in st.session_state["tier_rows"]]), use_container_width=True, hide_index=True, num_rows="dynamic", key="tier_editor")
        submitted = st.form_submit_button("计算报价", type="primary")
    if submitted:
        if not customer or not product_name: st.error("请填写客户名称和产品名称。"); return
        rows = st.session_state.get("confirmed_operations", [])
        if not rows:
            st.warning("请先在“自动识别结果确认表”点击“确认带入报价”。报价只读取已确认工序。")
            return
        rows, restored = _restore_missing_base_operations(rows, st.session_state.get("analysis_rows", []))
        if restored:
            st.session_state["confirmed_operations"] = rows
            st.warning("检测到确认数据遗漏了自动识别的基础设备工序，已补回粗加工、精加工、钻孔等基础工序；请复核后保存报价。")
        data = {"customer": customer, "product_name": product_name, "product_number": product_number,
                # 标题栏字段与用户可编辑的报价字段并存：PDF 标题栏优先，文件名仅是备用来源。
                "company_name": fields.get("company_name", ""), "english_company_name": fields.get("english_company_name", ""),
                "drawing_number": fields.get("drawing_number", product_number), "part_number": fields.get("part_number", ""),
                "identification_source": fields.get("identification_source", "需要人工确认"),
                "identification_confidence": fields.get("identification_confidence", "低"),
                "quantity": quantity, "product_type": product_type,
                "fixture_count": fixture_count, "sample_quantity": sample_quantity, "tier_rows": tier_editor.to_dict("records"), "material": material, "net_weight": net_weight, "casting_weight": casting_weight, "quote_mode": quote_mode,
                "casting_sales_rate": sale_rate, "packaging_mode": packaging_mode, "packaging_cost": packaging_cost, "surface_area_m2": step.get("total_planar_area_m2", 0.0), "surfaces": surfaces.to_dict("records")}
        result = calculate_quote(data, rows, config, additional.to_dict("records"), surfaces.to_dict("records"))
        result["auto_identified_operation_count"] = len(st.session_state.get("analysis_rows", []))
        if result["final_billed_operation_count"] < result["enabled_operation_count"]:
            st.error(f"计价工序异常：启用 {result['enabled_operation_count']} 项，但最终仅计价 {result['final_billed_operation_count']} 项。已阻止生成报价，请重新确认工序。")
            return
        st.session_state["tier_rows"] = data["tier_rows"]
        st.session_state["quote"] = (data, result, additional.to_dict("records"))
    if "quote" not in st.session_state: return
    data, result, _ = st.session_state["quote"]
    st.subheader("报价总览")
    st.caption(f"自动识别工序数：{result.get('auto_identified_operation_count', result['input_operation_count'])}；确认工序数：{len(result['confirmed_rows'])}；启用工序数：{result['enabled_operation_count']}；最终计价工序数：{result['final_billed_operation_count']}。")
    st.caption("打样与批量报价使用不同的数量分摊一次性费用；未确认的精度/检验或攻牙工序不会计入报价。")
    cal = calibration(data["product_type"])
    formula_time = result["base_time"] + result["extra_time"]
    st.caption(f"公式估算 {formula_time:.2f} h；历史相似产品修正 {formula_time * cal['factor']:.2f} h（系数 {cal['factor']:.2f}）；最终仍以已确认工序为准。")
    for col, (label, value, note) in zip(st.columns(4), [
        ("打样单价", result["sample_unit_price"], f"打样 {result['sample_quantity']} 件"),
        ("批量单价", result["unit_price"], f"本次批量 {data['quantity']} 件"),
        ("整批报价", result["batch_price"], "批量单价 × 数量"),
        ("批量平均单件成本", result["unit_cost"], "含一次性费用分摊"),
    ]): col.metric(label, f"¥ {value:,.2f}", note)
    st.subheader("单件成本组成")
    for col, (label, value) in zip(st.columns(5), [
        ("单件材料成本", result["casting_per_unit"]), ("原单件加工费", result["raw_processing_per_unit"]),
        ("优惠后单件加工费", result["discounted_processing_per_unit"]), ("其中人工攻牙费（已含）", result["tapping_labor_per_unit"]),
        ("单件热处理、表处、包装及其他", result["other_per_unit"] + result["surface_per_unit"] + result["packaging_per_unit"]),
    ]): col.metric(label, f"¥ {value:,.2f}")
    st.info(f"核对公式：材料 ¥{result['casting_per_unit']:.2f} + 优惠后加工费 ¥{result['discounted_processing_per_unit']:.2f} + 热处理/表处/包装/其他 ¥{result['other_per_unit'] + result['surface_per_unit'] + result['packaging_per_unit']:.2f} + 一次性费用分摊 ¥{result['one_time_per_unit']:.2f} = 批量平均单件成本 ¥{result['unit_cost']:.2f}。人工攻牙费已包含在“优惠后单件加工费”中，请勿重复相加。")
    with st.expander("工时与设备占机摘要", expanded=False):
        for col, (label, value) in zip(st.columns(5), [
            ("单件纯切削时间", result["pure_cutting_time"]), ("单件上下料时间", result["loading_time"]),
            ("每批一次性准备", result["batch_preparation_time"]),
            ("单件人工工时", result["labor_per_unit"] / max(1, config.get("manual_labor_rate", 35.0))),
            ("整批设备总时间", result["batch_equipment_time"]),
        ]): col.metric(label, f"{value:.2f} h")
        st.caption(f"其中单件人工攻牙：{result['tapping_labor_hours_per_unit']:.2f} h；整批人工攻牙：{result['tapping_labor_hours_per_unit'] * data['quantity']:.2f} h。")
    if result.get("pair_warning"):
        st.warning("本批数量为奇数，存在按对产品工序；最后一件没有匹配件，不能承诺成对等高交付。")
    st.subheader("一次性费用（整批仅计算一次）")
    one_time_rows = [{"项目": item["工序"], "设备/人工": item["设备"], "整批金额（元）": round(item["整批金额(元)"], 2)}
                     for item in result.get("operation_schedules", []) if item["计算类型"] in {"每批一次", "手动总价"}]
    for item in result.get("additional_details", []):
        if item["方式"] == "整批一次性费用":
            one_time_rows.append({"项目": item["名称"], "设备/人工": "外协/其他", "整批金额（元）": round(item["金额"], 2)})
    one_time_rows.append({"项目": "一次性费用合计", "设备/人工": "—", "整批金额（元）": round(result["one_time_cost"], 2)})
    st.dataframe(pd.DataFrame(one_time_rows), use_container_width=True, hide_index=True)
    st.subheader("阶梯批量报价")
    st.dataframe(pd.DataFrame(result.get("tier_results", [])), use_container_width=True, hide_index=True)
    brief_processes = [{"工序": item["工序"], "计算类型": item["计算类型"], "设备/人工": item["设备"],
                        "单件时间(h)": round(item["单件时间(h)"], 3),
                        "批量总时间(h)": round(item["整批设备时间(h)"] + item["整批人工时间(h)"], 3),
                        "单件成本（元）": round(item["整批金额(元)"] / max(1, data["quantity"]), 2)}
                       for item in result.get("operation_schedules", [])]
    with st.expander("工序详情", expanded=False):
        st.dataframe(pd.DataFrame(brief_processes), use_container_width=True, hide_index=True)
        with st.expander("工程计算详情", expanded=False):
            detailed = [{"工序": item["工序"], "特征标签": item.get("特征标签", ""), "特征类型": item.get("特征类型", ""), "规格": item.get("规格", ""),
                         "数量来源": item.get("数量来源", ""), "识别置信度": item.get("识别置信度", ""), "装夹编号": item.get("装夹编号", ""),
                         "加工方向": item.get("加工方向", ""), "刀具类型": item.get("刀具类型", ""), "时间计算依据": item.get("切削/时间依据", ""),
                         "执行方式": item.get("执行方式", ""), "计算类型": item["计算类型"], "设备/人工": item["设备"], "数量": item["数量"],
                         "单孔时间(h)": item.get("单孔时间(h)", 0), "单件时间(h)": item["单件时间(h)"], "每批时间(h)": item["每批时间(h)"],
                         "整批设备时间(h)": item["整批设备时间(h)"], "整批人工时间(h)": item["整批人工时间(h)"], "单价(元/h)": item["单价(元/h)"],
                         "整批金额(元)": item["整批金额(元)"], "判断依据": item["判断依据"]} for item in result.get("operation_schedules", [])]
            st.dataframe(pd.DataFrame(detailed), use_container_width=True, hide_index=True)
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
        st.subheader("批量加工费折扣（仅影响客户报价，不改变实际工时）")
        discount_editor = st.data_editor(pd.DataFrame(config.get("batch_processing_discounts", [])), hide_index=True, use_container_width=True, num_rows="dynamic")
        st.subheader("表面处理参数")
        surface_rows = [{"名称": name, "默认单价": item.get("rate", 0.0), "默认计价方式": item.get("basis", "按kg"), "最低收费": item.get("minimum", 0.0)} for name, item in config["surface_treatments"].items()]
        surface_editor = st.data_editor(pd.DataFrame(surface_rows), hide_index=True, use_container_width=True, num_rows="dynamic")
        if st.form_submit_button("保存参数", type="primary"):
            config["company_name"], config["profit_multiplier"] = company, profit
            for row in material_editor.to_dict("records"):
                n = row["材料"]; config["materials"][n] = float(row["成本价"]); config["casting_sales_prices"][n] = float(row["销售价"]); config["densities"][n] = float(row["密度"]); config["casting_blank_factors"][n] = float(row["毛坯系数"])
            for row in machine_editor.to_dict("records"):
                n = row["设备"]; config["machine_rates"][n] = float(row["成本价/h"]); config["direct_machine_rates"][n] = float(row["直接报价/h"]); config.setdefault("machine_capabilities", {})[n] = {"x": float(row["X"]), "y": float(row["Y"]), "z": float(row["Z"]), "max_weight": float(row["承重"]), "side_head": bool(row["侧铣头"]), "five_axis": bool(row["五面加工"]), "table_x": float(row["X"]), "table_y": float(row["Y"])}
            config["batch_processing_discounts"] = discount_editor.to_dict("records")
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
