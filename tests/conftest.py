"""测试会话共享：所有用例使用同一个独立临时库。"""
from __future__ import annotations

import os
import tempfile

# 必须在导入 app 之前设置，database.py 在导入时读取该环境变量。
if "MATRIX_DB_PATH" not in os.environ:
    _tmp = tempfile.TemporaryDirectory()
    os.environ["MATRIX_DB_PATH"] = os.path.join(_tmp.name, "test.db")
