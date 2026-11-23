# -*- coding: utf-8 -*-
"""临时脚本：再创建/挂载 2 个测试 VHD（x3wtest2/3.vhd）。"""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "vhd3.txt"

with open(OUT, "w", newline="\r\n", encoding="ascii") as f:
    for name in ("x3wtest2", "x3wtest3"):
        vhd = rf"C:\Users\admin\AppData\Local\Temp\{name}.vhd"
        f.write(f'create vdisk file="{vhd}" maximum=64 type=fixed\n')
        f.write(f'select vdisk file="{vhd}"\n')
        f.write("attach vdisk\n")
print("vhd3.txt written")
