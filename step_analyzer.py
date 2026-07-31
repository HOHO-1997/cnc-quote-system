"""STEP 实体重量、平面面积与回转特征提取。失败时返回不确定而非伪造数据。"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(item * item for item in vector))
    return tuple(item / length for item in vector) if length else (0.0, 0.0, 1.0)


def _canonical_axis(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    """同一根轴允许方向相反，统一成一个方向用于比较。"""
    axis = _unit(vector)
    for value in axis:
        if abs(value) > 1e-8:
            return axis if value > 0 else tuple(-item for item in axis)
    return axis


def _axis_distance(first: dict, second: dict) -> float:
    """平行轴线最短垂距；轴线上起点不同不影响同轴判断。"""
    direction = _canonical_axis(tuple(first["direction"]))
    delta = tuple(second["origin"][i] - first["origin"][i] for i in range(3))
    projection = sum(delta[i] * direction[i] for i in range(3))
    perpendicular = tuple(delta[i] - projection * direction[i] for i in range(3))
    return math.sqrt(sum(item * item for item in perpendicular))


def group_coaxial_cylinders(records: list[dict], angle_degrees: float = 2.0, distance_mm: float = 1.0) -> list[dict]:
    """按轴线夹角和最短垂距分组，避免要求圆柱面原点完全相等。"""
    groups: list[dict] = []
    cosine_limit = math.cos(math.radians(angle_degrees))
    for source in records:
        record = {**source, "direction": _canonical_axis(tuple(source["direction"]))}
        target = None
        for group in groups:
            reference = group["records"][0]
            dot = abs(sum(record["direction"][i] * reference["direction"][i] for i in range(3)))
            if dot >= cosine_limit and _axis_distance(record, reference) <= distance_mm:
                target = group; break
        if target is None:
            target = {"records": [], "area": 0.0, "diameters": [], "count": 0,
                      "axis_direction": record["direction"], "axis_distance_tolerance_mm": distance_mm}
            groups.append(target)
        target["records"].append(record); target["area"] += float(record.get("area", 0.0))
        target["diameters"].append(round(float(record.get("radius", 0.0)) * 2, 2)); target["count"] += 1
    for group in groups:
        group["diameters"] = sorted(set(group["diameters"]), reverse=True); group.pop("records", None)
    return sorted(groups, key=lambda item: item["area"], reverse=True)


def group_planar_directions(records: list[dict]) -> list[dict]:
    """按平面法向归类；同一刀轴方向可优先在同次装夹完成。"""
    groups: dict[tuple[float, float, float], dict] = {}
    for record in records:
        axis = _canonical_axis(tuple(record["direction"]))
        key = tuple(round(abs(item), 1) for item in axis)
        group = groups.setdefault(key, {"direction": axis, "area": 0.0, "count": 0})
        group["area"] += float(record.get("area", 0.0)); group["count"] += 1
    return sorted(groups.values(), key=lambda item: item["area"], reverse=True)


def analyze_step(file_bytes: bytes, material: str, config: dict) -> dict:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepGProp import BRepGProp
        from OCP.Bnd import Bnd_Box
        from OCP.GProp import GProp_GProps
        from OCP.GeomAbs import GeomAbs_BSplineSurface, GeomAbs_Cylinder, GeomAbs_Plane
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS
    except Exception as error:
        return {"available": False, "message": f"STEP 几何引擎不可用：{error}"}
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as handle:
            handle.write(file_bytes)
            temp_name = handle.name
        reader = STEPControl_Reader()
        if reader.ReadFile(temp_name) != IFSelect_RetDone:
            return {"available": False, "message": "STEP 文件无法读取或不包含可转换实体。"}
        reader.TransferRoots()
        shape = reader.OneShape()
        box = Bnd_Box(); BRepBndLib.Add_s(shape, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        dimensions = [max(0.0, xmax-xmin), max(0.0, ymax-ymin), max(0.0, zmax-zmin)]
        volume_props = GProp_GProps(); BRepGProp.VolumeProperties_s(shape, volume_props)
        volume_mm3 = max(0.0, volume_props.Mass())
        if volume_mm3 <= 0:
            return {"available": False, "message": "未检测到封闭实体，无法计算净重。"}
        density = float(config["densities"].get(material, 7.4))
        net_weight = volume_mm3 * density / 1_000_000
        blank_factor = float(config["casting_blank_factors"].get(material, 1.2))
        blank_weight = net_weight * blank_factor
        planar_areas, planar_records, cylinder_records = [], [], []
        face_count = cylinder_count = spline_count = 0
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            surface = BRepAdaptor_Surface(face, True)
            kind = surface.GetType(); face_count += 1
            if kind == GeomAbs_Plane:
                props = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, props)
                area = max(0.0, props.Mass()); planar_areas.append(area)
                try:
                    direction = surface.Plane().Axis().Direction()
                    planar_records.append({"area": area, "direction": (float(direction.X()), float(direction.Y()), float(direction.Z()))})
                except Exception:
                    pass
            elif kind == GeomAbs_Cylinder:
                cylinder_count += 1
                try:
                    cylinder = surface.Cylinder()
                    axis = cylinder.Axis(); direction = axis.Direction(); location = axis.Location()
                    props = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, props)
                    cylinder_records.append({"radius": float(cylinder.Radius()), "area": float(props.Mass()),
                                             "direction": (float(direction.X()), float(direction.Y()), float(direction.Z())),
                                             "origin": (float(location.X()), float(location.Y()), float(location.Z()))})
                except Exception:
                    pass
            elif kind == GeomAbs_BSplineSurface:
                spline_count += 1
            explorer.Next()
        # 相同方向、接近同一中心的圆柱面是车床强信号；只统计面积大于阈值的组。
        cylinder_groups = group_coaxial_cylinders(cylinder_records)
        planar_direction_groups = group_planar_directions(planar_records)
        largest_plane = max(planar_areas, default=0.0)
        total_plane = sum(planar_areas)
        return {"available": True, "dimensions": dimensions, "volume_mm3": volume_mm3, "net_weight": net_weight,
                "blank_weight": blank_weight, "blank_factor": blank_factor, "face_count": face_count,
                "cylinder_count": cylinder_count, "spline_count": spline_count, "largest_planar_area_m2": largest_plane/1_000_000,
                "total_planar_area_m2": total_plane/1_000_000, "cylinder_groups": cylinder_groups[:8],
                "planar_direction_groups": planar_direction_groups[:8],
                "message": "STEP 实体分析完成。"}
    except Exception as error:
        return {"available": False, "message": f"STEP 几何分析失败：{error}"}
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def turning_geometry_confidence(step: dict) -> tuple[float, str]:
    """只将大面积同轴圆柱组视为回转证据，局部孔壁不会单独触发车床。"""
    groups = step.get("cylinder_groups", [])
    if not groups:
        return 0.0, "STEP 未提取到可用的同轴圆柱组"
    largest = groups[0]
    total_area = sum(item.get("area", 0) for item in groups)
    ratio = largest.get("area", 0) / total_area if total_area else 0
    diameters = largest.get("diameters", [])
    if ratio >= 0.55 and largest.get("count", 0) >= 3:
        return 0.75, f"最大同轴圆柱组占圆柱面积 {ratio:.0%}，含直径 {diameters[:4]}"
    return 0.25, f"圆柱特征分散（最大同轴组仅占 {ratio:.0%}），不应仅凭孔壁推荐车床"
