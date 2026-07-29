"""报价参数、设备能力与旧版 pricing_config.json 兼容迁移。"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "pricing_config.json"

DEFAULT_CONFIG = {
    "company_name": "机械加工厂",
    "profit_multiplier": 1.20,
    "materials": {"灰铁": 6.0, "球铁": 7.0, "铸铝": 36.0},
    "casting_sales_prices": {"灰铁": 7.5, "球铁": 8.5, "铸铝": 0.0},
    "densities": {"灰铁": 7.4, "球铁": 7.6, "铸铝": 2.70},
    "casting_blank_factors": {"灰铁": 1.20, "球铁": 1.20, "铸铝": 1.12},
    "machine_rates": {"CNC加工中心": 90.0, "车床": 65.0, "龙门铣": 200.0, "卧式加工中心": 175.0, "磨床": 90.0},
    "direct_machine_rates": {"CNC加工中心": 90.0, "车床": 65.0, "龙门铣": 200.0, "卧式加工中心": 175.0, "磨床": 90.0},
    "manual_labor_rate": 35.0,
    "surface_treatments": {
        "喷砂": {"rate": 2.0, "basis": "按kg", "minimum": 0.0},
        "喷漆": {"rate": 2.0, "basis": "按kg", "minimum": 0.0},
        "喷粉": {"rate": 3.0, "basis": "按kg", "minimum": 0.0},
        "黑漆": {"rate": 0.5, "basis": "按kg", "minimum": 0.0},
        "氧化": {"rate": 7.0, "basis": "按kg", "minimum": 0.0},
        "电泳": {"rate": 10.0, "basis": "按kg", "minimum": 0.0},
        "磷化": {"rate": 4.0, "basis": "按kg", "minimum": 0.0},
    },
    "machine_capabilities": {
        "CNC加工中心": {"x": 800, "y": 500, "z": 500, "table_x": 900, "table_y": 500, "max_weight": 500, "side_head": False, "five_axis": False},
        "卧式加工中心": {"x": 1000, "y": 800, "z": 800, "table_x": 630, "table_y": 630, "max_weight": 800, "side_head": False, "five_axis": False},
        "龙门铣": {"x": 2500, "y": 1200, "z": 900, "table_x": 2600, "table_y": 1200, "max_weight": 3000, "side_head": True, "five_axis": True},
        "磨床": {"x": 1000, "y": 500, "z": 400, "table_x": 1000, "table_y": 500, "max_weight": 500, "side_head": False, "five_axis": False},
    },
}


def _merge(default: dict, loaded: dict) -> dict:
    result = deepcopy(default)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return deepcopy(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    # 兼容旧版：表面处理曾为“名称: 元/kg”的扁平结构。
    if isinstance(loaded.get("surface_treatments"), dict):
        old_surfaces = loaded["surface_treatments"]
        if old_surfaces and isinstance(next(iter(old_surfaces.values())), (float, int)):
            loaded["surface_treatments"] = {name: {"rate": rate, "basis": "按kg", "minimum": 0.0} for name, rate in old_surfaces.items()}
    config = _merge(DEFAULT_CONFIG, loaded)
    # 用户之前保存的旧默认密度自动迁移；其他手工维护值不覆盖。
    if config["densities"].get("灰铁") == 7.2:
        config["densities"]["灰铁"] = 7.4
    if config["densities"].get("球铁") in {7.1, 7.29}:
        config["densities"]["球铁"] = 7.6
    save_config(config)
    return config


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
