#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 static/i18n/zh.json 与 en.json 的 key 完全一致（CI 用）。

    python scripts/check_i18n.py
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZH = os.path.join(BASE, "static", "i18n", "zh.json")
EN = os.path.join(BASE, "static", "i18n", "en.json")


def main() -> int:
    zh = json.load(open(ZH, encoding="utf-8"))
    en = json.load(open(EN, encoding="utf-8"))
    missing = sorted(set(zh) - set(en))
    extra = sorted(set(en) - set(zh))
    if missing or extra:
        print("missing in en:", missing)
        print("extra in en:", extra)
        return 1
    print(f"i18n ok: {len(zh)} keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
