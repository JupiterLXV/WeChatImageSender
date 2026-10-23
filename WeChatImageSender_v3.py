#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entry point. Keep alongside main.py and wechat_sender/.
For standalone export, run: python scripts/export_single_file.py
"""
from main import main

if __name__ == '__main__':
    raise SystemExit(main())
