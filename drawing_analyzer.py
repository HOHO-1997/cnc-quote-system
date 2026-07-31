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
    detailed_thread_groups = []
    for match in re.finditer(r"(?:(\d+)\s*[×xX-]\s*)?M\s*(\d+)(?:\s*[×xX]\s*([0-9.]+))?", text, flags=re.IGNORECASE):
        count = int(match.group(1) or 1)
        diameter = int(match.group(2)); pitch = match.group(3) or ""
        detailed_thread_groups.append({"规格": f"M{diameter}" + (f"×{pitch}" if pitch else ""), "数量": count, "直径": diameter})
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
            "gd_terms": gd_terms, "roughness": roughness, "threads": threads, "thread_diameters": diameters, "thread_groups": detailed_thread_groups,
            "threaded_count": threaded_count, "hole_features": hole_features, "drilled_count": drilled_count,
            "pair_height_requirement": pair_height, "explicit_grinding": explicit_grinding, "requires_turning": requires_turning,
            "heat_treatments": heat_treatments, "surface_processes": surface_processes, "tests": tests,
            "extra_sources": extra_sources}


# 以下为新版工程标注解析。保留旧函数以兼容旧引用，但运行时使用本定义。
def normalize_engineering_text(text: str) -> str:
    """逐行恢复 OCR/PDF 拆开的工程标注，绝不把不同图框行拼在一起。"""
    lines = []
    for raw in text.splitlines():
        # PyPDF 常输出数学直径符号 ∅，统一成工程图常用 Ø，避免孔特征漏识别。
        raw = raw.replace("\u2205", "\u00d8")
        line = raw.replace("＊", "×").replace("X", "×").replace("x", "×")
        line = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", line)
        # PDF/OCR 常把小数末位和公差等级拆开，如 7 . 1 0、6 H。
        line = re.sub(r"(\d+\.\d)\s+(\d)\b", r"\1\2", line)
        line = re.sub(r"(\d)\s+([HhGg])\b", r"\1\2", line)
        line = re.sub(r"([+\-])\s*(\d)", r"\1\2", line)
        # 仅合并真正逐位拆开的 M 7 2，不能把正常的 “M5 10(深度)” 拼成 M510。
        line = re.sub(r"M\s+(\d)\s+(\d)(?:\s+(\d))?", lambda m: "M" + "".join(x for x in m.groups() if x), line, flags=re.I)
        # 直径后面的数字也可能被逐个拆开：Ø 1 0 5、Ø 7 . 1 0。
        line = re.sub(r"Ø\s*([0-9][0-9 .]*)", lambda m: "Ø" + re.sub(r"\s+", "", m.group(1)), line)
        line = re.sub(r"([ØΦφ])\s*(\d(?:\s*\d)*)", lambda m: "Ø" + "".join(m.group(2).split()), line)
        line = re.sub(r"\s*×\s*", "×", line)
        line = re.sub(r"\s*([-+/])\s*", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _feature_count(match: re.Match) -> int:
    return int(match.group("count") or 1)


def _valid_tolerance(value: float) -> bool:
    return 0.001 <= abs(value) <= 10.0


def analyze_drawing(text: str) -> dict:
    engineering = normalize_engineering_text(text)
    upper = engineering.upper()
    thread_features, hole_features = [], []
    # 螺纹必须以 M + 数字开头；不会从 Ø7.1、Ø2.4、局部视图 M 或比例 2:1 推导。
    thread_re = re.compile(r"(?:(?P<count>\d+)\s*[-×]\s*)?M(?P<dia>\d+(?:\.\d+)?)(?:×(?P<pitch>\d+(?:\.\d+)?))?(?:-(?P<class>\d[HGhg]))?(?:\s*(?:深|DEPTH)\s*(?P<depth>\d+(?:\.\d+)?))?", re.I)
    for m in thread_re.finditer(engineering):
        dia = float(m.group("dia")); count = _feature_count(m)
        if not (1 <= dia <= 300) or m.group(0).upper().startswith("M:"):
            continue
        specification = f"M{m.group('dia')}" + (f"×{m.group('pitch')}" if m.group("pitch") else "")
        if m.group("class"):
            specification += "-" + m.group("class").upper()
        thread_features.append({"specification": specification, "nominal_diameter": dia, "pitch": _number_or_none(m.group("pitch")),
                                "tolerance_class": m.group("class").upper() if m.group("class") else None, "count": count,
                                "depth": _number_or_none(m.group("depth")), "through": False, "source_text": m.group(0), "confidence": "高"})
    # 孔必须以 Ø/Φ 开头；通/贯穿/THRU 与深/DEPTH 分别保存，不能视为螺纹。
    hole_re = re.compile(r"(?:(?P<count>\d+)\s*[-×]\s*)?[ØΦφ](?P<dia>\d+(?:\.\d+)?)(?P<tol>\s*[+\-]\d+(?:\.\d+)?\s*/?\s*[+\-]?\d*(?:\.\d+)?)?(?P<tail>[^\n]{0,30})", re.I)
    for m in hole_re.finditer(engineering):
        tail = m.group("tail") or ""; count = _feature_count(m)
        through = bool(re.search(r"通孔|贯穿|\bTHRU\b|\bTHROUGH\b|\b通\b", tail, re.I))
        depth_m = re.search(r"(?:深|深度|DEPTH)\s*(\d+(?:\.\d+)?)", tail, re.I)
        hole_features.append({"diameter": float(m.group("dia")), "count": count, "depth": _number_or_none(depth_m.group(1)) if depth_m else None,
                              "through": through, "countersink": "沉头" in tail or "锪" in tail, "counterbore": "沉孔" in tail or "锪平" in tail,
                              "reamed": "铰" in tail, "tolerance": (m.group("tol") or "").strip() or None,
                              "source_text": m.group(0), "confidence": "高" if through or depth_m else "中"})
    # 大型装配图常将孔写成“6×33通”“2×20贯穿”，没有 Ø 符号。
    # 只在带通孔/贯穿/明确公差时作为孔，避免把普通 4×120 尺寸阵列误判成孔。
    known_holes = {(round(item["diameter"], 4), item["count"]) for item in hole_features}
    for line in engineering.splitlines():
        plain_hole = re.search(r"(?P<count>\d+)\s*[×xX]\s*(?P<dia>\d+(?:\.\d+)?)(?P<tail>.*)$", line)
        if not plain_hole:
            continue
        tail = plain_hole.group("tail")
        has_hole_signal = bool(re.search(r"通孔|贯穿|通\b|\bTHRU\b|\bTHROUGH\b|(?:[+\-]\d+(?:\.\d+)?)", tail, re.I))
        if not has_hole_signal:
            continue
        count, diameter = int(plain_hole.group("count")), float(plain_hole.group("dia"))
        if not (0.5 <= diameter <= 500) or (round(diameter, 4), count) in known_holes:
            continue
        through = bool(re.search(r"通孔|贯穿|通\b|\bTHRU\b|\bTHROUGH\b", tail, re.I))
        hole_features.append({"diameter": diameter, "count": count, "depth": None, "through": through,
                              "countersink": False, "counterbore": False, "reamed": False,
                              "tolerance": None, "source_text": line, "confidence": "中" if through else "低"})
    dimensional_tolerances = []
    for m in re.finditer(r"(?:[ØΦφ]?\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)", engineering):
        value = float(m.group(1))
        if _valid_tolerance(value): dimensional_tolerances.append({"kind": "对称", "value": value, "source_text": m.group(0), "confidence": "高"})
    for m in re.finditer(r"(?:[ØΦφ]?\d+(?:\.\d+)?)\s*\+(\d+(?:\.\d+)?)\s*/\s*-(\d+(?:\.\d+)?)", engineering):
        values = [float(m.group(1)), float(m.group(2))]
        if all(_valid_tolerance(v) for v in values): dimensional_tolerances.append({"kind": "上下偏差", "upper": values[0], "lower": -values[1], "source_text": m.group(0), "confidence": "高"})
    gd_names = ["平面度", "平行度", "垂直度", "同轴度", "同心度", "位置度", "圆度", "圆跳动", "全跳动"]
    geometric_tolerances = []
    for name in gd_names:
        for m in re.finditer(re.escape(name) + r"[^\d\n]{0,16}(0?\.\d+)", engineering):
            value = float(m.group(1))
            if _valid_tolerance(value): geometric_tolerances.append({"kind": name, "value": value, "source_text": m.group(0), "confidence": "中"})
    roughness = [float(v) for v in re.findall(r"\bRA\s*(\d+(?:\.\d+)?)|(?:其余|其它)\s*(\d+(?:\.\d+)?)", upper) for v in v if v]
    min_tolerance = min([x["value"] for x in dimensional_tolerances if "value" in x] + [abs(x["upper"]) for x in dimensional_tolerances if "upper" in x] or [999])
    min_tolerance = None if min_tolerance == 999 else min_tolerance
    threaded_count = sum(x["count"] for x in thread_features)
    drilled_count = sum(x["count"] for x in hole_features)
    turning_terms = ["车削", "车床", "内孔", "外圆", "密封槽", "环槽", "同轴孔"]
    requires_turning = any(x["nominal_diameter"] >= 40 for x in thread_features) or any(term in engineering for term in turning_terms)
    pair_height = "等高" in engineering and any(v in engineering for v in ["两件", "2件", "成对", "配对"])
    explicit_grinding = any(v in engineering for v in ["磨削", "配磨", "成对磨"])
    heat_treatments = [name for name, keys in {"退火": ["退火", "回火"], "人工时效": ["人工时效"], "去应力处理": ["去应力", "应力消除"]}.items() if any(k in engineering for k in keys)]
    surface_processes = [name for name in ["喷砂", "喷漆", "喷粉", "黑漆", "氧化", "电泳", "磷化"] if name in engineering]
    tests = [name for name, keys in {"水压测试": ["水压", "压力测试"], "密封测试": ["密封测试", "气密"], "材质报告": ["材质报告", "材质证明"], "三坐标检测": ["三坐标", "CMM"]}.items() if any(k in engineering for k in keys)]
    extra_sources = []
    if min_tolerance is not None and min_tolerance <= 0.05:
        extra_sources.append({"source": f"尺寸公差 ±{min_tolerance:.3f} mm", "hours": 0.20, "recommended_equipment": "CNC加工中心", "confidence": "中"})
    if geometric_tolerances and min(x["value"] for x in geometric_tolerances) <= 0.01:
        extra_sources.append({"source": f"形位精度 {min(x['value'] for x in geometric_tolerances):.3f} mm", "hours": 0.30, "recommended_equipment": "磨床", "confidence": "中"})
    return {"text_available": bool(engineering.strip()), "normalized_text": engineering, "thread_features": thread_features, "hole_features": hole_features,
            "threads": [(str(x["nominal_diameter"]), str(x["pitch"] or "")) for x in thread_features], "thread_diameters": [x["nominal_diameter"] for x in thread_features],
            "thread_groups": [{"规格": x["specification"], "数量": x["count"], "直径": x["nominal_diameter"]} for x in thread_features], "threaded_count": threaded_count,
            "drilled_count": drilled_count, "dimensional_tolerances": dimensional_tolerances, "geometric_tolerances": geometric_tolerances,
            "geometric_values": [x["value"] for x in geometric_tolerances], "gd_terms": [x["kind"] for x in geometric_tolerances], "roughness": roughness,
            "min_tolerance": min_tolerance, "pair_height_requirement": pair_height, "explicit_grinding": explicit_grinding, "requires_turning": requires_turning,
            "heat_treatments": heat_treatments, "surface_processes": surface_processes, "tests": tests, "extra_sources": extra_sources}


def _number_or_none(value: str | None) -> float | None:
    return float(value) if value else None
