# -*- coding: utf-8 -*-
"""生成省 / 市 / 区三级联动的离线数据 static/data/regions.json

数据来源：china-division 包内置的行政区划字典（六位行政区划代码 -> 名称）。
本脚本只在需要更新行政区划时手工执行一次，运行时不依赖该包：

    python tools/build_regions.py [path/to/data.py]

输出结构（前端 region-picker.js 使用）：
    [{"name": "广东省", "children": [{"name": "深圳市", "children": ["福田区", ...]}]}]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "data" / "regions.json"

# 直辖市与特别行政区没有地级市一层，补一个同名市级便于三级联动统一
DIRECT = {"110000", "120000", "310000", "500000", "710000", "810000", "820000"}


def load_data(src: Path) -> dict:
    namespace: dict = {}
    exec(compile(src.read_text(encoding="utf-8"), str(src), "exec"), namespace)
    return namespace["DIVISION_DATA"]


def build(data: dict) -> list:
    provinces: dict[str, dict] = {}
    for code, name in data.items():
        if len(code) != 6 or not code.isdigit():
            continue
        if code.endswith("0000"):
            provinces[code] = {"name": name, "children": {}}

    valid_codes = set(provinces)
    for code, name in data.items():
        if len(code) != 6 or not code.isdigit() or code.endswith("0000"):
            continue
        p_code = code[:2] + "0000"
        if p_code not in valid_codes:
            continue
        province = provinces[p_code]
        if code.endswith("00"):
            # 直辖市的「市辖区 / 县」替换为省名，避免出现「市辖区」这种层级
            if p_code in DIRECT and name in ("市辖区", "县"):
                name = province["name"]
            province["children"].setdefault(code, {"name": name, "children": []})
        else:
            c_code = code[:4] + "00"
            city = province["children"].get(c_code)
            if city is None:
                # 数据源省略了「市辖区 / 省直辖县级行政区划」这一层：
                # 直辖市用省名作为市级；省直辖县级市直接提升为市级（无区县）
                if p_code in DIRECT:
                    province["children"][c_code] = {"name": province["name"], "children": [name]}
                else:
                    province["children"][c_code] = {"name": name, "children": []}
                continue
            city["children"].append(name)

    result = []
    for p_code in sorted(provinces):
        province = provinces[p_code]
        cities = []
        for c_code in sorted(province["children"]):
            city = province["children"][c_code]
            cities.append({"name": city["name"], "children": city["children"]})
        # 无下级（台湾 / 港澳）时补一个同名市级，保证界面结构一致
        if not cities:
            cities = [{"name": province["name"], "children": []}]
        result.append({"name": province["name"], "children": cities})
    return result


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if src is None:
        raise SystemExit("请传入 china_division/data.py 路径（pip download china-division 后解压获取）")
    tree = build(load_data(src))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(tree, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    districts = sum(len(c["children"]) for p in tree for c in p["children"])
    print(f"[OK] {len(tree)} 个省 / 直辖市 -> {OUT}（区县 {districts} 个，{OUT.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
