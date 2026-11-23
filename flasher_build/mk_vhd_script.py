# -*- coding: utf-8 -*-
"""临时脚本：生成 diskpart 脚本（CRLF + ASCII 路径）创建/挂载 64MB 测试 VHD。"""
from pathlib import Path

VHD = r"C:\Users\admin\AppData\Local\Temp\x3wtest.vhd"
OUT = Path(__file__).resolve().parent / "vhd.txt"

with open(OUT, "w", newline="\r\n", encoding="ascii") as f:
    f.write(f'create vdisk file="{VHD}" maximum=64 type=fixed\n')
    f.write(f'select vdisk file="{VHD}"\n')
    f.write("attach vdisk\n")
print("vhd.txt written")
