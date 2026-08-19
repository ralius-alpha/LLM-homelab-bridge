# -*- coding: utf-8 -*-
"""
セッションログと、全役共通の「忘れてはいけない事項」置き場。

[NOTE] 受け取った側が渡す側を強制終了させる設計のため、渡す側は
       「後で自分でログを保存する」猶予が無い場合がある。そのため
       セッションログはターンごとに逐次追記する(start_session_log +
       append_session_log)。最後にまとめて書こうとしない。
"""

import os
from datetime import datetime

from scripts.config import LOGS_DIRNAME, SHARED_MEMORY_FILENAME


def start_session_log(base_dir, role):
    """新しいセッションログファイルを作り、パスを返す。以降はappend_session_logで追記する。"""
    logs_dir = os.path.join(base_dir, LOGS_DIRNAME)
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(logs_dir, f"{role}_{timestamp}.log")
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"=== Session Log - Role: {role} - Started: {timestamp} ===\n\n")
    return path


def append_session_log(log_path, label, text):
    """
    1ターン分をログファイルに追記する。書き込み失敗は握りつぶす（ログのためにセッションを止めない）。
    [NOTE] パイプ経由の入力等で不正なサロゲート文字が紛れることがあるため、
           encode不能な文字は置換して書き込む（ログを丸ごと失わないため）。
    """
    if not text:
        return
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{label}]\n{text}\n\n")
    except Exception as e:
        print(f"[WARN] セッションログの書き込みに失敗しました: {e}")


def append_role_transition(log_path, kind, from_role, to_role, detail=""):
    """
    役の引き継ぎ/復帰を、共通のログに記録する。
    [NOTE] 引き継ぎ先も同じlog_pathに書き込むことで、1つのファイルを読めば
           役をまたいだ会話の続きが分かるようにしている（役ごとに別ファイルだと、
           前の役に戻った時に何が起きたかを追うのにファイルを跨がなければならない）。
    """
    label = "引き継ぎ" if kind == "handoff" else "復帰"
    text = f"=== {label}: {from_role} → {to_role} ==="
    if detail:
        text += f"\n{detail}"
    append_session_log(log_path, "System", text)


def _shared_memory_path(base_dir):
    return os.path.join(base_dir, SHARED_MEMORY_FILENAME)


def load_shared_memory(base_dir):
    """shared_memory.mdの中身を返す。無ければ空文字列。"""
    path = _shared_memory_path(base_dir)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def append_shared_memory(base_dir, role, note):
    """全役共通の『忘れてはいけない事項』に1件追記する。"""
    path = _shared_memory_path(base_dir)
    is_new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", errors="replace") as f:
        if is_new:
            f.write("# 共有メモ（全役共通・忘れてはいけない事項）\n\n")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"## {timestamp} ({role})\n{note}\n\n")


def build_system_prompt_with_memory(base_prompt, base_dir):
    """システムプロンプトに、共有メモがあればそれを付加する。"""
    shared = load_shared_memory(base_dir)
    if not shared:
        return base_prompt
    return (
        f"{base_prompt}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "【共有メモ：他の役から引き継がれた、忘れてはいけない事項】\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{shared}"
    )
