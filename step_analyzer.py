"""STEP 实体重量、平面面积与回转特征提取。失败时返回不确定而非伪造数据。"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path


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
        planar_areas, cylinder_records = [], []
        face_count = cylinder_count = spline_count = 0
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            surface = BRepAdaptor_Surface(face, True)
            kind = surface.GetType(); face_count += 1
            if kind == GeomAbs_Plane:
                props = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, props)
                planar_areas.append(max(0.0, props.Mass()))
            elif kind == GeomAbs_Cylinder:
                cylinder_count += 1
                try:
                    cylinder = surface.Cylinder()
                    axis = cylinder.Axis(); direction = axis.Direction(); location = axis.Location()
                    props = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, props)
                    cylinder_records.append({"radius": float(cylinder.Radius()), "area": float(props.Mass()),
                                             "direction": (round(abs(direction.X()), 2), round(abs(direction.Y()), 2), round(abs(direction.Z()), 2)),
                                             "origin": (round(location.X(), -1), round(location.Y(), -1), round(location.Z(), -1))})
                except Exception:
                    pass
            elif kind == GeomAbs_BSplineSurface:
                spline_count += 1
            explorer.Next()
        # 相同方向、接近同一中心的圆柱面是车床强信号；只统计面积大于阈值的组。
        groups: dict[tuple, dict] = {}
        for record in cylinder_records:
            key = (record["direction"], record["origin"])
            group = groups.setdefault(key, {"area": 0.0, "diameters": [], "count": 0})
            group["area"] += record["area"]; group["diameters"].append(round(record["radius"]*2, 1)); group["count"] += 1
        cylinder_groups = sorted(groups.values(), key=lambda group: group["area"], reverse=True)
        largest_plane = max(planar_areas, default=0.0)
        total_plane = sum(planar_areas)
        return {"available": True, "dimensions": dimensions, "volume_mm3": volume_mm3, "net_weight": net_weight,
                "blank_weight": blank_weight, "blank_factor": blank_factor, "face_count": face_count,
                "cylinder_count": cylinder_count, "spline_count": spline_count, "largest_planar_area_m2": largest_plane/1_000_000,
                "total_planar_area_m2": total_plane/1_000_000, "cylinder_groups": cylinder_groups[:8],
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
