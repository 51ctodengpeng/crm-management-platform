# -*- coding: utf-8 -*-
"""一次性脚本：把模板里的原生 confirm() 提示改为 data-confirm 属性（走自定义弹窗）

执行后模板中不再出现 onsubmit="return confirm(...)"，统一由 static/js/dialog.js 处理。
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
PATTERN = re.compile(r"""\s*onsubmit="return confirm\('(.*?)'\);" """.strip(), re.S)
ALT = re.compile(r"""onsubmit="return confirm\('(.*?)'\);" """.strip(), re.S)


def convert(text: str) -> tuple[str, int]:
    count = 0

    def repl(match):
        nonlocal count
        count += 1
        message = match.group(1).replace("\\n", " ")
        return f'data-confirm="{message}"'

    return ALT.sub(repl, text), count


def main():
    total = 0
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        new, n = convert(text)
        if n:
            path.write_text(new, encoding="utf-8")
            total += n
            print(f"  {path.relative_to(TEMPLATES)}: {n} 处")
    print(f"[OK] 共替换 {total} 处原生 confirm")


if __name__ == "__main__":
    main()
