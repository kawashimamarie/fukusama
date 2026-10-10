#!/usr/bin/env python3
"""プレビュー用の擬似 sendmail。PHP の mail() から受け取ったメールを送信せず preview/outbox/ に保存する。"""
import sys
import time
from pathlib import Path

outbox = Path(__file__).resolve().parent / "outbox"
outbox.mkdir(exist_ok=True)
data = sys.stdin.buffer.read()
(outbox / time.strftime("mail-%Y%m%d-%H%M%S.eml")).write_bytes(data)
