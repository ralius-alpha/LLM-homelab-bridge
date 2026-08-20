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
from scripts.tools import strip_think_blocks


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


def render_recent_turns(messages, limit=6):
    """
    直近の発言を、引き継ぎ先に渡す会話の参考情報として整形する。
    [NOTE] 元は chat_agent.py にしかなかったが、arc_agent.py も他の役へ
    引き継げるようになったため（役同士が対等に呼び合える構造）、共通化した。
    以前は instructions（呼び出し元モデルが作った要約）だけを渡しており、
    実際の会話そのものは引き継ぎ先から一切見えなかった。要約は捏造や
    抜け漏れが起こりうるため、実データである直近の会話も併せて渡す。
    role=="tool" のメッセージ（他の役からの報告。例えばWriter役が書いた
    記事本文そのもの）を除外すると、2段階の引き継ぎで前の役の成果物が
    次の役に渡らない不具合になる（実機で確認済み）ため、tool役のメッセージも含める。
    """
    turns = [
        m for m in messages
        if m.get("role") in ("user", "assistant", "tool") and m.get("content")
    ]
    recent = turns[-limit:]
    lines = []
    for m in recent:
        text = strip_think_blocks(m["content"])
        if not text:
            continue
        if m["role"] == "user":
            lines.append(f"ユーザー: {text}")
        elif m["role"] == "assistant":
            lines.append(f"アシスタント: {text}")
        else:
            # tool役のcontentは "[xxx役からの報告]\n..." の形で既に自己説明的なため、
            # 話者ラベルを付けずそのまま載せる。
            lines.append(text)
    return "\n".join(lines)


def build_call_chain_notice(chain, own_role_id):
    """
    呼び出し連鎖（誰が誰を呼んで今の自分に至ったか）の表示ブロックを組み立てる。
    トップレベル起動（chainが空。単体起動や、まだ誰も何も引き継いでいない
    最初の役）の時はNoneを返す（何も注入しない）。
    役同士が対等に呼び合える構造では無限ループの危険があるため、各役に
    「自分が何代目か」「これまでどう呼ばれてきたか」を見せ、プロンプト側の
    判断でループを避けさせる（コード側のMAX_CALL_DEPTHはこれとは別の、
    プロンプトが守られなかった場合のハード制限）。
    """
    if not chain:
        return None
    full_chain = list(chain) + [own_role_id]
    depth = len(full_chain)
    arrow = " → ".join(full_chain)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "【呼び出し履歴（ループ防止のため必ず確認すること）】\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow}\n"
        f"あなたはこの中の「{own_role_id}」で、{depth}代目にあたる。\n"
        "この履歴に同じ役がすでに出てきている場合、同じ依頼をまた引き継ぐと"
        "堂々巡りになる可能性が高い。心当たりがあれば、これ以上handoff_to_role"
        "を使わず、return_to_callerでここまでの状況を報告すること。"
    )
