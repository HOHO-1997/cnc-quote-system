"""PDF/DXF 图纸文字与工艺强信号识别。无法可靠识别时保留不确定性。"""
from __future__ import annotations

import re
from io import BytesIO, StringIO
from pathlib import Path

from pypdf import PdfReader


def product_name_from_filename(name: str) -> str:
    stem = Path(name).stem.strip()
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", stem))
    return chinese or stem


def _ocr_pdf(file_bytes: bytes) -> str:
    """OCR 是可选能力；云端未安装引擎时返回空字符串且不阻断报价。"""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
        pages = convert_from_bytes(file_bytes, dpi=250)
        return "\n".join(pytesseract.image_to_string(page, lang="chi_sim+eng") for page in pages)
    except Exception:
        return ""


def extract_pdf(file_bytes: bytes, filename: str) -> tuple[dict, str, bool]:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(file_bytes)).pages)
    used_ocr = False
    if len(re.sub(r"\s+", "", text)) < 20:
        ocr_text = _ocr_pdf(file_bytes)
        if ocr_text:
            text, used_ocr = ocr_text, True
    return extract_fields(text, filename), text, used_ocr


def extract_dxf(file_bytes: bytes, filename: str) -> tuple[dict, str]:
    try:
        import ezdxf
        doc = ezdxf.read(StringIO(file_bytes.decode("gbk", errors="replace")))
        values = []
        for layout in doc.layouts:
            for entity in layout:
                if entity.dxftype() in {"TEXT", "ATTRIB"}:
                    values.append(entity.dxf.text)
                elif entity.dxftype() == "MTEXT":
                    values.append(entity.plain_text())
        text = "\n".join(values)
    except Exception as error:
        raise ValueError(f"DXF 读取失败：{error}") from error
    return extract_fields(text, filename), text


def extract_fields(text: str, filename: str = "") -> dict:
    result: dict[str, object] = {"product_name": product_name_from_filename(filename)} if filename else {}
    patterns = {
        "product_number": r"(?:图号|零件号|产品编号|零件代号|ITEM\s*NO)[：:\s#-]*([A-Za-z0-9_.#/-]{3,})",
        "product_name": r"(?:零件名称|产品名称|图名|名称|PART\s*NAME)[：:\s]*([^\n\r]{2,40})",
        "customer": r"(?:客户名称|客户|CUSTOMER)[：:\s]*([^\n\r]{2,40})",
        "weight": r"(?:重量|WEIGHT)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*(?:kg|KG|公斤)?",
        "quantity": r"(?:数量|QTY|QUANTITY)[：:\s]*([0-9]+)",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            result[name] = float(value) if name == "weight" else (int(value) if name == "quantity" else value)
    normalized = re.sub(r"\s+", "", text).upper()
    material_rules = [("球铁", r"QT\d+|FCD\d+|DUCTILE|球铁"), ("铸铝", r"ZL\d+|ADC\d+|A356|ALSI|铸铝"), ("灰铁", r"HT\d+|FC\d+|GRAYIRON|灰铁")]
    for material, rule in material_rules:
        if re.search(rule, normalized):
            result["material"] = material
            break
    return result


def analyze_drawing(text: str) -> dict:
    normalized = re.sub(r"\s+", "", text)
    # 只把带 +/-、形位框邻近或 Ra 标记的数字作为精度信号，普通尺寸不进入公差判断。
    bilateral = [float(v) for v in re.findall(r"±\s*([0-9]+(?:\.[0-9]+)?)", text)]
    unilateral = [float(v) for v in re.findall(r"[+−-]\s*0?\.([0-9]+)", text)]
    unilateral = [v / (10 ** len(str(int(v)))) if v >= 1 else v for v in unilateral]
    tolerance_values = bilateral + unilateral
    min_tolerance = min(tolerance_values) if tolerance_values else None
    gd_terms = [term for term in ["平面度", "平行度", "垂直度", "同轴度", "位置度", "圆跳动", "全跳动", "圆度"] if term in normalized]
    # 兼容 PDF 文字提取的“形位符号 + 0.01”场景；界面会注明低置信度。
    geometric_values = [float(v) for v in re.findall(r"(?:平面度|平行度|垂直度|同轴度|圆跳动|全跳动|圆度)[^0-9]{0,12}(0?\.\d+)", text)]
    roughness = [float(v) for v in re.findall(r"(?:RA|Ra|粗糙度)\s*[：:≤<]?\s*([0-9]+(?:\.[0-9]+)?)", text)]
    threads = re.findall(r"(?:\d+\s*[×xX-]\s*)?M\s*(\d+)(?:\s*[×xX]\s*([0-9.]+))?", text, flags=re.IGNORECASE)
    thread_groups = re.findall(r"(?m)(\d+)\s*[×xX-]\s*M\s*\d+", text, flags=re.IGNORECASE)
    threaded_count = sum(int(x) for x in thread_groups) or len(threads)
    holes = re.findall(r"(?m)(\d+)\s*[×xX-]\s*[ΦØ]\s*([0-9]+(?:\.[0-9]+)?)(?:\s*[深深]\s*([0-9.]+))?", text)
    drilled_count = sum(int(count) for count, _, _ in holes)
    hole_features = [{"count": int(c), "diameter": float(d), "depth": float(depth) if depth else None} for c, d, depth in holes]
    pair_height = "等高" in normalized and any(v in normalized for v in ["两件", "2件", "每两", "成对", "配对"])
    explicit_grinding = any(v in normalized for v in ["磨削", "配磨", "配对磨", "成对磨"])
    turning_markers = any(v in normalized for v in ["密封槽", "同轴孔", "车削", "车床", "外圆", "内孔"])
    diameters = [int(d) for d, _ in threads]
    requires_turning = any(d >= 40 for d in diameters) or turning_markers
    heat_treatments = [name for name, keys in {"退火": ["退火", "回火"], "人工时效": ["人工时效"], "去应力处理": ["去应力", "应力消除"]}.items() if any(k in normalized for k in keys)]
    surface_processes = [name for name in ["喷砂", "喷漆", "喷粉", "黑漆", "氧化", "电泳", "磷化"] if name in normalized]
    tests = [name for name, keys in {"水压测试": ["水压", "压力测试"], "密封测试": ["密封测试", "气密"], "材质报告": ["材质报告", "材质证明"], "三坐标检测": ["三坐标", "CMM"]}.items() if any(k in normalized for k in keys)]
    extra_sources = []
    if min_tolerance is not None and min_tolerance <= 0.05:
        extra_sources.append({"source": f"尺寸公差 ±{min_tolerance:.3f} mm", "hours": 0.20, "recommended_equipment": "CNC加工中心", "confidence": "中"})
    if geometric_values and min(geometric_values) <= 0.01:
        extra_sources.append({"source": f"形位精度 {min(geometric_values):.3f} mm", "hours": 0.30, "recommended_equipment": "磨床", "confidence": "中"})
    if pair_height:
        extra_sources.append({"source": "两件/成对等高交付", "hours": 0.50, "recommended_equipment": "磨床", "confidence": "高"})
    return {"text_available": bool(text.strip()), "min_tolerance": min_tolerance, "geometric_values": geometric_values,
            "gd_terms": gd_terms, "roughness": roughness, "threads": threads, "thread_diameters": diameters,
            "threaded_count": threaded_count, "hole_features": hole_features, "drilled_count": drilled_count,
            "pair_height_requirement": pair_height, "explicit_grinding": explicit_grinding, "requires_turning": requires_turning,
            "heat_treatments": heat_treatments, "surface_processes": surface_processes, "tests": tests,
            "extra_sources": extra_sources}
