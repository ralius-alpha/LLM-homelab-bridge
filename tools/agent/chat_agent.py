# -*- coding: utf-8 -*-
"""
雑談役エージェント。プログラムの入口（ユーザーが起動するのはこのスクリプト）。

ファイル操作・コマンド実行はできない、会話専用のフロントエンド。
会話だけでは対応できない要望を検知したら、handoff_to_roleで適切な役
（roles/<id>/role.json の "module" が指す実装）を scripts.dispatch.invoke_role
経由で呼び出す。呼び出された役が作業を終えたら（return_to_callerを呼ぶか、
単に終了すれば）、その呼び出しがreturnして自分の会話ループにそのまま戻ってくる。

[設計方針] 「役」はモデルと初期プロンプトの組み合わせに過ぎず、役の切り替えは
プロセスを跨いだ引き継ぎではなく、ただの関数呼び出し（入れ子構造）でよい。
  - GPU(VRAM)が1本しかない制約は、呼び出す前に自分のモデルをアンロードし、
    呼び出し先が終わったら自分のモデルを再ロードする、という同期的な
    関数呼び出しの中で自然に満たされる（同時に2つロードされることがない）。
  - 起動失敗时の心配も無い。別プロセスを新規に起動するわけではないので、
    「起動したはずが失敗する」という事故そのものが起こり得ない。
  - Ollamaサーバー自体はプログラム全体で1回だけ起動し、役の切り替えのたびに
    落として立て直したりしない（モデルの load/unload だけを行う）。
  - 呼び出し先の特定は roles/<id>/role.json の "module" を見て動的にimportする
    （scripts.dispatch.invoke_role）ため、新しい役を追加してもこのファイルは
    変更不要。呼び出せる役の一覧は roles/chat/role.json の "can_handoff_to" が
    決める。
"""

import os
import sys
import json
import urllib.request
import urllib.error

from scripts.config import OLLAMA_HOST, KEEP_ALIVE
from scripts.ollama import (
    setup_environment,
    cleanup_processes,
    unload_all_models,
    startup_cleanup,
    run_server,
    warmup_model,
)
from scripts.tools import (
    strip_think_blocks,
    tool_call_from_content,
    tool_calls_to_actions,
    handoff_from_tool_calls,
)
from scripts.display import stream_chat_response
from scripts.role_loader import load_role, roles_providing_tool
from scripts.dispatch import invoke_role
from scripts.memory import (
    start_session_log,
    append_session_log,
    append_role_transition,
    append_shared_memory,
    build_system_prompt_with_memory,
    render_recent_turns,
)

# 思考モデルは、日本語Windowsのコンソール既定(cp932)では表示できない文字
# （中国語文字等）を時々出す。素のprintだと即クラッシュするため、置換表示に倒す。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# [NOTE] stdoutだけreconfigureしてstdinを忘れると、標準入力がリダイレクト/パイプ
# 経由（対話的なコンソールでない）の時に、Pythonがコンソールの既定コードページ
# (cp932)でinput()をdecodeしてしまい、UTF-8で書かれた日本語の入力が丸ごと
# 文字化けする（エラーにはならず、無言で化けた文字列がそのままモデルに渡る）。
# [IMPORTANT] reconfigure()はストリームから1度でも読み込んだ後には呼べない
# （RuntimeErrorになる）。この呼び出しはinput()より確実に前（モジュール読み込み時）
# に実行されるので通常は問題無いが、念のためtry/exceptで囲む。
if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (RuntimeError, ValueError):
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLE_ID = "chat"
ROLE = load_role(BASE_DIR, ROLE_ID)
CHAT_MODEL = ROLE["model"]

# [NOTE] 以前はここにnum_ctxが無く、Ollamaの既定値（多くの場合2048〜4096）に
# 依存していた。雑談役のシステムプロンプトは役が増えるたびに大きくなって
# おり（6役分のtool定義＋自己認識ブロック込みで数千トークン規模）、既定値では
# 会話が始まる前から黙って切り詰められていた可能性がある（エラーにならない
# ため気づきにくい）。arc_agent.py側は元からnum_ctxを明示していたが、
# chat_agent.py側は抜けていたため揃える。
CHAT_NUM_CTX = 8192

# [IMPORTANT] 以前はここが 0.6 だった（雑談の自然さを狙って高めにしていた）。
# しかし実機テストで、雑談役だけが「Review役に引き継ぐことができます。手順を
# 教えていただきますか？」のような【会話文】を返すだけで、実際の
# handoff_to_role のtool呼び出しを出さない不具合を繰り返し確認した。
# arc_agent.py側の6役はすべて temperature 0.2 で、同じモデル
# (qwen2.5-coder:14b)にもかかわらずtool呼び出しは安定して出せている。
# tool呼び出しは構造化されたJSONの生成であり、温度が高いほど崩れやすい。
# 失敗しているのが「0.6の役だけ」という切り分けができているため、
# 動いている側(0.2)に揃える。repeat_penaltyも同じ理由で揃える。
CHAT_TEMPERATURE = 0.2
CHAT_REPEAT_PENALTY = 1.15

# 「引き継ぎます」と言いながらtool呼び出しを出さなかった時や、
# 自分が持っていないtoolを直接呼ぼうとした時に、促して作り直させる回数の上限。
# 無限に促すと会話が進まなくなるため1回だけ。
MAX_HANDOFF_NUDGES = 1

# 雑談役がこのファイルの会話ループで実際に処理できるtool。
# [IMPORTANT] これ以外のtool呼び出しが来たら、それは雑談役が持っていない
# toolを名前だけ借りて呼ぼうとしている（実機で summarize_text を直接
# 呼ぼうとするのを確認）。以前は黙って捨てていたため、ユーザーには生の
# JSONだけが表示され、依頼は果たされないまま終わっていた。
SERVICEABLE_TOOL_NAMES = {"remember", "handoff_to_role"}


def _called_tool_names(tool_calls):
    """tool_callsから呼び出されたtool名の一覧を取り出す。"""
    names = []
    for tc in tool_calls or []:
        name = (tc.get("function") or {}).get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


# 「自分ではできない/他の役に引き継ぐ」という【意図】が本文に出ているかを見る
# パターン。tool呼び出しが1つも無い返答に対してのみ使う（下記
# _looks_like_unfulfilled_handoff 参照）。
_HANDOFF_INTENT_PATTERNS = (
    "引き継",
    "引継",
    "役に頼",
    "役に依頼",
    "役にお願い",
    "役を呼",
    "私は直接",
    "私には直接",
    "自分では",
    "自分には",
    "検索してみてください",
    "調べてみてください",
    "確認してみてください",
)


def _looks_like_unfulfilled_handoff(text: str) -> bool:
    """
    「他の役に引き継ぐ」「自分では出来ない」と会話文で述べているのに、
    実際のtool呼び出しが1つも無い返答かどうかを判定する。

    [NOTE] プロンプト側で「必ずtool呼び出し機能そのものを使うこと」と
    何度強調しても、小型モデルでは確率的にしか守られないことを今夜
    繰り返し確認している（roles/*/prompt.txt の【絶対ルール】、
    role_loader._inject_skill_notes のいずれも、単独では取りこぼす）。
    そのため「言ったのにやっていない」状態をコード側で検知して、
    1回だけ促し直す保険をかける。これはこのプロジェクトで一貫して
    採ってきた「プロンプトでの軽減＋コードでの保険」の方針に沿う
    （arc_agent.py の MAX_AUTO_STEPS / MAX_FAILURE_STREAK 等と同じ考え方）。
    """
    if not text:
        return False
    return any(pattern in text for pattern in _HANDOFF_INTENT_PATTERNS)


def run_role_and_wait(server_proc, role_id, instructions, log_path):
    """
    role_idの役を invoke_role 経由で入れ子呼び出しし、終わるまで待つ。
    戻ってきたら自分(雑談役)のモデルを再ロードする。
    呼び出し先が例外で落ちても、ここで捕まえて会話は継続させる。
    戻り値: 呼び出し先からの報告（要約文字列）。無ければNone。

    [NOTE] call_chain=["chat"] を渡す。雑談役は呼び出し連鎖の常に起点(0番目)
    であり、他の役から呼び返される対象にはならない（moduleを持たないため
    invoke_roleの呼び出し先にもなれない）。役同士が対等に呼び合える構造に
    なったため、この最初の1段だけは雑談役側でマークしておく必要がある。
    """
    try:
        summary = invoke_role(BASE_DIR, role_id, server_proc, instructions, log_path, call_chain=["chat"])
    except Exception as e:
        print(f"\n[ERROR] {role_id}役の実行中に問題が発生しました: {e}")
        summary = f"{role_id}役の実行中にエラーが発生し、中断しました: {e}"
    finally:
        warmup_model(CHAT_MODEL)
    return summary


def run_chat_loop(model_name, server_proc, log_path):
    messages = [{"role": "system", "content": build_system_prompt_with_memory(ROLE["prompt"], BASE_DIR)}]

    print(f"\n[雑談役] {model_name} で起動しました。何でも話しかけてください。")
    print("※ 終了: 'exit'/'quit' / 送信: 新しい行で 'EOF' か Ctrl+Z/Ctrl+D\n")

    # [IMPORTANT] 想定外の例外でここから抜けた場合でも、VRAM解放とプロセス掃除だけは
    # 必ず行う。既知の終了経路はそれぞれ自分で後片付けしてからreturnするので、
    # cleaned_upフラグで二重に走らないようにする。
    cleaned_up = False

    def teardown():
        unload_all_models()
        try:
            server_proc.terminate()
            server_proc.wait(timeout=5)
        except Exception:
            pass
        cleanup_processes()

    try:
        while True:
            try:
                print("\n[Input] ---------------------------------")
                lines = []
                while True:
                    line = input(">>> " if not lines else "... ")
                    if not lines and line.strip().lower() in ("exit", "quit"):
                        cleaned_up = True
                        teardown()
                        return
                    if line.strip().upper() == "EOF":
                        break
                    lines.append(line)
            except (EOFError, KeyboardInterrupt):
                print("\n[INFO] 終了します。")
                cleaned_up = True
                teardown()
                return

            user_input = "\n".join(lines).strip()
            if not user_input:
                continue

            messages.append({"role": "user", "content": user_input})
            append_session_log(log_path, "User", user_input)

            # [NOTE] 「引き継ぐと言ったのにtool呼び出しが無い」場合に1回だけ
            # 促し直すため、モデル呼び出しをループにしてある（通常は1周で抜ける）。
            nudges_used = 0
            while True:
                payload = json.dumps({
                    "model": model_name,
                    "messages": messages,
                    "tools": ROLE["tools"],
                    "stream": True,
                    "keep_alive": KEEP_ALIVE,
                    "options": {
                        "temperature": CHAT_TEMPERATURE,
                        "top_p": 0.9,
                        "repeat_penalty": CHAT_REPEAT_PENALTY,
                        "num_ctx": CHAT_NUM_CTX,
                    },
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{OLLAMA_HOST}/api/chat", data=payload,
                    headers={"Content-Type": "application/json"})

                print("\n--- Response ---")
                try:
                    content, tool_calls = stream_chat_response(req)
                except urllib.error.URLError as e:
                    print(f"\n[ERROR] 通信エラー: {e}")
                    cleaned_up = True
                    teardown()
                    return

                assistant_msg = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)
                append_session_log(log_path, "Assistant", strip_think_blocks(content) or "(tool呼び出しのみ)")

                effective_tool_calls = tool_calls or tool_call_from_content(content)

                # --- 物理防御: 依頼が無言で取りこぼされる2種類のパターンを検知し、
                # 1回だけ促して作り直させる ---
                called_names = _called_tool_names(effective_tool_calls)
                unavailable = [n for n in called_names if n not in SERVICEABLE_TOOL_NAMES]

                # 何か1つでもこのループで処理できるtoolがあれば、通常処理へ進む。
                if any(n in SERVICEABLE_TOOL_NAMES for n in called_names):
                    break
                if nudges_used >= MAX_HANDOFF_NUDGES:
                    break

                if unavailable:
                    # パターン1: 自分が持っていないtoolを名前だけ借りて直接呼んだ。
                    owners = []
                    for name in unavailable:
                        owners.extend(roles_providing_tool(
                            BASE_DIR, ROLE.get("can_handoff_to", []), name))
                    owner_hint = (
                        f"（{'・'.join(sorted(set(owners)))}役が持っています）"
                        if owners else ""
                    )
                    nudge = (
                        f"[SYSTEM NOTICE] あなたは {', '.join(unavailable)} を直接"
                        f"呼び出そうとしましたが、それはあなたが使えるtoolでは"
                        f"ありません{owner_hint}。そのため今の呼び出しは実行されず、"
                        "ユーザーの依頼は果たされていません。あなたが使えるのは "
                        "handoff_to_role と remember だけです。そのtoolを持つ役に"
                        "handoff_to_role で引き継ぐか、会話だけで答えられる内容なら"
                        "tool を使わず普通の会話文で答えてください。"
                    )
                    print(f"\n[SYSTEM] 使えないtool({', '.join(unavailable)})を呼ぼうとしました。促し直します。")
                    append_session_log(
                        log_path, "System",
                        f"使用不可のtool呼び出しを検知({', '.join(unavailable)})のため促し直し")
                elif _looks_like_unfulfilled_handoff(strip_think_blocks(content)):
                    # パターン2: 「引き継ぎます」と会話文で述べたのにtool呼び出しが無い。
                    nudge = (
                        "[SYSTEM NOTICE] あなたは今の返答で「他の役に引き継ぐ」または"
                        "「自分では対応できない」と述べましたが、実際のtool呼び出しが"
                        "1つも行われていません。会話文でそう書くだけでは何も実行されず、"
                        "ユーザーの依頼は果たされないままです。"
                        "ユーザーに手順を尋ね返したり、ユーザー自身にやらせる案内をしたり"
                        "せず、今すぐtool呼び出し機能そのものを使って handoff_to_role を"
                        "呼び出してください（引き継ぎ先・具体的な作業指示・理由を引数に"
                        "入れること）。"
                    )
                    print("\n[SYSTEM] 引き継ぐと述べましたが実際のtool呼び出しがありません。促し直します。")
                    append_session_log(log_path, "System", "引き継ぎ意図はあるがtool呼び出しが無いため促し直し")
                else:
                    break

                nudges_used += 1
                messages.append({"role": "user", "content": nudge})

            # [IMPORTANT] handoff_to_roleをremember等より先にチェックしていると、
            # 1回の返答でhandoff_to_role + rememberがまとめて出た時、handoffだけが
            # 処理されてrememberが無言で捨てられる（arc_agent.py側で実機確認した
            # のと同じ系統の不具合）。remember等の処理を先に行い、handoffは最後に
            # チェックする。
            remembered = False
            for act in tool_calls_to_actions(effective_tool_calls):
                if act["type"] == "remember":
                    append_shared_memory(BASE_DIR, "chat", act["note"])
                    print(f"\n[REMEMBER] 共有メモに書き残しました: {act['note']}")
                    append_session_log(log_path, "System", f"共有メモに追記: {act['note']}")
                    remembered = True

            handoff = handoff_from_tool_calls(effective_tool_calls)
            if handoff and handoff["role_id"] not in ROLE.get("can_handoff_to", []):
                # [IMPORTANT] tool定義のenumで制限していても、モデルはそれを無視した
                # role_idを書いてくる（arc_agent.py側で自己引き継ぎの暴走を実機確認）。
                # 存在しない/許可されていない役をdispatchしようとすると
                # importlibのImportErrorになるだけなので、手前で弾いて説明を返す。
                bad = handoff["role_id"]
                allowed = ROLE.get("can_handoff_to", [])
                print(f"\n[SYSTEM] 無効な引き継ぎ先({bad})が指定されました。")
                append_session_log(log_path, "System", f"無効な引き継ぎ先を拒否: {bad}")
                messages.append({"role": "user", "content": (
                    f"[SYSTEM NOTICE] {bad}役はあなたの引き継ぎ先ではないため、"
                    f"引き継ぎは行われませんでした。引き継げるのは次の役だけです: "
                    f"{', '.join(allowed) or '(なし)'}。適切な役を選び直すか、"
                    "会話だけで答えられる内容なら普通の会話文で答えてください。"
                )})
                print("[雑談役] （引き継ぎ先の指定が正しくありませんでした。もう一度話しかけてください）")
                continue

            if handoff:
                role_id = handoff["role_id"]
                print(f"\n[HANDOFF] {role_id}役を呼び出します。理由: {handoff.get('reason') or '(不明)'}")
                append_role_transition(
                    log_path, "handoff", "chat", role_id,
                    f"理由: {handoff.get('reason') or '(不明)'}\n指示: {handoff['instructions']}",
                )

                context = render_recent_turns(messages)
                full_instructions = (
                    f"[直前までの会話の抜粋（参考。要約ではなく実際のやり取り）]\n{context}\n\n"
                    f"[今回の具体的な作業指示]\n{handoff['instructions']}"
                ) if context else handoff["instructions"]

                summary = run_role_and_wait(server_proc, role_id, full_instructions, log_path)

                append_role_transition(log_path, "return", role_id, "chat", summary or "(報告なし)")
                if summary:
                    note = f"[{role_id}役からの報告]\n{summary}"
                    messages.append({"role": "tool", "content": note})
                    print(f"\n{note}\n")
                continue

            # モデルがtoolだけ呼んで会話文を何も返さなかった場合、ユーザーには何も
            # 表示されず固まったように見える。必ず何かは表示する。
            if not strip_think_blocks(content):
                if remembered:
                    print("[雑談役] （メモしました。続けてどうぞ）")
                else:
                    print("[雑談役] （うまく言葉にできませんでした。もう一度話しかけてみてください）")
    finally:
        if not cleaned_up:
            print("\n[WARN] 想定外の形でセッションが終了しました。後片付け（VRAM解放）だけは行います。")
            try:
                teardown()
            except Exception as cleanup_err:
                print(f"[ERROR] 後片付けにも失敗しました: {cleanup_err}")


def main():
    setup_environment()
    startup_cleanup()

    log_file = os.path.join(BASE_DIR, "chat_server.log")
    server_proc = run_server(log_file)
    warmup_model(CHAT_MODEL)
    log_path = start_session_log(BASE_DIR, "chat")

    run_chat_loop(CHAT_MODEL, server_proc, log_path)


if __name__ == "__main__":
    main()
