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
        # 同时启用简体、繁体和英文，标题栏公司名经常是繁体中文。
        return "\n".join(pytesseract.image_to_string(page, lang="chi_sim+chi_tra+eng") for page in pages)
    except Exception:
        return ""


def title_block_preview(file_bytes: bytes) -> bytes | None:
    """Return the bottom-right title-block crop for human verification.

    This is a visual aid only: all machine extraction remains text/table based.
    It intentionally fails quietly on deployments without Poppler.
    """
    try:
        from pdf2image import convert_from_bytes
        image = convert_from_bytes(file_bytes, dpi=160, first_page=1, last_page=1)[0]
        width, height = image.size
        crop = image.crop((int(width * 0.48), int(height * 0.56), width, height))
        output = BytesIO(); crop.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return None


def extract_pdf(file_bytes: bytes, filename: str) -> tuple[dict, str, bool]:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(file_bytes)).pages)
    used_ocr = False
    # 有些中文 CAD 字体能提取到字符但出现乱码；这时仍要尝试 OCR，不能把乱码当公司名。
    damaged_text = "�" in text or text.count("\ufffd") > 0
    # 标题栏的嵌入 CAD 字体即使有文字，也可能映射成错误汉字；带有标题栏关键词时
    # 同步 OCR。型号/图号仍保留矢量文本，中文公司名优先取 OCR 段。
    title_block_present = bool(re.search(r"(?:公司|COMPANY|PARTS?\s*NO|TITLE|DWG\s*NO)", text, re.I))
    if len(re.sub(r"\s+", "", text)) < 20 or damaged_text or title_block_present:
        ocr_text = _ocr_pdf(file_bytes)
        if ocr_text:
            text = text + "\n[[OCR_TITLE_TEXT]]\n" + ocr_text
            used_ocr = True
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
    # 备注/材料说明中的 M1、M2……是条款编号，不是螺纹。孤立 M 数字没有数量、螺距、等级、深度或 THRU 时一律不计价。
    rejected_thread_notes = []
    qualified_threads = []
    for feature in thread_features:
        source = str(feature["source_text"])
        qualified = ("×" in source or "x" in source.lower() or feature.get("pitch") is not None or
                     feature.get("tolerance_class") is not None or bool(re.search(r"THRU|DEPTH|深", source, re.I)))
        if not qualified:
            rejected_thread_notes.append(source)
            continue
        qualified_threads.append(feature)
    thread_features = qualified_threads
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
    # 英文图纸常把孔写成“12.25 ±0.20 2X THRU”，数量在尺寸之后且没有 Ø。
    for line in engineering.splitlines():
        english_hole = re.search(r"(?P<dia>\d+(?:\.\d+)?)\s*±\s*(?P<tol>0?\.\d{1,2})\s*(?P<count>\d+)\s*[×Xx]\s*(?P<thru>THRU|THROUGH)\b", line, re.I)
        if not english_hole:
            continue
        diameter, count = float(english_hole.group("dia")), int(english_hole.group("count"))
        if not 0.5 <= diameter <= 500:
            continue
        hole_features.append({"diameter": diameter, "count": count, "depth": None, "through": True,
                              "countersink": False, "counterbore": False, "reamed": False,
                              "tolerance": "±" + english_hole.group("tol"), "source_text": line, "confidence": "高"})
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
    datum_refs = sorted(set(re.findall(r"\b[A-D]\b", engineering)))
    section_count = len(re.findall(r"(?:SECTION|剖面)\s*[A-Z]", upper))
    machining_datums = "MACHINING DATUM" in upper or "加工基准" in engineering
    post_machined = "POST MACHINED" in upper or "精加工" in engineering
    # 该提示仅用于提出独立槽工序候选，必须结合 STEP 凹槽几何确认，避免把普通铸造面直接当成槽。
    slot_candidate = {"candidate": bool(post_machined and machining_datums and section_count >= 2),
                      "section_count": section_count, "datum_count": len(datum_refs),
                      "confidence": "中" if post_machined and machining_datums else "低"}
    machined_surface_estimate = max(1, min(16, 2 + len(datum_refs) + section_count + (2 if post_machined else 0)))
    return {"text_available": bool(engineering.strip()), "normalized_text": engineering, "thread_features": thread_features, "hole_features": hole_features,
            "threads": [(str(x["nominal_diameter"]), str(x["pitch"] or "")) for x in thread_features], "thread_diameters": [x["nominal_diameter"] for x in thread_features],
            "thread_groups": [{"规格": x["specification"], "数量": x["count"], "直径": x["nominal_diameter"]} for x in thread_features], "threaded_count": threaded_count,
            "drilled_count": drilled_count, "dimensional_tolerances": dimensional_tolerances, "geometric_tolerances": geometric_tolerances,
            "geometric_values": [x["value"] for x in geometric_tolerances], "gd_terms": [x["kind"] for x in geometric_tolerances], "roughness": roughness,
            "min_tolerance": min_tolerance, "pair_height_requirement": pair_height, "explicit_grinding": explicit_grinding, "requires_turning": requires_turning,
            "heat_treatments": heat_treatments, "surface_processes": surface_processes, "tests": tests, "extra_sources": extra_sources,
            "rejected_thread_notes": rejected_thread_notes, "datum_references": datum_refs, "section_count": section_count,
            "machining_datums": machining_datums, "post_machined": post_machined,
            "machined_surface_estimate": machined_surface_estimate, "slot_candidate": slot_candidate}


def _number_or_none(value: str | None) -> float | None:
    return float(value) if value else None


# 保留上方的兼容解析器；以下包装层为标题栏、结构化孔表和工程备注提供更可靠的数据源。
_base_analyze_drawing = analyze_drawing
_base_extract_fields = extract_fields


def _title_block_fields(text: str, filename: str = "") -> dict:
    """从标题栏文本识别中英文公司、图名和图号，并保留来源/置信度。"""
    result = _base_extract_fields(text, filename)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    # OCR 段只用于中文标题栏内容；矢量文字仍是型号、图号的首选来源。
    ocr_lines = []
    if "[[OCR_TITLE_TEXT]]" in text:
        ocr_lines = [re.sub(r"\s+", " ", line).strip()
                     for line in text.split("[[OCR_TITLE_TEXT]]", 1)[1].splitlines() if line.strip()]
    company_lines = ocr_lines or lines
    chinese_companies = []
    english_companies = []
    pollution = re.compile(r"\b(?:RGB|SURFACE\s*FINISH|HEAT[ -]?TREAT|MATERIAL|QTY\.?|DATE|DRAWN|CHECKED|DESIGNED|APPROVED)\b|制图|审核|设计|日期", re.I)
    for line in company_lines:
        # A title-block row may visually sit beside surface finish/date cells;
        # OCR sometimes reads the whole row as one line.  Such a mixed line is
        # not a company name and must be discarded instead of saved as client.
        if pollution.search(line):
            continue
        # 含替换字符的 CAD 字体乱码不应作为公司名；OCR 成功后会提供可读中文行。
        if "�" not in line and re.search(r"(?:公司|科技有限公司|有限责任公司|股份有限公司)", line) and len(line) <= 80:
            chinese_companies.append(line)
        if re.search(r"\b(?:COMPANY|CO\.?|LIMITED|INC\.?|LTD\.?)\b", line, re.I) and len(line) <= 100:
            english_companies.append(line)
    if chinese_companies:
        result["company_name"] = max(chinese_companies, key=len)
        result["company_name_source"] = "PDF标题栏/公司行"; result["company_name_confidence"] = "高"
    if english_companies:
        result["english_company_name"] = max(english_companies, key=len)
    # 图号通常比图名多一个或多个以连字符分隔的版本段；优先寻找具有前缀关系的两段型号。
    codes = []
    for token in re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,}\b", text.upper()):
        if token not in codes and not token.startswith(("GB-", "ISO-", "GW-")):
            codes.append(token)
    parent_child = [(longer, shorter) for longer in codes for shorter in codes
                    if longer != shorter and longer.startswith(shorter + "-")]
    if parent_child:
        drawing_number, product_name = max(parent_child, key=lambda item: (item[0].count("-"), len(item[0])))
        result.update({"drawing_number": drawing_number, "product_number": drawing_number, "product_name": product_name,
                       # “Parts No.” 单元格为空时不能把图名复制进去，避免客户误以为已识别到零件号。
                       "part_number": "", "identification_source": "PDF右下角标题栏型号组合", "identification_confidence": "高"})
    else:
        result.setdefault("identification_source", "文件名备用")
        result.setdefault("identification_confidence", "需要人工确认")
    # 多数客户图纸并没有单独的“客户名称”字段，标题栏公司就是本次报价的客户。
    # 有明确 CUSTOMER/客户 字段时保持其优先级，不覆盖。
    if not str(result.get("customer", "")).strip():
        customer_candidate = result.get("company_name") or result.get("english_company_name")
        if str(customer_candidate or "").strip():
            result["customer"] = customer_candidate
            result["customer_source"] = result.get("company_name_source", "PDF标题栏")
            result["customer_confidence"] = result.get("company_name_confidence", "中")
    # This small evidence block is rendered by the app/export and gives the
    # engineer an auditable title-block source without mixing field contents.
    result["title_block_evidence"] = {
        "source": "OCR标题栏" if ocr_lines else "PDF标题栏文本",
        "confidence": result.get("identification_confidence", "需要人工确认"),
        "excerpt": " | ".join(company_lines[:12]),
    }
    return result


def extract_fields(text: str, filename: str = "") -> dict:
    return _title_block_fields(text, filename)


def _parse_hole_table(text: str) -> list[dict]:
    """解析“标签 / 大小或加工要求 / 数量”式多行孔表，数量继承给该标签全部加工步骤。"""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    start = next((i for i, line in enumerate(lines) if "标签" in line and "数量" in line), None)
    if start is None:
        return []
    table_lines = lines[start + 1:start + 100]
    rows, current_label, bucket = [], None, []
    label_re = re.compile(r"^(AA|AC|[A-Z])(?:\s+|$)(.*)$")
    def flush() -> None:
        if not current_label or not bucket:
            return
        source = " ".join(bucket)
        thread = re.search(r"M\s*(\d+(?:\.\d+)?)\s*[-×x]?\s*(6[Hh]|[Hh]\d)?", source)
        # 数量是该标签最后一条工艺说明的末尾整数；排除90°、孔径和深度。
        terminal = re.findall(r"(?<![.\d])(\d+)(?![.\d])", bucket[-1])
        quantity = int(terminal[-1]) if terminal else 0
        if quantity <= 0 or quantity > 10000:
            return
        # 螺纹标称直径（例如 M5）不是底孔直径；先移除 M 规格再取首个数值。
        numeric_source = re.sub(r"M\s*\d+(?:\.\d+)?\s*[-×x]?\s*(?:6[Hh]|[Hh]\d)?", "", source)
        numbers = re.findall(r"\d+(?:\.\d+)?", numeric_source)
        base_diameter = None
        if numbers:
            candidates = [float(value) for value in numbers if 0.5 <= float(value) <= 200]
            if candidates:
                # 螺纹行往往先列螺纹深度再列底孔直径；以常用底孔约为公称直径 0.8 倍
                # 选择最接近值，避免把“深10”当成 M5 的 Ø10 底孔。
                candidate = (min(candidates, key=lambda value: abs(value - float(thread.group(1)) * 0.82))
                             if thread else candidates[0])
                base_diameter = candidate
        countersink = re.search(r"(\d+(?:\.\d+)?)\s*[×xX]\s*90°", source)
        row = {"label": current_label, "quantity": quantity, "base_diameter": base_diameter,
               "thread_spec": (f"M{thread.group(1)}" + (f"-{thread.group(2).upper()}" if thread.group(2) else "")) if thread else None,
               "thread_diameter": float(thread.group(1)) if thread else None,
               "countersink_diameter": float(countersink.group(1)) if countersink else None,
               "through": "贯穿" in source or "THRU" in source.upper(), "source_text": source,
               "source": "PDF孔特征表", "confidence": "高"}
        # 视图索引中的“N 1”“P 1”并不是孔表记录。孤立数字没有孔径/螺纹/沉头
        # 说明时不自动计入报价，避免把图框坐标误识别成 Ø1 孔。
        has_process_detail = row["thread_spec"] or row["countersink_diameter"] or len(numbers) >= 2
        if has_process_detail and (row["base_diameter"] or row["thread_spec"] or row["countersink_diameter"]):
            rows.append(row)
    for line in table_lines:
        # 孔表结束后经常紧跟视图索引（只有一个字母）。AC 这类标签也可能独占一行，
        # 所以仅在已有完整记录且上一行只是数量时结束。
        if (current_label and line not in {"AA", "AC"} and re.fullmatch(r"[A-Z]{1,2}", line)
                and bucket and re.fullmatch(r"\d+", bucket[-1].strip())):
            flush()
            current_label, bucket = None, []
            break
        match = label_re.match(line)
        if match:
            flush(); current_label, bucket = match.group(1), [match.group(2)]
        elif current_label:
            # 标题栏开始后结束，避免把整张图的编号继续拼到最后一个孔标签。
            if any(marker in line.upper() for marker in ["TITLE", "DWG NO", "TECHNICAL", "图纸号", "技术要求"]):
                break
            bucket.append(line)
    flush()
    return rows


def _independent_thread_annotations(text: str) -> list[dict]:
    """识别孔表外由尺寸引线给出数量的螺纹，例如上一行“2×Ø17.5”后的 M20-6H。

    只有紧邻的数量引线才会加入，孤立 M20 仍保留为待人工确认，避免把说明编号当孔。
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    result = []
    thread_re = re.compile(r"\bM\s*(\d+(?:\.\d+)?)\s*(?:[-×x]\s*(\d[HhGg]))?", re.I)
    label_re = re.compile(r"^(?:AA|AC|[A-Z])(?:\s+|$)")
    for index, line in enumerate(lines):
        match = thread_re.search(line)
        if not match or label_re.match(line):
            continue
        previous = lines[index - 1] if index else ""
        count_match = re.search(r"\b(\d+)\s*[×xX]\s*(?:[ØΦφ]?\s*\d)", previous)
        if not count_match:
            continue
        count = int(count_match.group(1))
        spec = f"M{match.group(1)}" + (f"-{match.group(2).upper()}" if match.group(2) else "")
        result.append({"specification": spec, "nominal_diameter": float(match.group(1)), "count": count,
                       "source_text": previous + " / " + line, "confidence": "中", "label": "独立引线",
                       "source": "PDF尺寸引线"})
    return result


def analyze_drawing(text: str) -> dict:
    result = _base_analyze_drawing(text)
    table_rows = _parse_hole_table(text)
    if table_rows:
        threads, holes = [], []
        for row in table_rows:
            quantity = row["quantity"]
            if row["thread_spec"]:
                threads.append({"specification": row["thread_spec"], "nominal_diameter": row["thread_diameter"],
                                "pitch": None, "tolerance_class": "6H" if "-6H" in row["thread_spec"] else None,
                                "count": quantity, "depth": None, "through": False, "source_text": row["source_text"],
                                "confidence": "高", "label": row["label"], "source": row["source"]})
            if row["base_diameter"]:
                holes.append({"diameter": row["base_diameter"], "count": quantity, "depth": None,
                              "through": row["through"], "countersink": False, "counterbore": False, "reamed": False,
                              "tolerance": None, "source_text": row["source_text"], "confidence": "高",
                              "label": row["label"], "source": row["source"]})
            if row["countersink_diameter"]:
                holes.append({"diameter": row["countersink_diameter"], "count": quantity, "depth": None,
                              "through": False, "countersink": True, "counterbore": False, "reamed": False,
                              "tolerance": None, "source_text": row["source_text"], "confidence": "高",
                              "label": row["label"], "source": row["source"]})
        # 孔表是主要来源；孔表外、带明确数量引线的螺纹（如侧面 M20）补入，
        # 不把主视图的 A/B/C 标签重复累加。
        for feature in _independent_thread_annotations(text):
            threads.append({"specification": feature["specification"], "nominal_diameter": feature["nominal_diameter"],
                            "pitch": None, "tolerance_class": "6H" if "-6H" in feature["specification"] else None,
                            "count": feature["count"], "depth": None, "through": False, "source_text": feature["source_text"],
                            "confidence": feature["confidence"], "label": feature["label"], "source": feature["source"]})
        result["thread_features"] = threads
        result["hole_features"] = holes
        result["threaded_count"] = sum(item["count"] for item in threads)
        result["drilled_count"] = sum(item["count"] for item in holes)
        result["thread_diameters"] = [item["nominal_diameter"] for item in threads]
        result["thread_groups"] = [{"规格": item["specification"], "数量": item["count"], "直径": item["nominal_diameter"],
                                    "标签": item["label"], "数量来源": item["source"], "置信度": item["confidence"]} for item in threads]
    normalized = result.get("normalized_text", text)
    explicit_grinding = bool(re.search(r"精磨|磨削|磨床|研磨", normalized, re.I))
    tolerance_signals = [0.01] if "精磨" in normalized else []
    result["explicit_grinding"] = result.get("explicit_grinding", False) or explicit_grinding
    result["grinding_required"] = result["explicit_grinding"] or any(value <= 0.02 for value in result.get("geometric_values", []) + tolerance_signals)
    result["grinding_reason"] = "图纸明确要求精磨" if "精磨" in normalized else "未检测到强制磨削文字"
    result["hole_table_rows"] = table_rows
    return result


# Some suppliers use a coordinate table instead of a conventional
# "label/specification/quantity" table.  Each physical hole is one table row
# (A1, A2, ...), while the specification cell is often merged vertically.  A
# plain full-text regex sees the M3/M4 text but loses the number of labelled
# rows, which is why the former version quoted every hole as one piece.
_analyze_before_coordinate_table = analyze_drawing


def _coordinate_hole_table(text: str) -> list[dict]:
    """Read generic ``label / X / Y / specification`` coordinate tables.

    The algorithm deliberately uses label sequence and a vertically inherited
    specification; it does not know, or depend on, any drawing number.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in normalize_engineering_text(text).splitlines() if line.strip()]
    # Some CAD PDF fonts extract the X heading as multiplication sign, so the
    # stable header signal is "标签 + 位置 + Y", rather than a literal X.
    starts = [i for i, line in enumerate(lines)
              if "标签" in line and "位置" in line and "Y" in line.upper()]
    if not starts:
        return []

    label_re = re.compile(r"^(?P<prefix>AA|AB|AC|[A-Z])(?P<index>\d+)\s+(?P<x>-?\d+(?:\.\d+)?)\s+(?P<y>-?\d+(?:\.\d+)?)(?P<tail>.*)$", re.I)
    records: list[dict] = []
    active: dict[str, dict] = {}
    current: dict | None = None

    def spec_from(parts: list[str]) -> dict:
        body = " ".join(parts)
        thread = re.search(r"\bM\s*(\d+(?:\.\d+)?)(?:\s*[×x]\s*(\d+(?:\.\d+)?))?\s*-?\s*(6[Hh])?", body)
        # Coordinates are not dimensions.  Only the text below/alongside the
        # label is allowed to become a bottom-hole diameter.
        dims = re.findall(r"(?:Ø|\u2205)\s*(\d+(?:\.\d+)?)|(?<![A-Z0-9.])(\d+(?:\.\d+)?)(?=\s+(?:\d+(?:\.\d+)?\s*)?(?:M\s*\d|$))", body, re.I)
        numbers = [float(a or b) for a, b in dims if (a or b)]
        before_thread = body[:thread.start()] if thread else body
        plain = [float(v) for v in re.findall(r"(?<![A-Z0-9.])(\d+(?:\.\d+)?)", before_thread)]
        base = None
        if thread:
            nominal = float(thread.group(1))
            candidates = [v for v in (numbers or plain) if 0.5 <= v < nominal]
            if candidates:
                base = min(candidates, key=lambda v: abs(v - nominal * 0.82))
        elif numbers:
            base = numbers[0]
        countersink = re.search(r"(?:Ø|\u2205)?\s*(\d+(?:\.\d+)?)\s*[×x]\s*90", body, re.I)
        return {
            "thread_spec": (f"M{thread.group(1)}" + (f"×{thread.group(2)}" if thread and thread.group(2) else "")
                            + ("-6H" if thread and thread.group(3) else "")) if thread else None,
            "thread_diameter": float(thread.group(1)) if thread else None,
            "base_diameter": base,
            "countersink_diameter": float(countersink.group(1)) if countersink else None,
            "source_text": body,
        }

    def flush() -> None:
        nonlocal current
        if not current:
            return
        parsed = spec_from(current.pop("parts"))
        prefix = current["label_prefix"]
        if parsed["thread_spec"] or parsed["base_diameter"] or parsed["countersink_diameter"]:
            active[prefix] = parsed
        else:
            parsed = active.get(prefix, parsed)
        records.append({**current, **parsed, "quantity": 1,
                        "source": "PDF坐标孔表", "confidence": "高"})
        current = None

    for start in starts:
        # Stop at the next title/notes block.  600 lines is enough for a dense
        # coordinate table and avoids accidentally reading a later sheet.
        for line in lines[start + 1:start + 600]:
            if current and "标签" in line and "位置" in line and "Y" in line.upper():
                # Multi-column tables repeat the header.  Close the current
                # record and let the next header start parse the next column.
                flush()
                break
            match = label_re.match(line)
            if match:
                flush()
                current = {"label": f"{match.group('prefix').upper()}{match.group('index')}",
                           "label_prefix": match.group("prefix").upper(),
                           "x": float(match.group("x")), "y": float(match.group("y")),
                           "parts": [match.group("tail").strip()]}
                continue
            if current:
                # A new drawing/table title marks the end; ordinary dimension
                # lines belong to the currently open table record.
                if re.search(r"\b(?:TITLE|DWG\s*NO|SHEET)\b|技术要求|MATERIAL\s*SPEC", line, re.I):
                    flush()
                    break
                current["parts"].append(line)
        flush()
    return records


def _explicit_view_thread_groups(text: str) -> list[dict]:
    """Count explicit first-page annotations, but never coordinate labels."""
    engineering = normalize_engineering_text(text)
    table_at = engineering.find("标签")
    view_text = engineering if table_at < 0 else engineering[:table_at]
    lines = [line.strip() for line in view_text.splitlines() if line.strip()]
    groups: list[dict] = []
    previous = ""
    for line in lines:
        thread = re.search(r"\bM\s*(\d+(?:\.\d+)?)(?:\s*[×x]\s*(\d+(?:\.\d+)?))?\s*-?\s*(6[Hh])?", line, re.I)
        count_match = re.search(r"(?<![A-Z0-9.])(\d+)\s*[×x]\s*(?:(?:Ø|\u2205)?\s*\d|M)", line, re.I)
        previous_count = re.search(r"(?<![A-Z0-9.])(\d+)\s*[×x]\s*(?:(?:Ø|\u2205)?\s*\d|M)", previous, re.I)
        if thread and (count_match or previous_count):
            count = int((count_match or previous_count).group(1))
            groups.append({"specification": f"M{thread.group(1)}" + (f"×{thread.group(2)}" if thread.group(2) else "") + ("-6H" if thread.group(3) else ""),
                           "nominal_diameter": float(thread.group(1)), "count": count,
                           "source": "PDF视图直接标注", "confidence": "中", "label": "视图引线"})
        previous = line
    return groups


def analyze_drawing(text: str) -> dict:
    result = _analyze_before_coordinate_table(text)
    engineering = normalize_engineering_text(text)
    # Canonical surface-treatment names are kept separate from drawing wording,
    # so "black zinc plating" / "black zinc" and paint wording can preselect
    # the correct multiple-choice pricing items without hard-coding a drawing.
    treatment_signals = {
        "喷砂": ["喷砂", "喷丸"], "喷漆": ["喷漆"], "烤漆": ["烤漆", "烘烤漆"],
        "喷粉": ["喷粉"], "黑漆": ["黑漆"], "镀黑锌": ["镀黑锌", "黑锌", "黑色镀锌"],
        "氧化": ["氧化", "阳极氧化"], "电泳": ["电泳"], "磷化": ["磷化"],
    }
    result["surface_processes"] = sorted(set(result.get("surface_processes", [])) | {
        name for name, words in treatment_signals.items() if any(word in engineering for word in words)
    })
    coordinate_rows = _coordinate_hole_table(text)
    if not coordinate_rows:
        return result

    thread_buckets: dict[str, dict] = {}
    hole_buckets: dict[tuple[float, bool], dict] = {}
    for row in coordinate_rows:
        if row.get("thread_spec"):
            key = row["thread_spec"]
            bucket = thread_buckets.setdefault(key, {"specification": key, "nominal_diameter": row["thread_diameter"],
                                                     "count": 0, "labels": [], "source": "PDF坐标孔表", "confidence": "高"})
            bucket["count"] += 1; bucket["labels"].append(row["label"])
        if row.get("base_diameter"):
            key = (round(float(row["base_diameter"]), 3), False)
            bucket = hole_buckets.setdefault(key, {"diameter": key[0], "count": 0, "depth": None, "through": False,
                                                    "countersink": False, "counterbore": False, "reamed": False,
                                                    "source": "PDF坐标孔表", "confidence": "高", "labels": []})
            bucket["count"] += 1; bucket["labels"].append(row["label"])
        if row.get("countersink_diameter"):
            key = (round(float(row["countersink_diameter"]), 3), True)
            bucket = hole_buckets.setdefault(key, {"diameter": key[0], "count": 0, "depth": None, "through": False,
                                                    "countersink": True, "counterbore": False, "reamed": False,
                                                    "source": "PDF坐标孔表", "confidence": "高", "labels": []})
            bucket["count"] += 1; bucket["labels"].append(row["label"])
    # First-page numeric call-outs represent additional directions/features;
    # they are deliberately added once, while A/B labels are never re-counted.
    for group in _explicit_view_thread_groups(text):
        key = group["specification"]
        existed = key in thread_buckets
        bucket = thread_buckets.setdefault(key, {**group, "labels": []})
        if existed:
            bucket["count"] += group["count"]
            bucket["source"] = "PDF坐标孔表＋视图直接标注"
            bucket["confidence"] = "高"

    threads = list(thread_buckets.values())
    holes = list(hole_buckets.values())
    result.update({
        "coordinate_hole_details": coordinate_rows,
        "hole_table_rows": coordinate_rows,
        "thread_features": threads,
        "hole_features": holes,
        "threaded_count": sum(item["count"] for item in threads),
        "drilled_count": sum(item["count"] for item in holes),
        "thread_diameters": [item["nominal_diameter"] for item in threads],
        "thread_groups": [{"规格": item["specification"], "数量": item["count"], "直径": item["nominal_diameter"],
                           "标签": "、".join(item.get("labels", [])[:12]), "数量来源": item["source"],
                           "置信度": item["confidence"]} for item in threads],
        "two_sided_required": bool(re.search(r"两面加工|两侧加工|侧面加工|对面加工", normalize_engineering_text(text))),
    })
    return result
