# -*- coding: utf-8 -*-
"""临时脚本：给测试 VHD 分配盘符 X:（diskpart 需要 CRLF+ANSI）。"""
from pathlib import Path

VHD = r"C:\Users\admin\AppData\Local\Temp\x3wtest.vhd"
OUT = Path(__file__).resolve().parent / "vhd2.txt"

with open(OUT, "w", newline="\r\n", encoding="ascii") as f:
    f.write(f'select vdisk file="{VHD}"\n')
    f.write("attach vdisk\n")
    # 按 disk 号选择（枚举出的 VHD 是 PhysicalDrive3 → disk 3）
    f.write("select disk 3\n")
    f.write("create partition primary\n")
    f.write("assign letter=X\n")
print("vhd2.txt written")
