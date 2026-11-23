# -*- coding: utf-8 -*-
"""临时脚本：卸载 3 个测试 VHD。"""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "vhd_detach.txt"

with open(OUT, "w", newline="\r\n", encoding="ascii") as f:
    for name in ("x3wtest", "x3wtest2", "x3wtest3"):
        vhd = rf"C:\Users\admin\AppData\Local\Temp\{name}.vhd"
        f.write(f'select vdisk file="{vhd}"\n')
        f.write("detach vdisk\n")
print("vhd_detach.txt written")
