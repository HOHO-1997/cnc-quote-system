"""机械加工厂内部自动报价系统（第二版）。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
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
    "densities": {"灰铁": 7.2, "球铁": 7.29, "铸铝": 2.7},  # g/cm³
    "surface_treatments": {"无处理": 0.0, "喷砂": 2.0, "氧化": 7.0, "电泳": 10.0,
                           "黑漆": 0.5, "磷化": 4.0, "喷粉": 3.0, "喷漆": 2.0},
    "machine_rates": {"CNC加工中心": 90.0, "车床": 65.0, "龙门铣": 200.0, "卧式加工中心": 175.0},
    "default_stock_allowance_mm": 5.0,
    "casting_blank_factors": {"灰铁": 1.18, "球铁": 1.22, "铸铝": 1.12},
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
    changed = False
    config.setdefault("densities", DEFAULT_CONFIG["densities"])
    if config["densities"].get("球铁") == 7.1:  # 兼容第二版旧默认值
        config["densities"]["球铁"] = 7.29
        changed = True
    if "casting_blank_factors" not in config:
        config["casting_blank_factors"] = DEFAULT_CONFIG["casting_blank_factors"]
        changed = True
    if "machine_rates" not in config:
        old_rate = config.pop("cnc_rate", 60.0)
        config["machine_rates"] = {"CNC加工中心": 90.0, "车床": 65.0,
                                   "龙门铣": 200.0, "卧式加工中心": 175.0}
        changed = True
    if changed:
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
        "product_name": r"(?:零件名称|产品名称|图名|part\s*name)[：:\s]*([^\n\r]{2,40})",
        "customer": r"(?:客户名称|客户|customer)[：:\s]*([^\n\r]{2,40})",
        "weight": r"(?:重量|weight)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:kg|公斤)?",
        "quantity": r"(?:数量|quantity|qty)[：:\s]*([0-9]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            result[key] = float(match.group(1)) if key == "weight" else (int(match.group(1)) if key == "quantity" else match.group(1))
    if "customer" not in result:
        company = re.search(r"([\u4e00-\u9fff]{2,30}(?:有限责任公司|股份有限公司|有限公司))", text)
        if company: result["customer"] = company.group(1)
    # 优先识别“材料/材质”字段，而不是只要在图纸任意位置看到材料名称就判定。
    # 例如图纸的技术要求里可能同时提及灰铁和球铁，字段行才是产品实际材质。
    normalized = text.replace(" ", "").upper()
    material_rules = [
        ("球铁", r"(?:材料|材质|MATERIAL)[：:#-]*[^\n]{0,35}(?:球铁|QT\d+|FCD\d+|DUCTILE)"),
        ("铸铝", r"(?:材料|材质|MATERIAL)[：:#-]*[^\n]{0,35}(?:铸铝|ZL\d+|ADC\d+|A356|ALSI)"),
        ("灰铁", r"(?:材料|材质|MATERIAL)[：:#-]*[^\n]{0,35}(?:灰铁|HT\d+|FC\d+|GRAYIRON)"),
    ]
    for material, pattern in material_rules:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            result["material"] = material
            break
    # 未找到材料字段时，再用牌号/名称做保守回退；球铁优先于灰铁，避免 QT 图纸被误判。
    if "material" not in result:
        if re.search(r"球铁|QT\d+|FCD\d+|DUCTILE", normalized): result["material"] = "球铁"
        elif re.search(r"铸铝|ZL\d+|ADC\d+|A356|ALSI", normalized): result["material"] = "铸铝"
        elif re.search(r"灰铁|HT\d+|FC\d+|GRAYIRON", normalized): result["material"] = "灰铁"
    return result, text


def analyze_drawing_text(text: str) -> dict:
    """从 2D 图纸文字提取精度/工艺信号，作为工时复核依据。"""
    normalized = re.sub(r"\s+", "", text)
    # 对 PDF 文字保留空格/换行做数量与公差解析，避免“8 x M5”粘连成错误大数字。
    bilateral = re.findall(r"±\s*([0-9]+(?:\.[0-9]+)?)\b", text)
    unilateral = re.findall(r"[+−-]\s*([0-9]+(?:\.[0-9]+)?)\b", text)
    tolerance_values = [float(value) for value in bilateral + unilateral]
    fit_matches = re.findall(r"(?:[A-Za-z]\d{1,2}|\d{1,2}[a-z])", normalized)
    gd_terms = [term for term in ["平面度", "平行度", "垂直度", "同轴度", "位置度", "圆跳动", "全跳动"] if term in normalized]
    roughness = [float(value) for value in re.findall(r"(?:RA|Ra|粗糙度)[：:≤]*([0-9]+(?:\.[0-9]+)?)", text)]
    threads = re.findall(r"M\s*\d+(?:[×xX]\s*\d+(?:\.\d+)?)?(?:\s*[-]\s*[0-9A-Za-z]+)?", text, flags=re.IGNORECASE)
    hole_matches = re.findall(r"(?m)(\d+)\s*[-×xX]\s*[ΦØ]\s*([0-9]+(?:\.\d+)?)", text)
    thread_groups = re.findall(r"(?m)(\d+)\s*[-×xX]\s*M\s*\d+", text, flags=re.IGNORECASE)
    threaded_count = sum(int(count) for count in thread_groups)
    drilled_count = sum(int(count) for count, _ in hole_matches)
    min_tolerance = min(tolerance_values) if tolerance_values else None
    suggestions, extra_hours = [], 0.0
    if min_tolerance is not None and min_tolerance <= 0.05:
        suggestions.append(f"检测到最严尺寸公差约 ±{min_tolerance:.3f} mm：建议增加精加工和专用量检具检验。")
        extra_hours += 0.35
    elif min_tolerance is not None and min_tolerance <= 0.10:
        suggestions.append("检测到 ±0.10 mm 级尺寸公差：建议保留精加工余量并进行首件检验。")
        extra_hours += 0.18
    if gd_terms:
        suggestions.append("检测到形位要求（" + "、".join(gd_terms) + "）：建议以基准面一次装夹/复装夹加工，并增加检验工时。")
        extra_hours += 0.25
    if roughness and min(roughness) <= 1.6:
        suggestions.append("检测到 Ra1.6 或更高表面要求：建议精镗/精铣，必要时评估磨削工序。")
        extra_hours += 0.20
    if threads:
        suggestions.append("检测到螺纹（" + "、".join(sorted(set(threads))[:5]) + "）：建议安排钻孔、攻牙或车螺纹工序。")
        extra_hours += 0.03 * max(threaded_count, len(threads))
    if hole_matches:
        hole_total = drilled_count
        suggestions.append(f"检测到约 {hole_total} 个成组孔：建议计入钻孔/扩孔/倒角工时。")
        extra_hours += 0.03 * hole_total
    return {"min_tolerance": min_tolerance, "gd_terms": gd_terms, "roughness": roughness,
            "threads": threads, "hole_matches": hole_matches, "threaded_count": threaded_count,
            "drilled_count": drilled_count, "suggestions": suggestions,
            "extra_hours": round(extra_hours, 2)}


def analyze_step(file_bytes: bytes, material: str, config: dict, stock_allowance_mm: float,
                 drawing_analysis: dict | None = None) -> dict:
    """使用 OpenCascade 读取 STEP 实体，取得真实体积、尺寸及基础加工特征。"""
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepGProp import BRepGProp
        from OCP.Bnd import Bnd_Box
        from OCP.GProp import GProp_GProps
        from OCP.GeomAbs import GeomAbs_BSplineSurface, GeomAbs_Cylinder
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
    except ImportError as error:
        return {"available": False, "message": f"STEP 几何引擎加载失败：{error}。请检查部署日志中的 cadquery-ocp 安装结果。"}

    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as temp:
            temp.write(file_bytes)
            temp_name = temp.name
        reader = STEPControl_Reader()
        if reader.ReadFile(temp_name) != IFSelect_RetDone:
            return {"available": False, "message": "STEP 文件无法读取，请确认文件未损坏且包含实体模型。"}
        reader.TransferRoots()
        shape = reader.OneShape()
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        dimensions = [max(0.0, xmax - xmin), max(0.0, ymax - ymin), max(0.0, zmax - zmin)]
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        volume_mm3 = max(0.0, props.Mass())
        if volume_mm3 <= 0:
            return {"available": False, "message": "模型未检测到封闭实体，无法计算真实体积和重量。"}

        faces = cylinders = spline_faces = 0
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            faces += 1
            surface = BRepAdaptor_Surface(TopoDS.Face_s(explorer.Current()), True)
            if surface.GetType() == GeomAbs_Cylinder: cylinders += 1
            if surface.GetType() == GeomAbs_BSplineSurface: spline_faces += 1
            explorer.Next()
        density = float(config["densities"].get(material, 7.0))
        weight_kg = volume_mm3 * density / 1_000_000
        # 铸件不是外接长方体：只在加工面保留余量，用成品重量倍率估算毛坯。
        # 避免将镂空框架的整个外接包络误算成数吨实心毛坯。
        blank_factor = float(config.get("casting_blank_factors", {}).get(material, 1.20))
        blank_weight_kg = weight_kg * blank_factor
        # density uses g/cm³: mass difference (kg) must be converted to grams
        # before converting it to cm³.  Using 1,000,000 here inflated removal
        # volume (and therefore roughing time) by 1,000 times.
        removal_cm3 = (blank_weight_kg - weight_kg) * 1_000 / density
        blank_dimensions = [dimension + 2 * stock_allowance_mm for dimension in dimensions]
        max_dim = max(dimensions)
        difficulty_score = faces + cylinders * 3 + spline_faces * 7 + (8 if max_dim > 1000 else 0)
        threads = (drawing_analysis or {}).get("threaded_count", 0)
        drilled = (drawing_analysis or {}).get("drilled_count", 0)
        tight_tolerance = (drawing_analysis or {}).get("min_tolerance")
        is_large_frame = max_dim >= 1500 and (threads + drilled >= 80 or faces >= 500)
        if is_large_frame:
            # 按大型铸造机架批量加工工艺模板估算，避免把每个圆柱面都误当作独立孔。
            setup_hours = 5.0
            rough_hours = 9.0 + max(0.0, removal_cm3 - 30_000) / 6_000
            finish_hours = 9.0 + (2.0 if tight_tolerance and tight_tolerance <= 0.01 else 0.5)
            side_end_hours = 8.0
            boring_hours = 5.0
            hole_hours = min(15.0, 2.5 + threads * 0.045 + drilled * 0.025)
            contour_hours = 0.0
            inspection_hours = 4.0 + (1.5 if tight_tolerance and tight_tolerance <= 0.01 else 0.0)
            total_hours = setup_hours + rough_hours + finish_hours + side_end_hours + boring_hours + hole_hours + inspection_hours
            time_breakdown = {"上机找正与装夹": setup_hours, "粗铣基准与主要平面": rough_hours,
                              "精铣导轨/安装面": finish_hours, "侧面及端面加工": side_end_hours,
                              "大孔扩孔与精镗": boring_hours, "钻孔攻牙": hole_hours,
                              "机内测量、去毛刺与检验": inspection_hours}
            first_piece_hours = total_hours + 12.0
            process_template = "大型铸造机架（批量龙门加工）"
        else:
            setup_hours = 0.45 + (0.25 if max_dim > 500 else 0) + (0.35 if max_dim > 1000 else 0)
            rough_hours = removal_cm3 / (2200 if material == "铸铝" else 1300)
            finish_hours = faces * 0.012
            hole_hours = min(cylinders * 0.055, 2.5 + threads * 0.045 + drilled * 0.025) if drawing_analysis else cylinders * 0.055
            contour_hours = spline_faces * 0.12
            inspection_hours = 0.10 + (0.15 if difficulty_score >= 30 else 0)
            total_hours = max(0.25, setup_hours + rough_hours + finish_hours + hole_hours + contour_hours + inspection_hours)
            time_breakdown = {"装夹与编程": setup_hours, "粗加工（去除材料）": rough_hours,
                              "精加工（平面/轮廓）": finish_hours, "孔/圆柱特征": hole_hours,
                              "自由曲面": contour_hours, "检验与去毛刺": inspection_hours}
            first_piece_hours = total_hours + (4.0 if max_dim > 500 else 1.5)
            process_template = "通用铸件加工"
        difficulty = "高" if difficulty_score >= 80 else ("中" if difficulty_score >= 30 else "低")
        recommended = {name: 0.0 for name in config["machine_rates"]}
        primary = "龙门铣" if max_dim > 1000 else ("卧式加工中心" if max_dim > 500 or weight_kg > 50 else "CNC加工中心")
        recommended[primary] = round(total_hours, 2)
        if not is_large_frame and cylinders >= 4 and "车床" in recommended:
            lathe_hours = round(min(total_hours * 0.45, 0.3 + cylinders * 0.035), 2)
            recommended["车床"] = lathe_hours
            recommended[primary] = round(max(0.15, total_hours - lathe_hours), 2)
        return {"available": True, "dimensions": dimensions, "blank_dimensions": blank_dimensions, "stock_allowance_mm": stock_allowance_mm,
                "volume_mm3": volume_mm3, "actual_weight": weight_kg, "blank_weight": blank_weight_kg,
                "faces": faces, "cylinders": cylinders, "spline_faces": spline_faces, "removal_cm3": removal_cm3,
                "difficulty": difficulty, "recommended_machine_hours": recommended, "source": "STEP 实体体积",
                "time_breakdown": time_breakdown, "batch_hours": round(total_hours, 1),
                "first_piece_hours": round(first_piece_hours, 1), "process_template": process_template}
    except Exception as error:
        return {"available": False, "message": f"STEP 几何分析失败：{error}"}
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


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
        messages.append(f"STEP 实体分析的加工难度为“{step_result['difficulty']}”。自动工时仅作前期报价依据，请结合公差、装夹、刀具和检验要求复核。")
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
        stock_allowance = st.number_input("毛坯加工余量（每侧，mm）", min_value=0.0,
                                          value=float(config.get("default_stock_allowance_mm", 5.0)), step=0.5)
        if pdf_file and st.button("读取 PDF 图纸"):
            try:
                pdf_result, raw_text = extract_pdf_info(pdf_file.getvalue())
                st.session_state["drawing_analysis"] = analyze_drawing_text(raw_text)
                for key, value in pdf_result.items():
                    widget_key = {"product_number": "product_number_input", "product_name": "product_name_input",
                                  "customer": "customer_input", "weight": "weight_input", "quantity": "quantity_input",
                                  "material": "material_input"}.get(key)
                    if widget_key: st.session_state[widget_key] = value
                st.success("图纸读取完成，已识别的信息会带入下方表单。")
                if not raw_text.strip(): st.warning("该 PDF 未包含可复制文字，扫描图纸需要后续接入 OCR。")
            except Exception as error:
                st.error(f"PDF 读取失败：{error}")
        if step_file and st.button("分析 STEP 模型"):
            step_result = analyze_step(step_file.getvalue(), selected_material, config, stock_allowance,
                                       st.session_state.get("drawing_analysis"))
            st.session_state["step_result"] = step_result
            if step_result.get("available"):
                st.session_state["weight_input"] = round(step_result["actual_weight"], 3)
                for machine, hours in step_result["recommended_machine_hours"].items():
                    st.session_state[f"hours_{machine}"] = hours
                st.success("模型实体分析完成：真实体积重量与建议工时已带入下方表单。")
            else: st.warning(step_result["message"])
    step_result = st.session_state.get("step_result", step_result)
    drawing_analysis = st.session_state.get("drawing_analysis")
    if drawing_analysis:
        st.subheader("2D 图纸精度与工艺分析")
        for suggestion in drawing_analysis["suggestions"] or ["未检测到可解析的公差/工艺文字，请人工复核图纸。"]:
            st.write(f"- {suggestion}")
        st.caption(f"建议追加精加工与检验工时：{drawing_analysis['extra_hours']:.2f} 小时（请工艺员确认）。")
    if step_result and step_result.get("available"):
        dims = step_result["dimensions"]
        st.info(f"STEP 实体分析：尺寸 {dims[0]:.1f} × {dims[1]:.1f} × {dims[2]:.1f} mm；"
                f"实体体积 {step_result['volume_mm3'] / 1000:.1f} cm³；重量 {step_result['actual_weight']:.3f} kg；"
                f"加工难度 {step_result['difficulty']}。")
        blank_dims = step_result["blank_dimensions"]
        st.success(f"铸件毛坯估算（局部加工面余量，非外接实心方料）："
                   f"外形参考 {blank_dims[0]:.1f} × {blank_dims[1]:.1f} × {blank_dims[2]:.1f} mm，"
                   f"毛坯重量 {step_result['blank_weight']:.1f} kg，预计加工去除 {step_result['removal_cm3']:.0f} cm³。")
        st.caption(f"特征识别：{step_result['faces']} 个面、{step_result['cylinders']} 个圆柱面、"
                   f"{step_result['spline_faces']} 个自由曲面；建议按下方自动带入的工时复核。")
        st.write(f"自动工时构成 - {step_result['process_template']}（小时，仅作工艺员复核起点）：")
        st.dataframe(pd.DataFrame(list(step_result["time_breakdown"].items()), columns=["工序", "建议工时（小时）"]),
                     hide_index=True, use_container_width=True)
        st.info(f"建议批量占机：{step_result['batch_hours']:.1f} 小时；首件含编程/工艺准备：{step_result['first_piece_hours']:.1f} 小时。")

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
        st.subheader("材料密度（g/cm³）")
        densities = {n: st.number_input(n, min_value=0.1, value=float(v), step=0.01, key=f"d_{n}") for n, v in config["densities"].items()}
        st.subheader("铸件毛坯重量系数（局部加工余量）")
        blank_factors = {n: st.number_input(n, min_value=1.0, value=float(v), step=0.01, key=f"b_{n}") for n, v in config["casting_blank_factors"].items()}
        st.subheader("设备工时单价（元/小时）")
        rates = {n: st.number_input(n, min_value=0.0, value=float(v), step=5.0, key=f"r_{n}") for n, v in config["machine_rates"].items()}
        st.subheader("表面处理单价（元/kg）")
        treatments = {n: st.number_input(n, min_value=0.0, value=float(v), step=0.5, key=f"s_{n}") for n, v in config["surface_treatments"].items()}
        if st.form_submit_button("保存参数", type="primary"):
            save_config({"company_name": company_name, "profit_multiplier": profit, "materials": materials,
                         "densities": densities, "machine_rates": rates, "surface_treatments": treatments,
                         "default_stock_allowance_mm": config.get("default_stock_allowance_mm", 5.0),
                         "casting_blank_factors": blank_factors})
            st.success("参数已保存。")


def main() -> None:
    st.set_page_config(page_title="机械加工自动报价系统", page_icon="⚙️", layout="wide")
    init_database(); config = load_config()
    st.sidebar.title("⚙️ 自动报价系统")
    page = st.sidebar.radio("功能", ["新建报价", "历史报价", "成本参数设置"])
    {"新建报价": pricing_page, "历史报价": lambda _: history_page(), "成本参数设置": settings_page}[page](config)


if __name__ == "__main__": main()
