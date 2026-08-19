# -*- coding: utf-8 -*-
"""
ストリーム応答の表示を担当するモジュール（役をまたいで共通）。
点字スピナー(Spinner)と、Ollamaのストリーミング応答を読みながら
思考(thinking)/本文(content)を表示する stream_chat_response() を提供する。

[NOTE] 以前は arc_agent.py にだけ実装があり、chat_agent.py は別の簡易版を
       個別に持っていた（スピナーなし・ラベル固定）。役の「定義」はrole.json/
       prompt.txtでひな型化したが、この表示処理はひな型化されておらず実装が
       分岐していたため、ここに共通化した。
"""

import sys
import time
import json
import threading
import urllib.request

from scripts.config import IGNORE_FIELDS, THINKING_FIELDS, SPINNER_FRAMES
from scripts.tools import strip_think_blocks


class Spinner:
    """別スレッドで点字スピナーを回し、状態ラベル＋受信トークン数を表示する。1秒で1周。"""
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._token_count = 0
        self._label = "受信中"
        self._active = False

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
            n = self._token_count
            label = self._label
            sys.stdout.write(f"\r{frame} {label}... {n} tokens      ")
            sys.stdout.flush()
            i += 1
            time.sleep(0.1)

    def start(self, label="受信中"):
        self._stop.clear()
        self._token_count = 0
        self._label = label
        self._active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_label(self, label):
        self._label = label

    def add_token(self, n=1):
        self._token_count += n

    def get_count(self):
        return self._token_count

    def stop(self, clear_line=False):
        if not self._active:
            return self._token_count
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._active = False
        final = self._token_count
        if clear_line:
            sys.stdout.write("\r" + " " * 50 + "\r")
            sys.stdout.flush()
        else:
            sys.stdout.write(f"\r[DONE] 受信完了 {final} tokens              \n")
            sys.stdout.flush()
        return final


def stream_chat_response(req, debug_mode: bool = False, raw_dump: bool = False):
    """通常/ログ: スピナー(点字/1秒1周)＋思考(thinking)と本文(content)のトークンを数える。
                 思考中は『思考中』、本文中は『出力中』とラベルを切り替える。
       raw_dump=True: role等のノイズを無視し、フィールドが変わったらヘッダを出して都度出力。"""
    ai_response_full = ""
    tool_calls_full = []

    # ---------- 生ダンプモード ----------
    if raw_dump:
        chunk_index = 0
        current_field = None
        field_char_counts = {}

        with urllib.request.urlopen(req) as res:
            for line in res:
                if not line:
                    continue
                chunk = json.loads(line.decode("utf-8"))
                msg_obj = chunk.get("message", {})
                chunk_index += 1

                tc = msg_obj.get("tool_calls")
                if tc:
                    tool_calls_full.extend(tc)

                for field_name, value in msg_obj.items():
                    if field_name in IGNORE_FIELDS:
                        continue
                    if not isinstance(value, str) or value == "":
                        continue

                    if field_name != current_field:
                        if current_field is not None:
                            print()
                        print(f"\n------ [{field_name}] ------")
                        current_field = field_name

                    print(value, end="", flush=True)

                    field_char_counts[field_name] = field_char_counts.get(field_name, 0) + len(value)
                    if field_name == "content":
                        ai_response_full += value

        print()
        print("========== DUMP SUMMARY ==========")
        print(f"[SUMMARY] 総チャンク数: {chunk_index}")
        if field_char_counts:
            for fname, cnt in field_char_counts.items():
                print(f"[SUMMARY] フィールド '{fname}' の総文字数: {cnt}")
        else:
            print("[SUMMARY] 中身のあるフィールドはありませんでした。")
        print("==================================")
        return ai_response_full, tool_calls_full

    # ---------- 通常 / ログモード ----------
    spinner = Spinner()
    spinner.start(label="思考中")

    content_started = False

    try:
        with urllib.request.urlopen(req) as res:
            for line in res:
                if not line:
                    continue
                chunk = json.loads(line.decode("utf-8"))
                msg_obj = chunk.get("message", {})

                tc = msg_obj.get("tool_calls")
                if tc:
                    tool_calls_full.extend(tc)

                content = msg_obj.get("content", "")
                thinking = ""
                for k, v in msg_obj.items():
                    if k in IGNORE_FIELDS:
                        continue
                    if k in THINKING_FIELDS and isinstance(v, str):
                        thinking += v

                if thinking:
                    spinner.add_token(1)
                    if not content_started:
                        spinner.set_label("思考中")

                if content:
                    ai_response_full += content
                    spinner.add_token(1)
                    if not content_started:
                        content_started = True
                        spinner.set_label("出力中")

                if not content and not thinking:
                    continue
    finally:
        spinner.stop(clear_line=False)

    visible = strip_think_blocks(ai_response_full)
    if debug_mode:
        print(visible if visible else ai_response_full)
    else:
        if visible:
            print(visible)

    return ai_response_full, tool_calls_full
