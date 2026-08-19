# -*- coding: utf-8 -*-
"""
雑談役エージェント。プログラムの入口（ユーザーが起動するのはこのスクリプト）。

ファイル操作・コマンド実行はできない、会話専用のフロントエンド。
会話だけでは対応できない要望を検知したら、handoff_to_executeでExecute役
(arc_agent.start_interactive_chat)を直接呼び出す。Execute役が作業を終えたら
（return_to_chatを呼ぶか、単に終了すれば）、その呼び出しがreturnして
自分の会話ループにそのまま戻ってくる。

[設計方針] 「役」はモデルと初期プロンプトの組み合わせに過ぎず、役の切り替えは
プロセスを跨いだ引き継ぎではなく、ただの関数呼び出し（入れ子構造）でよい。
  - GPU(VRAM)が1本しかない制約は、呼び出す前に自分のモデルをアンロードし、
    呼び出し先が終わったら自分のモデルを再ロードする、という同期的な
    関数呼び出しの中で自然に満たされる（同時に2つロードされることがない）。
  - 起動失敗时の心配も無い。別プロセスを新規に起動するわけではないので、
    「起動したはずが失敗する」という事故そのものが起こり得ない。
  - Ollamaサーバー自体はプログラム全体で1回だけ起動し、役の切り替えのたびに
    落として立て直したりしない（モデルの load/unload だけを行う）。
"""

import os
import sys
import json
import urllib.request
import urllib.error

import arc_agent
from scripts.config import OLLAMA_HOST, KEEP_ALIVE, MODELS
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
from scripts.role_loader import load_role
from scripts.memory import (
    start_session_log,
    append_session_log,
    append_shared_memory,
    build_system_prompt_with_memory,
)

# 思考モデルは、日本語Windowsのコンソール既定(cp932)では表示できない文字
# （中国語文字等）を時々出す。素のprintだと即クラッシュするため、置換表示に倒す。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLE_ID = "chat"
ROLE = load_role(BASE_DIR, ROLE_ID)
CHAT_MODEL = ROLE["model"]

# Execute役に引き継ぐ時の既定モデル（Qwen2.5-Coder 14B・推奨）
EXECUTE_DEFAULT_MODEL_KEY = "6"


def stream_chat(req):
    """ストリーム応答を表示しつつ、content全文とtool_callsを返す。"""
    content_full = ""
    tool_calls_full = []
    thinking_started = False
    content_started = False

    with urllib.request.urlopen(req, timeout=300) as res:
        for line in res:
            if not line:
                continue
            chunk = json.loads(line.decode("utf-8"))
            msg = chunk.get("message", {})

            tc = msg.get("tool_calls")
            if tc:
                tool_calls_full.extend(tc)

            thinking = ""
            for k, v in msg.items():
                if k in ("thinking", "reasoning", "reasoning_content") and isinstance(v, str):
                    thinking += v
            if thinking and not thinking_started:
                thinking_started = True
                print("[思考中...]", end="", flush=True)

            piece = msg.get("content", "")
            if piece:
                if not content_started:
                    content_started = True
                    print("\n" if thinking_started else "", end="")
                print(piece, end="", flush=True)
                content_full += piece

    print()
    return content_full, tool_calls_full


def run_execute_and_wait(server_proc, instructions):
    """
    Execute役を直接呼び出し（入れ子）、終わるまで待つ。
    自分のモデルをアンロードしてから呼び、戻ってきたら自分のモデルを再ロードする。
    Execute役が例外で落ちても、ここで捕まえて会話は継続させる。
    戻り値: Execute役からの報告（要約文字列）。無ければNone。
    """
    _, exec_model_name = MODELS[EXECUTE_DEFAULT_MODEL_KEY]
    unload_all_models()
    try:
        summary = arc_agent.start_interactive_chat(
            exec_model_name, "safe", server_proc,
            initial_message=instructions, is_nested=True,
        )
    except Exception as e:
        print(f"\n[ERROR] Execute役の実行中に問題が発生しました: {e}")
        summary = f"Execute役の実行中にエラーが発生し、中断しました: {e}"
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

            payload = json.dumps({
                "model": model_name,
                "messages": messages,
                "tools": ROLE["tools"],
                "stream": True,
                "keep_alive": KEEP_ALIVE,
                "options": {"temperature": 0.6, "top_p": 0.9},
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{OLLAMA_HOST}/api/chat", data=payload,
                headers={"Content-Type": "application/json"})

            print("\n--- Response ---")
            try:
                content, tool_calls = stream_chat(req)
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

            handoff = handoff_from_tool_calls(effective_tool_calls)
            if handoff:
                print(f"\n[HANDOFF] Execute役を呼び出します。理由: {handoff.get('reason') or '(不明)'}")
                append_session_log(log_path, "System", f"引き継ぎ指示: {handoff['instructions']}")

                summary = run_execute_and_wait(server_proc, handoff["instructions"])

                if summary:
                    note = f"[Execute役からの報告]\n{summary}"
                    messages.append({"role": "tool", "content": note})
                    append_session_log(log_path, "System", note)
                    print(f"\n{note}\n")
                continue

            remembered = False
            for act in tool_calls_to_actions(effective_tool_calls):
                if act["type"] == "remember":
                    append_shared_memory(BASE_DIR, "chat", act["note"])
                    print(f"\n[REMEMBER] 共有メモに書き残しました: {act['note']}")
                    append_session_log(log_path, "System", f"共有メモに追記: {act['note']}")
                    remembered = True

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
