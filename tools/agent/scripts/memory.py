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
from scripts.tools import strip_think_blocks, strip_tool_call_json


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


# コード側がモデルを促すために差し込む内部指示の目印。
# この目印で始まるメッセージは、引き継ぎ先に渡す抜粋には含めない。
SYSTEM_NOTICE_PREFIX = "[SYSTEM NOTICE]"

# 引き継ぎ指示書(build_handoff_brief)の各セクションの見出し。
# 指示書を抜粋に載せる時は、最後の「作業指示」だけに畳む（_flatten_handoff_brief）。
BRIEF_CAPABILITY_HEADER = "[あなたが使えるtool]"
BRIEF_ROOT_REQUEST_HEADER = "[ユーザーの当初の依頼（原文）]"
BRIEF_EXCERPT_HEADER = "[直前までの会話の抜粋（参考。要約ではなく実際のやり取り）]"
BRIEF_INSTRUCTION_HEADER = "[今回の具体的な作業指示]"


def _flatten_handoff_brief(text):
    """
    引き継ぎ指示書を、その中の「今回の具体的な作業指示」だけに畳む。

    [IMPORTANT] 引き継ぎ先の役のmessagesは、先頭のuserメッセージが
    「指示書まるごと」になっている。それをそのまま次の引き継ぎの抜粋に
    載せると、指示書の中に前の指示書が入り、その中にまた前の指示書が…と
    再帰的に入れ子になる（実機で5重の入れ子を確認）。
    指示書のうち後続の役にとって意味があるのは「何をしてほしいか」だけなので、
    最後の作業指示セクション以降だけを残す。
    """
    if not text or BRIEF_INSTRUCTION_HEADER not in text:
        return text
    return text.rsplit(BRIEF_INSTRUCTION_HEADER, 1)[1].strip()


def build_handoff_brief(target_tool_names, root_request, context, instructions):
    """
    引き継ぎ先に渡す指示書を組み立てる。chat_agent.py と arc_agent.py の
    両方がこれを使う（以前は各々が同じ文字列連結を持っていた）。

    [IMPORTANT] 冒頭に「引き継ぎ先自身のtool一覧」を明記するのが要点。
    実機で、search_webを持っている review→execute→debug の3役が揃って
    「私は直接search_webを呼び出す能力を持っていません」と言ってたらい回しに
    する暴走を確認した。原因は、抜粋に含まれる【前の役の「できません」という
    発言】を、受け取った役が自分の発言として真似てしまうこと（tool呼び出しJSONの
    丸写しと同じ構造の、文章版）。自分の能力を先に突きつけ、抜粋中の他役の
    自己申告は自分には当てはまらないと明示することで打ち消す。

    root_request（ユーザーの当初の依頼の原文）も併せて渡す。引き継ぎのたびに
    instructionsがモデルの言い換えで書き換わり、元の依頼から乖離していく
    （「今日の東京の天気」→「最新の天気情報」→「適切なAPIを呼び出して」）
    のを防ぐため、原文だけは書き換えずに持ち回る。
    """
    parts = []
    if target_tool_names:
        parts.append(
            f"{BRIEF_CAPABILITY_HEADER}\n"
            f"{', '.join(target_tool_names)}\n"
            "※この一覧があなたの能力そのもの。下の抜粋に出てくる他の役の"
            "「〜できません」「〜する能力を持っていません」といった発言は、"
            "その役についての話であって、あなたには当てはまらない。"
            "真似して同じことを言わず、必ず自分のtool一覧を見て判断すること。"
            "この一覧に必要なtoolがあるなら、引き継がずに自分で実行すること。"
        )
    if root_request:
        parts.append(
            f"{BRIEF_ROOT_REQUEST_HEADER}\n{root_request}\n"
            "※これが最終的に満たすべき依頼。途中の役が言い換えた指示より、"
            "この原文を優先して解釈すること。"
        )
    if context:
        parts.append(f"{BRIEF_EXCERPT_HEADER}\n{context}")
    parts.append(f"{BRIEF_INSTRUCTION_HEADER}\n{instructions}")
    return "\n\n".join(parts)


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

    [IMPORTANT] 生のtool呼び出しJSONは strip_tool_call_json() で落とす。
    残したままにすると、引き継ぎ先の役がそれを丸写しして同じtool呼び出しを
    繰り返す（実機で execute→execute の自己引き継ぎ暴走を確認）。

    [IMPORTANT] SYSTEM NOTICE（コード側がモデルを促すために差し込む内部指示）と、
    過去の引き継ぎ指示書そのものは、抜粋に含めない/畳む。含めたままにすると、
    引き継ぎのたびに指示書が入れ子で積み重なり（実機で
    「[直前までの会話の抜粋]」が5重に入れ子になるのを確認）、文脈が
    指数的に汚れる。詳細は _flatten_handoff_brief を参照。
    """
    turns = [
        m for m in messages
        if m.get("role") in ("user", "assistant", "tool") and m.get("content")
    ]
    recent = turns[-limit:]
    lines = []
    for m in recent:
        raw = m["content"]
        # コード側が差し込んだ内部指示は、他の役に見せる情報ではない。
        if raw.lstrip().startswith(SYSTEM_NOTICE_PREFIX):
            continue
        text = _flatten_handoff_brief(raw)
        text = strip_tool_call_json(strip_think_blocks(text))
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
