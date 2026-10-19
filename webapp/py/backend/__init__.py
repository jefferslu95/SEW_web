# -*- coding: utf-8 -*-
"""SEW_web backend package."""
import os

# ★ 单线程(BLAS/OpenMP)：必须在 import numpy/scipy（OpenBLAS 首次初始化）之前
#   【直接赋值】（不是 setdefault）—— OpenBLAS 只在此刻读线程数，之后再改 env
#   无效（见 DGIS/cube.ipynb 与 CCC/ssy_muse/fit.ipynb 的 import 格）。
#   放在包的 __init__ ⇒ 任何 `from backend import ...` 都先执行本文件，
#   早于 backend/app.py 里的 `import numpy`。
#   ppxf 每次迭代都是小矩阵乘，多线程收益为负、还会与并行任务抢核；
#   将来做数据立方的批量拟合时，并行放在任务层（按 CPU 内核数开多进程），
#   每个进程内部仍保持单线程。
for _k, _v in {"OMP_NUM_THREADS": "1",
               "OPENBLAS64_NUM_THREADS": "1",
               "OPENBLAS_NUM_THREADS": "1",
               "MKL_NUM_THREADS": "1",
               "VECLIB_MAXIMUM_THREADS": "1",
               "NUMEXPR_NUM_THREADS": "1"}.items():
    os.environ[_k] = _v
