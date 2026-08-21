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
import re
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
from scripts.skills import web_search, fetch_url, summarize_text, calculate
from scripts.role_loader import load_role, roles_providing_tool, role_tool_names
from scripts.dispatch import invoke_role
from scripts.memory import (
    start_session_log,
    append_session_log,
    append_role_transition,
    append_shared_memory,
    build_system_prompt_with_memory,
    render_recent_turns,
    build_handoff_brief,
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
SERVICEABLE_TOOL_NAMES = {
    "remember", "handoff_to_role",
    "search_web", "fetch_url", "summarize_text", "calculate",
}

# 上記のうち「実行して結果を本人に返し、同じターンの中で考えを続けさせる」もの。
# いずれも副作用が無く承認フローも要らない読み取り専用スキルなので、
# 雑談役が自分で実行してよい（scripts/skills.py）。
CHAT_SKILL_ACTION_TYPES = {"search", "fetch_url", "summarize", "calculate"}

# 1ターンの中で連続してスキルを実行できる上限。
# 検索→本文取得→要約、程度は通したいが、無限に調べ続けさせない。
MAX_SKILL_STEPS = 5

# 同じスキルを同じ引数で呼び直すだけの周回が、これだけ続いたら打ち切って
# 「今ある情報で答えろ」と促す（進んでいないのにターンが伸びるのを防ぐ）。
MAX_REPEATED_SKILL_ROUNDS = 2

# skillの結果が長すぎると雑談役のnum_ctxを食い潰すため、ここで頭打ちにする
# （arc_agent.py側と同じ考え方。特にfetch_urlはページ全文が返る）。
MAX_SKILL_FEEDBACK = 6000


def _called_tool_names(tool_calls):
    """tool_callsから呼び出されたtool名の一覧を取り出す。"""
    names = []
    for tc in tool_calls or []:
        name = (tc.get("function") or {}).get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _run_chat_skill(act, model_name):
    """
    雑談役が自分で実行してよい読み取り専用スキルを1つ実行し、
    モデルに返すフィードバック文字列を返す。

    [NOTE] roles/chat/role.json に、ここで実装していないtoolを足すと
    「使えないtool」として弾かれてしまう。tool を増やす時は
    SERVICEABLE_TOOL_NAMES と、必要ならここの分岐も併せて更新すること
    （起動時に _check_chat_tools_implemented() が食い違いを検出する）。
    """
    if act["type"] == "search":
        print(f"\n[SEARCH REQUESTED] クエリ: {act['query']}")
        res = web_search(act["query"])
    elif act["type"] == "fetch_url":
        print(f"\n[FETCH REQUESTED] URL: {act['url']}")
        res = fetch_url(act["url"])
    elif act["type"] == "summarize":
        print("\n[SUMMARIZE REQUESTED]")
        res = f"[SUMMARY]\n{summarize_text(act['text'], model_name, act.get('instruction'))}"
    elif act["type"] == "calculate":
        print(f"\n[CALCULATE REQUESTED] {act['expression']}")
        res = calculate(act["expression"])
    else:
        return None
    if len(res) > MAX_SKILL_FEEDBACK:
        res = res[:MAX_SKILL_FEEDBACK] + "\n...(長いため省略)"
    return res


def _tried_summary(skill_cache):
    """このターンで既に試した呼び出しを、人が読める形に並べる。"""
    label = {"search": "検索", "fetch_url": "本文取得",
             "calculate": "計算", "summarize": "要約"}
    lines = []
    for key in skill_cache:
        kind = label.get(key[0], key[0])
        arg = key[1] if len(key) > 1 else ""
        lines.append(f"- {kind}: {arg}" if arg else f"- {kind}")
    return "\n".join(lines)


def _skill_cache_key(act):
    """
    スキル呼び出しを「同じ呼び出しかどうか」で識別するキーを作る。

    [NOTE] 実機で、同じターンの中で calculate を同じ式のまま5回、
    search を同じクエリで3回呼ぶ挙動を確認した（MAX_SKILL_STEPSで
    止まるので暴走はしないが、結果は毎回同じなので純粋に無駄）。
    引数まで含めて突き合わせ、2回目以降は実行せず前回の結果を返す。
    """
    t = act["type"]
    if t == "search":
        return (t, act.get("query", "").strip())
    if t == "fetch_url":
        return (t, act.get("url", "").strip())
    if t == "calculate":
        return (t, act.get("expression", "").strip())
    if t == "summarize":
        return (t, hash(act.get("text", "")), (act.get("instruction") or "").strip())
    return (t,)


def _check_chat_tools_implemented():
    """
    roles/chat/role.json のtoolが、この会話ループで本当に実行できるか起動時に確かめる。
    実装が無いtoolを持たせると、モデルがそれを呼んでも「使えないtool」として
    弾かれ続けるという分かりにくい壊れ方をするため、起動時に気づけるようにする。
    """
    declared = {t["function"]["name"] for t in ROLE["tools"]}
    unimplemented = sorted(declared - SERVICEABLE_TOOL_NAMES)
    if unimplemented:
        print(f"[WARN] roles/chat/role.json の {unimplemented} は "
              f"chat_agent.py が実行方法を実装していません。"
              f"呼ばれても実行されないため、role.jsonから外すか実装を追加してください。")


# 「自分にはできない」と断って何もしない返答を見つけるためのパターン。
# tool呼び出しが1つも無い返答に対してのみ使う（_looks_like_deflection 参照）。
#
# [IMPORTANT] 「リアルタイム情報を提供する能力はありません」系は、モデルが
# 事前学習で強く持っている定型句で、プロンプトで「あなたは検索できる」と
# 書いても最初の一回はこれが出てしまうことを実機で確認した。素の
# 「私は直接」だけでは拾えないため、断り文句・丸投げ文句を広めに列挙する。
_DEFLECTION_PATTERNS = (
    # 「自分にはその能力が無い」と明確に断っている
    "私は直接", "私には直接", "私が直接",
    "能力はありません", "能力がありません", "能力を持っていません",
    "対応できません", "できかねます",
    "検索できません", "調べられません", "調べることはできません",
    # ユーザー自身に検索させようとしている
    "検索してみてください", "調べてみてください",
    "検索してください", "調べてください",
    "利用するのがおすすめ",
)
# [IMPORTANT] ここに何を入れないかが重要。実機で次の誤検知を出した:
#   ユーザー「何ができる？」
#   雑談役「…ファイルの読み書きはできません。それらが必要な場合は
#           適切な役に【引き継ぐ】のでご安心ください」
# これは完全に正しい自己紹介なのに、「引き継」が引っかかって促しが走り、
# 促し文に載せた例のクエリ（東京 天気 今日）をモデルが丸写しして、
# 会話と無関係な検索を実行してしまった。
# 「引き継/引継/役に頼/自分では」等は、断りではない普通の説明文にも
# 当たり前に出てくるため、意図的に外してある。
# 同様に「できません」「ご覧ください」「確認してください」も汎用的すぎるので入れない
# （「そのバージョンでは指定できません」「詳しくはドキュメントをご覧ください」等）。
# 検索後にリンクを並べて終わる形は、言い回しではなく
# _looks_like_link_dump() で構造的に検知する。


def _looks_like_deflection(text: str) -> bool:
    """
    「自分にはできない」「他の役に引き継ぐ」と会話文で述べるだけで、
    実際のtool呼び出しを1つもしていない返答かどうかを判定する。

    [NOTE] プロンプト側で「あなたは検索できる」「必ずtool呼び出し機能を
    使うこと」と何度強調しても、小型モデルでは確率的にしか守られないことを
    繰り返し確認している。そのため「断っただけで何もしていない」状態を
    コード側で検知して1回だけ促し直す保険をかける。これはこのプロジェクトで
    一貫して採ってきた「プロンプトでの軽減＋コードでの保険」の方針に沿う
    （arc_agent.py の MAX_AUTO_STEPS / MAX_FAILURE_STREAK 等と同じ考え方）。
    """
    if not text:
        return False
    return any(pattern in text for pattern in _DEFLECTION_PATTERNS)


_URL_RE = re.compile(r"https?://[^\s)\]>\"'、。]+")


def _looks_like_link_dump(text: str, searched: bool, fetched: bool) -> bool:
    """
    検索はしたのに本文を読まず、URLを並べただけで終わらせた返答かを判定する。

    [IMPORTANT] 言い回しでの判定はやめて、構造で判定する。
    実機で「〜サイトを訪れてみてください」を検知できるようにしたら、次は
    「これらのサイトから最新の天気情報を得ることができます」と言い換えられ、
    その次は別の言い方…と、いたちごっこになった。日本語の言い換えは無限にあり、
    パターンを増やすほど普通の説明文まで誤検知する。
    一方「検索したのに本文は読まず、URLだけ複数並べた」という【形】は
    言い回しに左右されない。search_webが返すのは一覧に過ぎないので、
    この形になっている時点で、ユーザーの質問には答えられていない。
    """
    if not searched or fetched:
        return False
    return len(set(_URL_RE.findall(text or ""))) >= 2


def run_role_and_wait(server_proc, role_id, instructions, log_path, root_request=None):
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
        summary = invoke_role(BASE_DIR, role_id, server_proc, instructions, log_path,
                              call_chain=["chat"], root_request=root_request)
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

            # [NOTE] このループは1周で抜けるのが普通。周回するのは
            # (a) 読み取り専用スキルを実行して結果を本人に返す時、
            # (b) 「引き継ぐと言ったのにtool呼び出しが無い」等を促し直す時。
            nudges_used = 0
            skill_steps = 0
            remembered = False
            fetched = False   # このターンで fetch_url を実行したか（リンク丸投げ検知に使う）
            skill_cache = {}      # このターンで実行済みのスキル呼び出し → 結果
            repeated_rounds = 0   # 実行済みの呼び出しだけを繰り返した回数
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
                actions = tool_calls_to_actions(effective_tool_calls)

                # [IMPORTANT] rememberは他のtool呼び出しと束ねて出てくることがある。
                # 終端系(handoff)を先に見ると取りこぼすため、常にここで先に処理する。
                for act in actions:
                    if act["type"] == "remember":
                        append_shared_memory(BASE_DIR, "chat", act["note"])
                        print(f"\n[REMEMBER] 共有メモに書き残しました: {act['note']}")
                        append_session_log(log_path, "System", f"共有メモに追記: {act['note']}")
                        remembered = True

                # --- 読み取り専用スキルは雑談役が自分で実行する ---
                # [NOTE] 以前は雑談役にこれらのtoolを持たせず、「調べて」の
                # たびに他の役へ引き継いでいた。しかし引き継ぎは
                # 「自分のモデルをアンロード→相手をロード→戻ってきて自分を再ロード」
                # という重い往復で、実機では数分かかるうえ、最も壊れやすい経路
                # でもあった（能力の誤申告の伝染・たらい回し）。search_web等は
                # 副作用が無く承認も要らないので、雑談役が直接使ってよい。
                skill_acts = [a for a in actions if a["type"] in CHAT_SKILL_ACTION_TYPES]
                if skill_acts:
                    if skill_steps >= MAX_SKILL_STEPS:
                        print(f"\n[SYSTEM] 1ターンでのスキル実行が上限({MAX_SKILL_STEPS}回)に達しました。")
                        messages.append({"role": "user", "content": (
                            "[SYSTEM NOTICE] 1回のやり取りで調べられる回数の上限に"
                            "達しました。これ以上toolを呼ばず、ここまでに分かったことを"
                            "普通の会話文でユーザーに答えてください。"
                        )})
                        skill_steps = 0
                        continue
                    if any(a["type"] == "fetch_url" for a in skill_acts):
                        fetched = True

                    # 同じターン内で同じ呼び出しを繰り返しても結果は変わらないので、
                    # 2回目以降は実行せず前回の結果を返し、そう伝える。
                    feedback_parts = []
                    all_cached = True
                    for a in skill_acts:
                        key = _skill_cache_key(a)
                        if key in skill_cache:
                            # [IMPORTANT] ここで「前回の結果を使って答えて」と返すのは誤り。
                            # モデルは多くの場合、その結果に欲しい情報が無かったからこそ
                            # やり直そうとしている。行き止まりに押し返すことになり、
                            # 実機では同じ検索を上限まで繰り返し続けた。
                            # 必要なのは「同じ手は無駄」ではなく「別の手に切り替えろ」で、
                            # そのために既に試したことを具体的に示す。
                            feedback_parts.append(
                                f"[SYSTEM NOTICE] その {a['type']} は、このやり取りの中で"
                                "既に同じ引数で実行済みです。実行自体は成功しており、"
                                "同じ引数で呼び直しても全く同じ結果が返るだけで前に進みません。"
                                "（結果は上のやり取りに残っているので読み返せます）\n"
                                f"このやり取りで既に試したこと:\n{_tried_summary(skill_cache)}\n"
                                "欲しい情報が得られなかったのは、呼び出しが失敗したからではなく"
                                "「取得できた内容にその情報が無かった」からです。"
                                "同じ手を繰り返さず、必ず別の手に切り替えてください:\n"
                                "(a) 検索結果に出ていた【まだ試していない別のURL】をfetch_urlする\n"
                                "(b) 【違うキーワード】で検索し直す"
                                "（地名を足す・「気温」「予報」等の語を足す・表現を変える）\n"
                                "(c) それでも分からなければ、分かった範囲と"
                                "分からなかった点を正直に答える\n"
                                "上の(a)(b)(c)のいずれかを選ぶこと。同じ呼び出しの再実行は選択肢にありません。"
                            )
                            print(f"\n[SYSTEM] 実行済みの {a['type']} を再要求されました。別の手を促します。")
                            continue
                        res = _run_chat_skill(a, model_name)
                        if res is None:
                            continue
                        skill_cache[key] = res
                        all_cached = False
                        feedback_parts.append(res)

                    if all_cached:
                        # 前に進んでいない。何度も続くようなら打ち切って答えさせる。
                        repeated_rounds += 1
                        if repeated_rounds >= MAX_REPEATED_SKILL_ROUNDS:
                            print("\n[SYSTEM] 同じスキル呼び出しの繰り返しを検知しました。回答を促します。")
                            append_session_log(log_path, "System", "同一スキル呼び出しの繰り返しを検知")
                            skill_steps = MAX_SKILL_STEPS
                    else:
                        repeated_rounds = 0

                    feedback = "\n\n".join(feedback_parts)
                    # [IMPORTANT] 検索結果を返すだけだと、モデルは
                    # 「参考になるサイトはこちらです」とサイト名やURLを列挙して
                    # 終わりにしがちで、ユーザーの質問には答えないままになる。
                    # 悪い出力を後から検知して直させるより、結果を渡す【その場で】
                    # 次に何をすべきかを添える方が確実（実機で、検知側の言い回し
                    # 一致は何度もすり抜けられた）。
                    if any(a["type"] == "search" for a in skill_acts) and not fetched:
                        feedback += (
                            "\n\n[SYSTEM NOTICE] 上は検索結果の一覧です。"
                            "タイトル・URL・短い抜粋だけで、ユーザーが知りたい中身"
                            "そのものではありません。"
                            "サイト名やURLを並べただけの返答は、質問への回答に"
                            "なっていません（ユーザーに調べさせているのと同じです）。"
                            "抜粋だけで確実に答えられる場合を除き、この中から最も"
                            "適したURLを1つ選んで fetch_url を実行し、本文を読んでから"
                            "答えてください。"
                        )
                    print("\n[SYSTEM] 実行結果をAIにフィードバックして解析中...")
                    append_session_log(log_path, "System", f"スキル実行結果を反映({len(skill_acts)}件)")
                    messages.append({"role": "user", "content": feedback})
                    skill_steps += 1
                    continue

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
                elif _looks_like_link_dump(strip_think_blocks(content), skill_steps > 0, fetched):
                    # パターン2: 検索はしたが本文を読まず、URLを並べて終わらせた。
                    # search_webは一覧を返すだけなので、この形では質問に答えられていない。
                    nudge = (
                        "[SYSTEM NOTICE] 今の返答は誤りです。検索は済んでいますが、"
                        "search_webが返すのは検索結果の一覧（タイトル・URL・短い抜粋）"
                        "だけで、ユーザーが知りたい中身そのものではありません。"
                        "リンクを並べて案内するのは、結局ユーザー自身に調べさせているのと"
                        "同じで誤りです。\n"
                        "検索結果の中から最も適したURLを1つ選び、今すぐ"
                        "tool呼び出し機能で fetch_url を実行して本文を取得し、"
                        "その内容を読んでユーザーの質問に直接答えてください:\n"
                        "```json\n"
                        '{"name": "fetch_url", "arguments": {"url": "https://..."}}\n'
                        "```\n"
                        "※ urlには、直前の検索結果に実際に出てきたURLを入れること"
                        "（存在しないURLを想像で書かないこと）。"
                    )
                    print("\n[SYSTEM] 検索結果のリンクを並べただけの返答を検知しました。促し直します。")
                    append_session_log(log_path, "System", "検索後にfetch_urlせず丸投げしたため促し直し")
                elif _looks_like_deflection(strip_think_blocks(content)):
                    # パターン3: 「自分にはできない」と断っただけでtool呼び出しが無い。
                    # [IMPORTANT] ここで handoff_to_role を勧めてはいけない。
                    # 一般的な調べ物は雑談役自身のスキルで完結するのに、
                    # 引き継ぎを勧めるとモデルは素直に従い、不要なモデル入れ替えが
                    # 走る（実機で、天気を聞かれてexecute役に引き継ぐのを確認）。
                    # まず「自分でやれ」、権限が要る時だけ引き継ぎ、の順で促す。
                    # [IMPORTANT] 抽象的に「search_webを使え」と促すだけでは
                    # 効かないことを実機で確認した（促した直後に「私の誤解が
                    # ありました」と言いながら、同じ断り文句を繰り返した）。
                    # このプロジェクトで一貫して効いてきたのは、そのまま真似できる
                    # 具体的なJSONを見せること（plan役の未呼び出し問題等でも同様）。
                    # [NOTE] ただし query に依頼文をそのまま埋めた例を見せるのは
                    # 逆効果だった。モデルはその文字列を丸写しし、
                    # 「貴方が調べてよ」のような検索語として無意味なqueryで
                    # 検索してしまう（実機で確認）。呼び出しの【形】だけを見せ、
                    # 中身は会話から自分で組み立てさせる。
                    nudge = (
                        "[SYSTEM NOTICE] 今の返答は誤りです。あなたは search_web を"
                        "持っており、ネット検索をその場で実行できます。"
                        "「リアルタイム情報を提供する能力はありません」は、"
                        "search_webを持っていなかった頃の言い回しで、"
                        "今のあなたには当てはまりません。\n"
                        "会話文で断るだけでは何も実行されず、ユーザーの依頼は"
                        "果たされないままです。ユーザーは検索の仕方を聞いているのでは"
                        "なく、あなたに調べてほしいと言っています。\n"
                        "今すぐ、次の形のtool呼び出しを（文章としてではなく"
                        "tool呼び出し機能そのもので）実行してください:\n"
                        "```json\n"
                        '{"name": "search_web", "arguments": {"query": "検索キーワード"}}\n'
                        "```\n"
                        "※ \"検索キーワード\" の部分は、この例の文字列をそのまま使わず、"
                        "直前までの会話でユーザーが知りたがっている内容に合う検索語を"
                        "自分で組み立てて入れること（ユーザーの発言をそのまま貼るのでは"
                        "なく、検索に適した語にする。例えば「貴方が調べてよ」ではなく"
                        "「東京 天気 今日」のように）。\n"
                        "検索結果が返ってきたら、それを読んでユーザーに答えること。\n"
                        "なお、ファイルの読み書きやコマンド実行など、あなたの権限では"
                        "できない作業が必要な場合に限り handoff_to_role を使います。"
                    )
                    print("\n[SYSTEM] 何もせず断る返答を検知しました。促し直します。")
                    append_session_log(log_path, "System", "断りだけでtool呼び出しが無いため促し直し")
                else:
                    break

                nudges_used += 1
                messages.append({"role": "user", "content": nudge})

            # [NOTE] remember と読み取り専用スキルは上のループの中で処理済み
            # （handoff等の終端系より先に処理しないと、束ねて出てきた時に
            # 取りこぼす。arc_agent.py側で実機確認した不具合と同じ系統）。
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

                full_instructions = build_handoff_brief(
                    role_tool_names(BASE_DIR, role_id),
                    user_input,
                    render_recent_turns(messages),
                    handoff["instructions"],
                )

                summary = run_role_and_wait(server_proc, role_id, full_instructions,
                                            log_path, root_request=user_input)

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
    _check_chat_tools_implemented()
    setup_environment()
    startup_cleanup()

    log_file = os.path.join(BASE_DIR, "chat_server.log")
    server_proc = run_server(log_file)
    warmup_model(CHAT_MODEL)
    log_path = start_session_log(BASE_DIR, "chat")

    run_chat_loop(CHAT_MODEL, server_proc, log_path)


if __name__ == "__main__":
    main()
