import os
import sys
import time
import subprocess
import shutil
import re
import json
import base64
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime

# 点字スピナー等、日本語Windowsのコンソール既定(cp932)では表示できない文字を
# 出すことがある。素のprintだと即クラッシュするため、置換表示に倒す。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# [NOTE] stdoutだけreconfigureしてstdinを忘れると、標準入力がリダイレクト/パイプ
# 経由（対話的なコンソールでない）の時に、Pythonがコンソールの既定コードページ
# (cp932)でinput()をdecodeしてしまい、UTF-8で書かれた日本語の入力が丸ごと
# 文字化けする（エラーにはならず、無言で化けた文字列がそのままモデルに渡る）。
# [IMPORTANT] reconfigure()はストリームから1度でも読み込んだ後には呼べない
# （RuntimeError）。chat_agent.pyからの入れ子呼び出しでは、このモジュールが
# importされる時点で既にchat_agent.py側がstdinから読み込み済み（同じプロセス・
# 同じsys.stdin）なので、ここでのreconfigureは常に失敗する。失敗しても
# chat_agent.py側で既に正しくreconfigure済みなので実害は無く、try/exceptで
# 無視してよい（単体起動時は逆にこちらが最初のreconfigureになるので効く）。
if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (RuntimeError, ValueError):
        pass

from scripts.config import (
    OLLAMA_HOST,
    KEEP_ALIVE,
    MODELS,
    EXEC_MODES,
)
from scripts.ollama import (
    setup_environment,
    cleanup_processes,
    unload_all_models,
    startup_cleanup,
    wait_for_server,
    run_server,
    warmup_model,
    list_installed_models,
)
from scripts.tools import (
    tool_calls_to_actions,
    tool_call_from_content,
    return_to_caller_from_tool_calls,
    handoff_from_tool_calls,
    strip_think_blocks,
)
from scripts.display import stream_chat_response
from scripts.skills import web_search, fetch_url, summarize_text, calculate, git_diff_summary
from scripts.role_loader import load_role, role_tool_names
from scripts.dispatch import invoke_role
from scripts.memory import (
    start_session_log,
    append_session_log,
    append_shared_memory,
    append_role_transition,
    build_system_prompt_with_memory,
    render_recent_turns,
    build_call_chain_notice,
    build_handoff_brief,
)

# ==========================================
# 1. 動作・環境設定 (Intel Arc A770 最適化)
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROLE_ID = "execute"
ROLE = load_role(BASE_DIR, ROLE_ID)
PROMPT_FILE = os.path.join(BASE_DIR, "roles", ROLE_ID, "prompt.txt")

# ★ AIが実行するコマンドの作業ディレクトリ（cdが引き継がれない問題への物理的対策）
#   起動時にユーザーが設定する。未設定なら BASE_DIR を使う。
WORK_DIR = BASE_DIR

CURRENT_MODE = "safe"

_prev_len = [0]


_FAILURE_TAG_RE = re.compile(r"^\s*\[[A-Z_ ]*(ERROR|FAILED)\]")


def is_failure_feedback(text: str) -> bool:
    """
    行動の実行結果(feedback)が失敗を表すかを判定する。
    このファイル・scripts/skills.pyのエラー文言はすべて
    `[EDIT ERROR]`/`[EDIT FAILED]`/`[WRITE ERROR]`/`[READ ERROR]`/
    `[API ERROR]`/`[SEARCH ERROR]`/`[FETCH ERROR]`/`[SUMMARIZE ERROR]`/
    `[CALC ERROR]` のように「先頭が大文字の角括弧＋ERROR/FAILED」で
    統一されているため、その形を見るだけで型を問わず判定できる。
    """
    return bool(_FAILURE_TAG_RE.match(text or ""))


def clean_clixml_noise(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"#<\s*CLIXML.*?</Objs>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"#<\s*CLIXML.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"Preparing modules for first use\.?", "", text, flags=re.IGNORECASE)
    return text.strip()


# ==========================================
# 3. SEARCH/REPLACE の適用（曖昧マッチング付き）
# ==========================================
def _normalize_lines(text):
    return [line.strip() for line in text.splitlines()]


def apply_search_replace(file_content, search_block, replace_block):
    if search_block in file_content:
        return file_content.replace(search_block, replace_block, 1), "完全一致で置換しました。"

    content_lines = file_content.splitlines()
    search_lines = search_block.splitlines()
    norm_search = _normalize_lines(search_block)
    if not norm_search:
        return None, "SEARCHが空です。"

    match_indexes = []
    for i in range(len(content_lines) - len(search_lines) + 1):
        window = [content_lines[i + j].strip() for j in range(len(search_lines))]
        if window == norm_search:
            match_indexes.append(i)

    if len(match_indexes) == 1:
        i = match_indexes[0]
        original_first = content_lines[i]
        indent = original_first[:len(original_first) - len(original_first.lstrip())]
        replace_lines = replace_block.splitlines()
        adjusted = []
        for idx, rl in enumerate(replace_lines):
            if idx == 0 and rl and not rl.startswith((" ", "\t")):
                adjusted.append(indent + rl)
            else:
                adjusted.append(rl)
        new_lines = content_lines[:i] + adjusted + content_lines[i + len(search_lines):]
        return "\n".join(new_lines), "曖昧一致（空白差異を吸収）で置換しました。"

    if len(match_indexes) > 1:
        lines_str = ", ".join(str(m + 1) for m in match_indexes)
        return None, (f"SEARCHが複数箇所({lines_str}行目付近)に一致しました。"
                      f"もっと前後の行を含めて一意にしてください。")

    hint = _find_similar_hint(content_lines, norm_search)
    return None, (f"SEARCHブロックが見つかりませんでした。{hint}"
                  f"Get-Contentで読み直し、実在する行を正確にSEARCHに入れてください。")


def _find_similar_hint(content_lines, norm_search):
    if not norm_search:
        return ""
    target = norm_search[0]
    if not target:
        return ""
    for idx, line in enumerate(content_lines):
        if target in line.strip():
            return (f"（ヒント: {idx + 1}行目に似た行 '{line.strip()[:60]}' があります。）")
    return ""


def run_edit(edit, mode):
    file_path = edit["file"]
    print(f"\n[EDIT REQUESTED] 対象: {file_path}")
    print(f"  SEARCH({len(edit['search'].splitlines())}行) -> REPLACE({len(edit['replace'].splitlines())}行)")

    # 編集は「不可逆(変更系)」操作。モードに応じて承認要否を決める。
    op_type_str = "不可逆(変更/編集系)"
    need_approval = False
    if mode == "strict":
        need_approval = True
    elif mode == "safe":
        need_approval = True
    # full の場合は need_approval = False のまま（自動実行）

    if need_approval:
        raw = input(f"この {op_type_str} 編集を実行しますか？ (y/n / 拒否理由コメント): ").strip()
        val = unicodedata.normalize('NFKC', raw).lower()

        if val == 'y':
            pass  # 実行へ
        elif val == 'n' or val == '':
            print("[API NOTICE] 実行が拒否されました。")
            return "[SYSTEM NOTICE] ユーザーによってファイル編集が拒否されました。"
        else:
            # y/n 以外の文字列を入力した場合はコメント付き拒否として返す
            print(f"[API NOTICE] 実行が拒否されました (コメント: {raw})")
            return f"[SYSTEM NOTICE] ユーザーによってファイル編集が拒否されました。拒否理由/指示: {raw}"

    if not os.path.exists(file_path):
        msg = f"[EDIT ERROR] ファイルが存在しません: {file_path}（先にCopy-Item等で作成が必要）"
        print(msg)
        return msg

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as ex:
        msg = f"[EDIT ERROR] 読み込み失敗 {file_path}: {ex}"
        print(msg)
        return msg

    new_content, message = apply_search_replace(content, edit["search"], edit["replace"])
    if new_content is None:
        msg = f"[EDIT FAILED] {file_path}: {message}"
        print(msg)
        return msg

    try:
        shutil.copy2(file_path, file_path + ".bak")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        msg = f"[EDIT OK] {file_path}: {message}（バックアップ: {os.path.basename(file_path)}.bak）"
        print(msg)
        return msg
    except Exception as ex:
        msg = f"[EDIT ERROR] 書き込み失敗 {file_path}: {ex}"
        print(msg)
        return msg


def run_write(write_args, mode):
    """
    新規ファイルの作成、または既存ファイルの全文書き換え。
    edit_fileはSEARCH/REPLACEで既存ファイルの一部だけを直すためのものだが、
    真っ新なファイルを作る手段がこれまで無く、prompt側の「Copy-Itemで空の
    ファイルを作ってからedit_fileで書く」という回りくどい手順に頼っていた。
    write_fileは指定した内容をそのまま書き込む。既存ファイルを上書きする
    場合は必ずバックアップを取る（edit_fileと同じ安全策）。
    """
    file_path = write_args["file"]
    content = write_args["content"]
    exists = os.path.exists(file_path)
    action_label = "上書き" if exists else "新規作成"
    print(f"\n[WRITE REQUESTED] 対象: {file_path} ({action_label})")

    # 書き込みは「不可逆(変更系)」操作。edit_fileと同じ承認ルール。
    op_type_str = "不可逆(変更/編集系)"
    need_approval = False
    if mode == "strict":
        need_approval = True
    elif mode == "safe":
        need_approval = True
    # full の場合は need_approval = False のまま（自動実行）

    if need_approval:
        raw = input(f"この {op_type_str} 書き込み（{action_label}）を実行しますか？ (y/n / 拒否理由コメント): ").strip()
        val = unicodedata.normalize('NFKC', raw).lower()

        if val == 'y':
            pass  # 実行へ
        elif val == 'n' or val == '':
            print("[API NOTICE] 実行が拒否されました。")
            return "[SYSTEM NOTICE] ユーザーによってファイル書き込みが拒否されました。"
        else:
            print(f"[API NOTICE] 実行が拒否されました (コメント: {raw})")
            return f"[SYSTEM NOTICE] ユーザーによってファイル書き込みが拒否されました。拒否理由/指示: {raw}"

    try:
        if exists:
            shutil.copy2(file_path, file_path + ".bak")
        else:
            parent = os.path.dirname(file_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        backup_note = f"（バックアップ: {os.path.basename(file_path)}.bak）" if exists else ""
        msg = f"[WRITE OK] {file_path}: {action_label}しました{backup_note}"
        print(msg)
        return msg
    except Exception as ex:
        msg = f"[WRITE ERROR] 書き込み失敗 {file_path}: {ex}"
        print(msg)
        return msg


def run_read(read):
    """ファイルをPythonがUTF-8で直接読み、行番号付きで返す（PowerShell経由の文字化けを回避）。"""
    file_path = read["file"]
    if not os.path.isabs(file_path):
        file_path = os.path.join(WORK_DIR, file_path)
    print(f"\n[READ REQUESTED] 対象: {file_path}")

    if not os.path.exists(file_path):
        msg = f"[READ ERROR] ファイルが存在しません: {file_path}"
        print(msg)
        return msg

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, "r", encoding="cp932", errors="replace") as f:
                content = f.read()
        except Exception as ex:
            msg = f"[READ ERROR] 読み込み失敗 {file_path}: {ex}"
            print(msg)
            return msg
    except Exception as ex:
        msg = f"[READ ERROR] 読み込み失敗 {file_path}: {ex}"
        print(msg)
        return msg

    lines = content.splitlines()
    numbered = "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    print(f"[READ OK] {len(lines)}行を読み込みました。")
    return f"[FILE CONTENT of {file_path} ({len(lines)}行)]\n{numbered}"


def run_remember(act):
    """全役共通の共有メモ(shared_memory.md)に1件書き残す。"""
    note = act["note"]
    append_shared_memory(BASE_DIR, "execute", note)
    print(f"\n[REMEMBER] 共有メモに書き残しました: {note}")
    return f"[REMEMBER OK] 共有メモに書き残しました: {note}"


# ==========================================
# 4. コマンド安全判定 & 実行
# ==========================================
def is_read_only_command(command: str) -> bool:
    cmd = command.strip()
    if re.search(r'>|>>', cmd):
        return False
    mutating_patterns = [
        r'\b(remove|set|new|add|rename|move|copy|clear|stop|restart|invoke|start|register|unregister)-',
        r'\b(rm|del|erase|mkdir|md|rmdir|rd|mv|cp|ren|write|out-file|set-content|add-content|clear-content)\b',
        r'\bgit\s+(commit|push|pull|checkout|merge|rebase|reset|clean|branch\s+-[dD])\b',
        r'\b(pip|npm|yarn|cargo|apt|winget|choco)\s+(install|uninstall|update|remove|build)\b'
    ]
    for pattern in mutating_patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False
    read_only_patterns = [
        r'^\s*(get|show|find|test|select|measure)-',
        r'^\s*(dir|ls|cat|type|pwd|cd|tree|echo|Get-ChildItem|Get-Content|Get-Location|Select-String|ForEach-Object)\b',
        r'^\s*git\s+(status|log|diff|show|branch)\b'
    ]
    sub_commands = [c.strip() for c in cmd.split(';') if c.strip()]
    for sub in sub_commands:
        sub_is_ro = False
        for pattern in read_only_patterns:
            if re.search(pattern, sub, re.IGNORECASE):
                sub_is_ro = True
                break
        if not sub_is_ro:
            return False
    return True


def run_command(command: str, mode: str) -> str:
    read_only = is_read_only_command(command)
    op_type_str = "可逆(参照系)" if read_only else "不可逆(変更/削除系)"
    print(f"\n[API TRIGGERED] コマンド要求: {command}")
    print(f"[OP TYPE] 操作属性: {op_type_str}")

    need_approval = False
    if mode == "strict":
        need_approval = True
    elif mode == "safe":
        need_approval = not read_only

    if need_approval:
        raw = input(f"この {op_type_str} コマンドを実行しますか？ (y/n): ")
        if unicodedata.normalize('NFKC', raw).strip().lower() != 'y':
            print("[API NOTICE] 実行が拒否されました。")
            return "[SYSTEM NOTICE] ユーザーによってコマンドの実行が拒否されました。"

    print(f"[RUNNING] -> {command}")
    try:
        # [NOTE] 文字化けの原因は2つ重なっていた。
        # (1) 出力側: 日本語Windowsでは、PowerShellの標準出力は既定でコンソールの
        #     コードページ(cp932)で書き出される。以前はここをutf-8で先にdecodeを
        #     試みており、cp932のバイト列がたまたまutf-8として"エラー無く"decode
        #     できてしまうケースで文字化けが起きていた（decode自体は成功するので
        #     フォールバックのcp932側に落ちない）。
        # (2) 入力側: Windows PowerShell 5.1のGet-Content等は、UTF-8(BOM無し)の
        #     ファイルを読む時、既定ではシステムのコードページ(cp932)として読んで
        #     しまう（BOM付きUTF-8/UTF-16でなければ自動判定されない）。この
        #     リポジトリのファイルはBOM無しUTF-8で保存されているため、Get-Content
        #     経由で読むと内部表現の時点で既に化けており、正しい日本語パターンで
        #     Select-Stringしても一致しない、という無言の不具合になっていた。
        # 両方をutf-8に固定することで解消する。
        wrapped = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$PSDefaultParameterValues['*:Encoding'] = 'utf8'; "
            "$ProgressPreference='SilentlyContinue'; " + command
        )
        encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
        ps_command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-EncodedCommand", encoded]
        result = subprocess.run(ps_command, capture_output=True, cwd=WORK_DIR)
        try:
            stdout = result.stdout.decode('utf-8')
            stderr = result.stderr.decode('utf-8')
        except UnicodeDecodeError:
            stdout = result.stdout.decode('cp932', errors='replace')
            stderr = result.stderr.decode('cp932', errors='replace')
        stderr = clean_clixml_noise(stderr)

        print("--- [COMMAND OUTPUT] ---")
        if stdout:
            print(stdout, end="")
        if stderr:
            print(f"Error Output: {stderr}", end="")
        print("\n------------------------")

        output = ""
        if stdout:
            output += stdout
        if stderr.strip():
            output += f"\n[Error Output]\n{stderr}"
        return output if output.strip() else "(実行結果: 出力なし。正常終了)"
    except Exception as e:
        print(f"[API ERROR] {e}")
        return f"[API ERROR] 実行失敗: {e}"


# ==========================================
# 5. 対話セッション（物理防御入り）
# ==========================================
def start_interactive_chat(model_name: str, exec_mode: str, server_proc,
                           debug_mode: bool = False, raw_dump: bool = False,
                           initial_message: str = None, is_nested: bool = False,
                           num_ctx: int = 8192, log_path: str = None,
                           role_id: str = None, call_chain: list = None,
                           root_request: str = None):
    """
    このファイルの実装を使う役（既定はExecute役）のセッション本体。

    is_nested=True の場合、他の役から直接呼び出されている（入れ子呼び出し）。
    この場合は自分でOllamaサーバーを止めたりしない（呼び出し元と共有しているため）。
    代わりに、自分のモデルだけVRAMから解放してから、呼び出し元に制御を返す
    （return_to_callerが呼ばれれば要約文字列を、それ以外の終了ならNoneを返す）。

    is_nested=False（単体起動時）は今まで通り、終了時にサーバーも止めて完全に片付ける。

    log_path: 呼び出し元から共通のセッションログを渡された場合はそこに追記する
    （役をまたいでも1つのログで会話の続きを追えるようにするため）。
    未指定（単体起動時）なら自分で新しいログファイルを作る。

    role_id: このファイル(arc_agent.py)を"module"として使う役はExecute以外にも
    ありうる（例: 読み取り専用のReview役）。role_idを指定すると、モジュール
    読み込み時に固定された execute のプロンプト/tool一覧ではなく、
    roles/<role_id>/ の定義を都度読み込んで使う。未指定なら従来通り execute。
    [IMPORTANT] モジュール直下の `ROLE` はimport時に一度だけ読まれる固定値
    （execute用）なので、他の役がこのモジュールを共用する時は必ずrole_idを渡すこと。
    渡し忘れると、その役のtool制限（例: edit_fileを持たせない）が効かず、
    実際にはExecute役と同じ全toolが使えてしまう。

    call_chain: ここまでの呼び出し履歴（役IDのリスト。例: ["chat", "plan"]）。
    役同士が対等に呼び合える構造のため、無限にたらい回しが続く危険がある。
    自分より前の履歴+自分自身をプロンプトに見せて「同じ役が既に出てきていないか」
    をモデル自身に判断させ、かつ MAX_CALL_DEPTH を超えたらこの役自身は
    handoff_to_roleを使えなくする（tool一覧から動的に外す）ことで、
    プロンプトの指示が守られなくても物理的にループし得ないようにする。
    """
    role = load_role(BASE_DIR, role_id) if role_id else ROLE
    log_role_name = role_id or ROLE_ID
    this_role_id = role_id or ROLE_ID
    chain = list(call_chain or [])

    # 呼び出し深さの上限。これを超えたら、この役はhandoff_to_role自体を
    # 使えなくする（role["tools"]からその場で除外する）ことで、プロンプトの
    # 指示（ループを避けろ）が守られなかった場合の物理的な保険にする。
    MAX_CALL_DEPTH = 6
    at_max_depth = len(chain) + 1 >= MAX_CALL_DEPTH
    role_tools = role["tools"]
    depth_notice = None
    if at_max_depth:
        role_tools = [t for t in role_tools if t["function"]["name"] != "handoff_to_role"]
        print(f"\n[SYSTEM] 呼び出し階層が上限({MAX_CALL_DEPTH})に達しているため、"
              f"この役ではhandoff_to_roleを無効化します。")
        # [IMPORTANT] toolを外すだけでは足りない。実機で、外された後も
        # handoff_to_roleのJSONを出し続け（何も実行されない）、結局ユーザーには
        # 「できません」しか返らないまま連鎖が終わるのを確認した。
        # 「もう引き継げないので自分で完結させて報告しろ」と明示する。
        depth_notice = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "【重要: これ以上ほかの役に引き継げません】\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "呼び出し階層が上限に達したため、handoff_to_roleはあなたのtool一覧から"
            "外されています。呼び出そうとしても何も実行されません。\n"
            "自分が持っているtoolでできる範囲を実行し、必ずreturn_to_callerで"
            "結果を報告してください。できない部分があれば「何ができて何ができ"
            "なかったか」をsummaryに具体的に書くこと。何も報告せずに終わるのが"
            "最も避けるべき結果です。"
        )

    warmup_model(model_name)
    if log_path is None:
        log_path = start_session_log(BASE_DIR, log_role_name)

    def teardown(summary=None):
        """呼び出し元に返す前の後片付け。nestedならモデル解放だけ、単体ならサーバーも止める。"""
        unload_all_models()
        if not is_nested:
            try:
                server_proc.terminate()
                server_proc.wait(timeout=5)
            except Exception:
                pass
            cleanup_processes()
        return summary

    chat_log_file = None
    if debug_mode:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        chat_log_file = os.path.join(BASE_DIR, f"debug_chat_log_{timestamp}.txt")
        print(f"[DEBUG LOG] 会話ログ記録先: {chat_log_file}")
        with open(chat_log_file, "w", encoding="utf-8") as f:
            f.write(f"=== Debug Chat Log - Model: {model_name} - Time: {timestamp} ===\n\n")

    print(f"\n[CLIENT] {model_name} との対話セッションを開始します。")
    print(f"[MODE] 実行承認モード: {EXEC_MODES[exec_mode]}")
    print(f"[WORK_DIR] コマンド実行ディレクトリ: {WORK_DIR}")
    print(f"[KEEP_ALIVE] アイドル {KEEP_ALIVE} で自動的にVRAMを解放します。")
    print(f"[CONTEXT] num_ctx = {num_ctx}")
    if raw_dump:
        print("[MODE] 生ダンプモード有効（フィールド切り替え式・都度出力）")
    elif debug_mode:
        print("[MODE] ログモード有効（生ストリーム＋会話ファイル保存）")
    print("※ 終了: 'exit'/'quit' / 送信: 新しい行で 'EOF' か Ctrl+Z/Ctrl+D\n")

    system_prompt = build_system_prompt_with_memory(role["prompt"], BASE_DIR)
    chain_notice = build_call_chain_notice(chain, this_role_id)
    if chain_notice:
        system_prompt = f"{system_prompt}\n\n{chain_notice}"
    if depth_notice:
        system_prompt = f"{system_prompt}\n\n{depth_notice}"
    messages = [{"role": "system", "content": system_prompt}]
    print("[AI] 初期化が完了しました。質問をどうぞ。")

    MAX_AUTO_STEPS = 12
    MAX_SAME_ACTION = 2
    # [NOTE] 実機テストで、Plan/Writer役が「今すぐ書けるのに、rememberに
    # 内容を少しずつ書き残すだけで本来の仕事(return_to_caller)をしない」
    # という不具合を繰り返し起こした（毎回内容が違うため、下の「同一アクション
    # 連続検知」には引っかからなかった）。プロンプト側で軽減したが、プロンプトの
    # 指示追従は確率的で完全ではないため、コード側にも保険をかけておく。
    MAX_REMEMBER_STREAK = 2
    # [NOTE] 実機テストで、Review役が実在しないファイルパスを毎回少しずつ
    # 変えながら(read_fileの引数が違う=同一アクション連続検知に引っかからない)
    # 12回リトライし続けた不具合を確認した。型が同じでも結果が失敗し続けて
    # いれば、進捗が無いという意味では同一アクション連続と同じ暴走。
    MAX_FAILURE_STREAK = 2

    # [IMPORTANT] 想定外の例外（バグ・EOFError等）でここから抜けた場合でも、
    # VRAM解放だけは必ず行う。既知の終了経路はそれぞれ自分でteardown()を
    # 呼んでから return するので、cleaned_up フラグで二重に走らないようにする。
    cleaned_up = False
    try:
        while True:
            if initial_message is not None:
                user_input = initial_message.strip()
                initial_message = None
                print("\n[Input] ---------------------------------")
                print(f">>> {user_input}")
            else:
                try:
                    print("\n[Input] ---------------------------------")
                    lines = []
                    while True:
                        line = input(">>> " if not lines else "... ")
                        if not lines and line.strip().lower() in ["exit", "quit"]:
                            cleaned_up = True
                            return teardown()
                        if line.strip().upper() == "EOF":
                            break
                        lines.append(line)
                except (EOFError, KeyboardInterrupt):
                    print("\n[INFO] 対話を終了します。")
                    cleaned_up = True
                    return teardown()
                user_input = "\n".join(lines).strip()

            if not user_input:
                continue

            messages.append({"role": "user", "content": user_input})
            append_session_log(log_path, "User", user_input)
            if chat_log_file:
                with open(chat_log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[User Input]\n{user_input}\n")

            auto_steps = 0
            last_action_signature = None
            same_action_count = 0
            remember_streak = 0
            failure_streak = 0

            while True:
                payload = json.dumps({
                    "model": model_name,
                    "messages": messages,
                    "tools": role_tools,
                    "stream": True,
                    "keep_alive": KEEP_ALIVE,
                    "options": {
                        "num_ctx": num_ctx,
                        "temperature": 0.2,
                        "top_p": 0.9,
                        "repeat_penalty": 1.15
                    }
                }).encode("utf-8")

                req = urllib.request.Request(
                    f"{OLLAMA_HOST}/api/chat", data=payload,
                    headers={"Content-Type": "application/json"})

                print("\n--- Response ---")

                try:
                    ai_response_full, tool_calls = stream_chat_response(req, debug_mode, raw_dump)
                except urllib.error.URLError as e:
                    print(f"\n[ERROR] 通信エラー: {e}")
                    cleaned_up = True
                    return teardown()

                print()
                assistant_msg = {"role": "assistant", "content": ai_response_full}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)
                append_session_log(log_path, "Assistant", strip_think_blocks(ai_response_full) or "(tool呼び出しのみ)")
                if chat_log_file:
                    with open(chat_log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[AI Response]\n{ai_response_full}\n")

                effective_tool_calls = tool_calls or tool_call_from_content(strip_think_blocks(ai_response_full))

                # [IMPORTANT] return_to_callerを他のtool呼び出しより先にチェックしていると、
                # モデルが1回の返答で複数tool（例: remember + return_to_caller）をまとめて
                # 出した時、return_to_callerの方だけが処理されて他のtool呼び出しが無言で
                # 捨てられる不具合になる（実機で確認済み。「rememberを呼んだのに
                # shared_memory.mdに書かれない」という形で顕在化した）。
                # 「1回の返答で出すアクションは1個だけ」というプロンプト側の指示は
                # 確率的にしか守られないため、コード側でも先に実行すべきactionが
                # あればそちらを優先し、return_to_callerはactionsが無い時だけ見る。
                actions = tool_calls_to_actions(effective_tool_calls)

                if not actions:
                    # [NOTE] 役同士が対等に呼び合える構造では、この役自身も他の役へ
                    # handoff_to_roleできる（role.jsonのcan_handoff_toで許可された
                    # 範囲。呼び出し深さの上限に達していればtool一覧から動的に
                    # 除外済みなので、at_max_depth時はここに来ない）。
                    # actionsが無い時だけ見るのは、上と同じ「終端系のtool呼び出しを
                    # 他より先にチェックすると、bundleされた時に本来やるべき
                    # アクションが無言で消える」不具合を避けるため。
                    handoff = handoff_from_tool_calls(effective_tool_calls) if not at_max_depth else None
                    if handoff:
                        target_role_id = handoff["role_id"]
                        # [IMPORTANT] tool定義のenumで引き継ぎ先を制限していても、
                        # モデルはそれを無視した role_id を平気で書いてくる。
                        # 特に「自分自身」を指定されると、同じ依頼を延々と自分に
                        # 渡し続ける無限ループになる（実機で execute→execute→…
                        # をMAX_CALL_DEPTHまで繰り返す暴走を確認）。
                        # dispatchする前にcan_handoff_toで検証する。
                        allowed = role.get("can_handoff_to", [])
                        if target_role_id not in allowed:
                            reason = ("自分自身には引き継げません"
                                      if target_role_id == this_role_id
                                      else f"{target_role_id}役はあなたの引き継ぎ先ではありません")
                            notice = (
                                f"[SYSTEM NOTICE] {reason}。"
                                f"引き継げるのは次の役だけです: {', '.join(allowed) or '(なし)'}。"
                                "この一覧に無い役を指定しても実行されません。"
                                "適切な役を選び直すか、自分でできる範囲の作業を続けるか、"
                                "これ以上できることが無ければreturn_to_callerで"
                                "ここまでの状況を報告してください。"
                            )
                            print(f"\n[SYSTEM] 無効な引き継ぎ先({target_role_id})を拒否しました。")
                            append_session_log(log_path, "System", f"無効な引き継ぎ先を拒否: {target_role_id}")
                            messages.append({"role": "user", "content": notice})
                            auto_steps += 1
                            if auto_steps >= MAX_AUTO_STEPS:
                                print(f"\n[SYSTEM] 物理防御作動: 自動実行が上限({MAX_AUTO_STEPS}回)到達。強制停止します。")
                                report = _final_report(messages, model_name, debug_mode, chat_log_file)
                                if is_nested:
                                    cleaned_up = True
                                    return teardown(report or "無効な引き継ぎ先の指定が続いたため、途中で強制停止しました。")
                                break
                            continue
                        print(f"\n[HANDOFF] {target_role_id}役を呼び出します。理由: {handoff.get('reason') or '(不明)'}")
                        append_role_transition(
                            log_path, "handoff", this_role_id, target_role_id,
                            f"理由: {handoff.get('reason') or '(不明)'}\n指示: {handoff['instructions']}",
                        )

                        full_instructions = build_handoff_brief(
                            role_tool_names(BASE_DIR, target_role_id),
                            root_request,
                            render_recent_turns(messages),
                            handoff["instructions"],
                        )

                        try:
                            sub_summary = invoke_role(
                                BASE_DIR, target_role_id, server_proc, full_instructions, log_path,
                                call_chain=chain + [this_role_id], root_request=root_request,
                            )
                        except Exception as e:
                            print(f"\n[ERROR] {target_role_id}役の実行中に問題が発生しました: {e}")
                            sub_summary = f"{target_role_id}役の実行中にエラーが発生し、中断しました: {e}"
                        finally:
                            # invoke_role側で自分のモデルは既にアンロード済みなので、
                            # ここで自分のモデルを再ロードしてから続行する
                            # （chat_agent.pyのrun_role_and_waitと同じパターン）。
                            warmup_model(model_name)

                        append_role_transition(log_path, "return", target_role_id, this_role_id, sub_summary or "(報告なし)")
                        if sub_summary:
                            note = f"[{target_role_id}役からの報告]\n{sub_summary}"
                            messages.append({"role": "tool", "content": note})
                            print(f"\n{note}\n")

                        # 物理防御2と同じ扱い: handoffも1ステップとして数える
                        # （でなければ「呼び合うだけで何もしない」連鎖がauto_stepsを
                        # 消費せずに続いてしまう）。
                        auto_steps += 1
                        if auto_steps >= MAX_AUTO_STEPS:
                            print(f"\n[SYSTEM] 物理防御作動: 自動実行が上限({MAX_AUTO_STEPS}回)到達。強制停止します。")
                            report = sub_summary or f"自動実行が上限({MAX_AUTO_STEPS}回)に達したため、途中で強制停止しました。"
                            if is_nested:
                                print("\n[RETURN] 自動実行の上限に達したため、呼び出し元に会話を戻します。")
                                append_session_log(log_path, "System", f"上限到達により呼び出し元に戻る: {report}")
                                cleaned_up = True
                                return teardown(report)
                            break

                        print("\n[SYSTEM] 引き継ぎ結果をAIにフィードバックして解析中...")
                        continue

                    return_info = return_to_caller_from_tool_calls(effective_tool_calls)
                    if return_info:
                        cleaned_up = True
                        if is_nested:
                            print(f"\n[RETURN] 呼び出し元に会話を戻します。")
                            append_session_log(log_path, "System", f"呼び出し元に戻る: {return_info['summary']}")
                            return teardown(return_info["summary"])
                        else:
                            print(f"\n[完了] {return_info['summary']}")
                            append_session_log(log_path, "System", f"完了: {return_info['summary']}")
                            return teardown()
                    break

                actions = actions[:1]

                # 物理防御1: 同一アクション連続検知
                current_signature = json.dumps(actions[0], sort_keys=True, ensure_ascii=False)
                if current_signature == last_action_signature:
                    same_action_count += 1
                else:
                    same_action_count = 0
                last_action_signature = current_signature

                if same_action_count >= MAX_SAME_ACTION:
                    print(f"\n[SYSTEM] 物理防御作動: 同一アクションが{MAX_SAME_ACTION + 1}回連続。強制停止します。")
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM NOTICE] 同じアクションが繰り返し検出されました。"
                                   "その操作は既に成功済みです。これ以上アクションを出さず、"
                                   "行った変更内容を日本語で簡潔に報告して終了してください。"
                    })
                    report = _final_report(messages, model_name, debug_mode, chat_log_file)
                    if is_nested:
                        # [IMPORTANT] ここでbreakして自分の入力待ちに戻ると、壊れた文脈のまま
                        # ユーザーの次の発言を処理してしまい、同じ暴走を繰り返す。
                        # nested実行中なら、暴走を検知した時点で強制的に呼び出し元へ戻す。
                        print("\n[RETURN] 暴走を検知したため、呼び出し元に会話を戻します。")
                        append_session_log(log_path, "System", f"暴走検知により呼び出し元に戻る: {report}")
                        cleaned_up = True
                        return teardown(report or "同じ操作が繰り返されたため、途中で強制停止しました。")
                    break

                # 物理防御1.5: remember連投検知（内容が毎回違っても、rememberばかりで
                # 本来の仕事(実作業/return_to_caller)が進んでいなければ暴走とみなす）
                if actions[0]["type"] == "remember":
                    remember_streak += 1
                else:
                    remember_streak = 0

                if remember_streak >= MAX_REMEMBER_STREAK:
                    print(f"\n[SYSTEM] 物理防御作動: rememberが{MAX_REMEMBER_STREAK}回連続。強制停止します。")
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM NOTICE] rememberの連続呼び出しが検出されました。"
                                   "rememberはメモ帳ではありません。これ以上rememberを呼ばず、"
                                   "今すぐ本来の作業（実際の作業か、それが既に十分なら"
                                   "return_to_caller）を行ってください。"
                    })
                    report = _final_report(messages, model_name, debug_mode, chat_log_file)
                    if is_nested:
                        print("\n[RETURN] rememberの連投を検知したため、呼び出し元に会話を戻します。")
                        append_session_log(log_path, "System", f"remember連投検知により呼び出し元に戻る: {report}")
                        cleaned_up = True
                        return teardown(report or "rememberが連続で呼ばれたため、途中で強制停止しました。")
                    break

                feedback_parts = []
                for act in actions:
                    if act["type"] == "command":
                        res = run_command(act["content"], exec_mode)
                        MAX_FEEDBACK = 14000
                        if len(res) > MAX_FEEDBACK:
                            res = res[:MAX_FEEDBACK] + "\n...(長いため省略)"
                        feedback_parts.append(f"[SYSTEM COMMAND OUTPUT for '{act['content']}']\n{res}")
                    elif act["type"] == "edit":
                        res = run_edit(act, exec_mode)
                        feedback_parts.append(res)
                    elif act["type"] == "write":
                        res = run_write(act, exec_mode)
                        feedback_parts.append(res)
                    elif act["type"] == "read":
                        res = run_read(act)
                        MAX_FEEDBACK = 14000
                        if len(res) > MAX_FEEDBACK:
                            res = res[:MAX_FEEDBACK] + "\n...(長いため省略)"
                        feedback_parts.append(res)
                    elif act["type"] == "remember":
                        res = run_remember(act)
                        feedback_parts.append(res)
                    elif act["type"] == "search":
                        print(f"\n[SEARCH REQUESTED] クエリ: {act['query']}")
                        res = web_search(act["query"])
                        MAX_FEEDBACK = 14000
                        if len(res) > MAX_FEEDBACK:
                            res = res[:MAX_FEEDBACK] + "\n...(長いため省略)"
                        feedback_parts.append(res)
                    elif act["type"] == "fetch_url":
                        print(f"\n[FETCH REQUESTED] URL: {act['url']}")
                        res = fetch_url(act["url"])
                        MAX_FEEDBACK = 14000
                        if len(res) > MAX_FEEDBACK:
                            res = res[:MAX_FEEDBACK] + "\n...(長いため省略)"
                        feedback_parts.append(res)
                    elif act["type"] == "summarize":
                        print("\n[SUMMARIZE REQUESTED]")
                        res = summarize_text(act["text"], model_name, act.get("instruction"))
                        feedback_parts.append(f"[SUMMARY]\n{res}")
                    elif act["type"] == "calculate":
                        print(f"\n[CALCULATE REQUESTED] {act['expression']}")
                        res = calculate(act["expression"])
                        feedback_parts.append(res)
                    elif act["type"] == "git_diff":
                        print("\n[GIT DIFF SUMMARY REQUESTED]")
                        res = git_diff_summary(WORK_DIR)
                        MAX_FEEDBACK = 14000
                        if len(res) > MAX_FEEDBACK:
                            res = res[:MAX_FEEDBACK] + "\n...(長いため省略)"
                        feedback_parts.append(res)

                feedback = "\n\n".join(feedback_parts)
                messages.append({"role": "tool", "content": feedback})
                append_session_log(log_path, "Tool", feedback)
                if chat_log_file:
                    with open(chat_log_file, "a", encoding="utf-8") as f:
                        f.write(f"\n[Action Results]\n{feedback}\n")

                # 物理防御1.7: 失敗連続検知（型は同じでも引数を変えながら
                # 失敗し続けるハルシネーション連投を検知する。実機で
                # Review役がread_fileの存在しないパスを毎回微妙に変えながら
                # 12回リトライし続けた不具合を確認した。同一アクション連続検知
                # (物理防御1)は引数が完全一致でないと発動しないため、このケースは
                # すり抜けていた）。
                if is_failure_feedback(feedback):
                    failure_streak += 1
                else:
                    failure_streak = 0

                if failure_streak >= MAX_FAILURE_STREAK:
                    print(f"\n[SYSTEM] 物理防御作動: 同じ種類の操作が{MAX_FAILURE_STREAK}回連続で失敗。強制停止します。")
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM NOTICE] 同じ種類の操作が連続して失敗しています。"
                                   "存在しないパスを推測で試すのをやめ、別の方法を試すか、"
                                   "これ以上できることが無ければreturn_to_callerで"
                                   "ここまでの状況を具体的に報告してください。"
                    })
                    report = _final_report(messages, model_name, debug_mode, chat_log_file)
                    if is_nested:
                        print("\n[RETURN] 失敗の連投を検知したため、呼び出し元に会話を戻します。")
                        append_session_log(log_path, "System", f"失敗連投検知により呼び出し元に戻る: {report}")
                        cleaned_up = True
                        return teardown(report or "同じ種類の操作が失敗し続けたため、途中で強制停止しました。")
                    break

                # 物理防御2: 自動継続の絶対上限
                auto_steps += 1
                if auto_steps >= MAX_AUTO_STEPS:
                    print(f"\n[SYSTEM] 物理防御作動: 自動実行が上限({MAX_AUTO_STEPS}回)到達。強制停止します。")
                    if is_nested:
                        messages.append({
                            "role": "user",
                            "content": "[SYSTEM NOTICE] 自動実行の上限に達しました。これ以上アクションを"
                                       "出さず、ここまでに行った内容を日本語で簡潔に報告して終了してください。"
                        })
                        report = _final_report(messages, model_name, debug_mode, chat_log_file)
                        print("\n[RETURN] 自動実行の上限に達したため、呼び出し元に会話を戻します。")
                        append_session_log(log_path, "System", f"上限到達により呼び出し元に戻る: {report}")
                        cleaned_up = True
                        return teardown(report or f"自動実行が上限({MAX_AUTO_STEPS}回)に達したため、途中で強制停止しました。")
                    break

                print("\n[SYSTEM] 実行結果をAIにフィードバックして解析中...")
                continue
    finally:
        if not cleaned_up:
            print("\n[WARN] 想定外の形でセッションが終了しました。後片付け（VRAM解放）だけは行います。")
            try:
                teardown()
            except Exception as cleanup_err:
                print(f"[ERROR] 後片付けにも失敗しました: {cleanup_err}")


def _final_report(messages, model_name, debug_mode, chat_log_file):
    """
    暴走停止後、最終報告を1回だけ生成させる（アクションは実行しない）。
    報告文字列を返す（失敗時はNone）。呼び出し元は、nested実行中ならこれを
    そのままreturn_to_callerの要約として使い、呼び出し元に自動的に会話を戻す。
    """
    # [NOTE] 暴走停止までに調査対象を無限に広げてしまった等で会話が非常に
    # 長くなっているケースでは、そのまま全履歴を送るとnum_ctx=16384でも
    # サーバー側の処理が重すぎて失敗する（HTTP 500）ことを実機で確認した
    # （Arc A770でのVRAM不足が有力な原因）。暴走を検知した直後の呼び出しで
    # あることが確定しているここでは、system prompt + 直近のやり取りだけに
    # 絞ることで、最終報告のリクエスト自体は会話がどれだけ長くなっていても
    # 常に軽量であることを保証する（プロンプト側の「調査範囲を広げすぎない」
    # 指示に頼らない、コード側の保険）。
    MAX_REPORT_MESSAGES = 10
    report_messages = messages
    if len(messages) > MAX_REPORT_MESSAGES + 1:
        system_msgs = [m for m in messages if m.get("role") == "system"]
        recent = messages[-MAX_REPORT_MESSAGES:]
        report_messages = system_msgs + recent

    payload = json.dumps({
        "model": model_name,
        "messages": report_messages,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "options": {"num_ctx": 16384, "temperature": 0.3}
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat", data=payload,
        headers={"Content-Type": "application/json"})
    print("\n--- 最終報告 ---")
    try:
        report, _ = stream_chat_response(req, debug_mode, raw_dump=False)
        print()
        if chat_log_file:
            with open(chat_log_file, "a", encoding="utf-8") as f:
                f.write(f"\n[Final Report]\n{report}\n")
        # toolsを渡していない呼び出しだが、モデルがtool呼び出し風のJSONを
        # そのまま書いてしまうことがある。summaryが拾えればそちらを使う。
        forced_return = return_to_caller_from_tool_calls(tool_call_from_content(report))
        if forced_return:
            return forced_return["summary"]
        return strip_think_blocks(report) or None
    except Exception as e:
        # [NOTE] 以前はurllib.error.URLErrorだけを捕まえていたが、暴走停止までに
        # 会話が非常に長くなっているケース（例: 調査対象を無限に広げてしまった後）
        # では、サーバー側がプロンプト処理中に落ちてHTTP 500を返した後、接続が
        # 途中で切れて urllib.error.URLError ではない例外（ConnectionResetError等）
        # になることを実機で確認した。narrow過ぎる例外捕捉だと、この場合エラーが
        # ここで捕まらずstart_interactive_chat側のtry/finallyまで伝播してしまい、
        # 何が起きたかの手がかり（このprint）が失われる。広めに捕まえて必ず
        # ログ・画面に残す。
        print(f"\n[ERROR] 最終報告の生成に失敗: {e}")
        return None


def select_exec_mode() -> str:
    global CURRENT_MODE
    os.system("cls" if os.name == "nt" else "clear")
    print("===================================================")
    print(" 承認モードの変更")
    print("===================================================")
    print(" [1] Safe Auto   [2] Strict   [3] Full Auto")
    print("===================================================")
    choice = input("モード番号 (1-3): ").strip()
    if choice == "1":
        CURRENT_MODE = "safe"
    elif choice == "2":
        CURRENT_MODE = "strict"
    elif choice == "3":
        CURRENT_MODE = "full"
    return CURRENT_MODE


def select_work_dir():
    """作業ディレクトリを変更する。"""
    global WORK_DIR
    os.system("cls" if os.name == "nt" else "clear")
    print("===================================================")
    print(" 作業ディレクトリの変更")
    print("===================================================")
    print(f" 現在: {WORK_DIR}")
    print(" AIが実行する全コマンドは、このディレクトリで動きます。")
    print("===================================================")
    raw_wd = input(" 新しいパス (空Enterで変更なし): ").strip().strip('"').strip("'")
    if raw_wd:
        if os.path.isdir(raw_wd):
            WORK_DIR = os.path.abspath(raw_wd)
            print(f" [OK] 作業ディレクトリを設定しました: {WORK_DIR}")
        else:
            print(f" [WARN] 指定パスが存在しません。変更しません: {WORK_DIR}")
    time.sleep(1)
    return WORK_DIR


def _select_model_and_run(exec_mode, log_file, debug_mode, raw_dump, header):
    """モデル選択→セッション起動の共通処理"""
    print(f"\n--- {header} ---")
    for key, (label, _) in MODELS.items():
        print(f" [{key}] {label}")
    sub_choice = input("モデル番号を選択してください (1-7): ").strip()
    sub_choice = unicodedata.normalize('NFKC', sub_choice).strip()
    if sub_choice not in MODELS:
        print("[INFO] 無効な選択です。メニューに戻ります。")
        return
    server_proc = run_server(log_file)
    _, model_name = MODELS[sub_choice]
    start_interactive_chat(model_name, exec_mode, server_proc, debug_mode=debug_mode, raw_dump=raw_dump)
    input("\nメニューに戻るには何かキーを押してください...")


def _test_mode_launch(exec_mode, log_file):
    """
    テスト用起動: scripts.config.MODELSの決め打ちリストではなく、
    実際にOllamaへpull済みのモデルを一覧して選ばせる。
    num_ctx（コンテキスト長）も自由に指定できる。
    """
    print("\n--- テストモード起動 ---")
    server_proc = run_server(log_file)

    models = list_installed_models()
    if not models:
        print("[WARN] インストール済みモデルを取得できませんでした。メニューに戻ります。")
        input("\nメニューに戻るには何かキーを押してください...")
        return

    print("インストール済みモデル:")
    for i, name in enumerate(models, 1):
        print(f" [{i}] {name}")
    raw_choice = unicodedata.normalize('NFKC', input("番号、またはモデル名を直接入力: ").strip())
    if raw_choice.isdigit() and 1 <= int(raw_choice) <= len(models):
        model_name = models[int(raw_choice) - 1]
    elif raw_choice:
        model_name = raw_choice
    else:
        print("[INFO] 未入力のため中止します。メニューに戻ります。")
        input("\nメニューに戻るには何かキーを押してください...")
        return

    raw_ctx = input("num_ctx（コンテキスト長。空Enterで既定8192）: ").strip()
    num_ctx = int(raw_ctx) if raw_ctx.isdigit() else 8192

    print(f"[TEST MODE] model={model_name} / num_ctx={num_ctx}")
    start_interactive_chat(model_name, exec_mode, server_proc, num_ctx=num_ctx)
    input("\nメニューに戻るには何かキーを押してください...")


def main():
    """
    単体起動（例: python arc_agent.py）専用のメニュー駆動フロー。
    他の役から入れ子で呼ばれる時はこのmain()は通らず、
    scripts.dispatch.invoke_role() がstart_interactive_chat()を直接呼ぶ。
    """
    global CURRENT_MODE, WORK_DIR
    log_file = os.path.join(BASE_DIR, "ollama_server.log")
    setup_environment()

    # ★ 作業ディレクトリの初期設定（AIコマンドはここで実行される）
    print("===================================================")
    print(" 作業ディレクトリの設定")
    print("===================================================")
    print(f" 現在の既定: {WORK_DIR}")
    print(" AIが実行する全コマンドは、このディレクトリで動きます。")
    print(" （Gitリポジトリ等の対象フォルダを指定してください）")
    raw_wd = input(" 作業ディレクトリのパス (空Enterで既定のまま): ").strip().strip('"').strip("'")
    if raw_wd:
        if os.path.isdir(raw_wd):
            WORK_DIR = os.path.abspath(raw_wd)
            print(f" [OK] 作業ディレクトリを設定しました: {WORK_DIR}")
        else:
            print(f" [WARN] 指定パスが存在しません。既定のまま使用します: {WORK_DIR}")
    time.sleep(1)

    # 起動時: 既に動いているOllamaを全部掃除する
    startup_cleanup()

    while True:
        cleanup_processes()
        os.system("cls" if os.name == "nt" else "clear")
        print("===================================================")
        print("   Official Ollama - Intel Arc A770 Agent UI")
        print("===================================================")
        for key, (label, _) in MODELS.items():
            print(f" [{key}] {label}")
        print("---------------------------------------------------")
        print(" [m] 承認モードの変更")
        print(" [w] 作業ディレクトリの変更")
        print(" [l] ログモード (会話をファイル保存＋生ストリーム表示)")
        print(" [d] 生ダンプモード (フィールド切り替え式・都度出力)")
        print(" [t] テストモード (インストール済みモデル+num_ctxを自由に指定)")
        print(" [0] 終了")
        print("===================================================")
        print(f" [Mode]      Current: {EXEC_MODES[CURRENT_MODE]}")
        print(f" [WorkDir]   {WORK_DIR}")
        print(f" [KeepAlive] アイドル {KEEP_ALIVE} で自動解放")
        print(f" [Log Path]  {log_file}")
        print(f" [Prompt]    Loaded from '{PROMPT_FILE}'")
        print("===================================================")

        raw_choice = input("メニュー番号を入力 (1-7 / m / w / l / d / t / 0=終了): ")
        choice = unicodedata.normalize('NFKC', raw_choice).strip().lower()

        if choice in MODELS:
            server_proc = run_server(log_file)
            _, model_name = MODELS[choice]
            start_interactive_chat(model_name, CURRENT_MODE, server_proc, debug_mode=False, raw_dump=False)
            input("\nメニューに戻るには何かキーを押してください...")

        elif choice == "m":
            select_exec_mode()

        elif choice == "w":
            select_work_dir()

        elif choice == "l":
            _select_model_and_run(CURRENT_MODE, log_file,
                                  debug_mode=True, raw_dump=False,
                                  header="ログモード用モデル選択")

        elif choice == "d":
            _select_model_and_run(CURRENT_MODE, log_file,
                                  debug_mode=True, raw_dump=True,
                                  header="生ダンプモード用モデル選択")

        elif choice == "t":
            _test_mode_launch(CURRENT_MODE, log_file)

        elif choice == "0":
            # ★ 終了時: VRAM解放してから全プロセスを掃除
            unload_all_models()
            cleanup_processes()
            print("[INFO] 終了しました。")
            break


if __name__ == "__main__":
    main()
