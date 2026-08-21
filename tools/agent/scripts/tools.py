# -*- coding: utf-8 -*-
"""
Ollama の tool calling (function calling) 用のツール定義と、
返ってきた tool_calls をエージェント内部のアクション形式に変換する処理。

[NOTE] 内部アクション形式 {"type": "command"/"edit"/"read", ...} は
       旧・正規表現タグ抽出方式と互換にしてある。
       run_command / run_edit / run_read 側の変更は不要。

[NOTE] モデルによっては "tools" capability を名乗っていても、
       実際には構造化された message.tool_calls を返さず、
       {"name": ..., "arguments": {...}} 形式のJSONをそのまま
       content に出力するだけの場合がある（例: このリポジトリの
       検証時点の qwen2.5-coder / Ollama 0.32.6 組み合わせ）。
       tool_call_from_content() はその救済フォールバック。
"""

import json
import re

REMEMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "役をまたいで絶対に忘れてはいけないと判断した事実・決定事項を、"
            "全役共通の共有メモに書き残す。世間話や、その場限りの作業手順は書かないこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "書き残す内容（短く、要点だけ）",
                }
            },
            "required": ["note"],
        },
    },
}

RETURN_TO_CALLER_TOOL = {
    "type": "function",
    "function": {
        "name": "return_to_caller",
        "description": (
            "依頼された作業が完了し、これ以上コマンド実行やファイル編集が不要になったと"
            "判断した時に呼ぶ。自分を呼び出した側に会話を戻す。作業の途中で呼ばないこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "何をして、結果がどうなったかの要約。"
                        "呼び出し元はこの会話の履歴を見られないため、これだけ読んで"
                        "ユーザーに説明できるように具体的に書くこと。"
                    ),
                }
            },
            "required": ["summary"],
        },
    },
}

EXECUTE_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_command",
        "description": (
            "Windows PowerShellでコマンドを1つ実行する。"
            "調査・ファイル一覧・検索・コピー・移動などに使う。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "実行するPowerShellコマンド（1個だけ）",
                }
            },
            "required": ["command"],
        },
    },
}

EDIT_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "既存ファイルの一部を SEARCH/REPLACE 形式でピンポイント編集する。"
            "コード改造は必ずこれを使うこと。全文書き換えは禁止。"
            "SEARCH には read_file 等で実際に確認した、実在する行のみを書く。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "編集対象ファイルの実際のパス",
                },
                "search": {
                    "type": "string",
                    "description": "変更したい既存の行（一意に特定できる範囲で、実在する行のみ）",
                },
                "replace": {
                    "type": "string",
                    "description": "置き換え後の内容",
                },
            },
            "required": ["file", "search", "replace"],
        },
    },
}

WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "新しいファイルを作成する、または既存ファイルを全文書き換えする。"
            "既存ファイルの一部だけを直す時は必ずedit_fileを使うこと（write_fileでの"
            "全文書き換えは既存ファイルには使わない）。write_fileは主に「まだ存在しない"
            "ファイルを新規作成する」時に使う。既存ファイルを上書きする場合はバックアップを取る。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "作成/上書きするファイルの実際のパス",
                },
                "content": {
                    "type": "string",
                    "description": "書き込む内容（ファイルの中身そのもの、全文）",
                },
            },
            "required": ["file", "content"],
        },
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "ファイルをUTF-8で読み、行番号付きで返す。"
            "PowerShellのGet-Contentは文字化けの恐れがあるため、"
            "ファイルを読む際は必ずこちらを使うこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "読み込むファイルのパス",
                }
            },
            "required": ["file"],
        },
    },
}

SEARCH_WEB_TOOL = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "インターネットを検索し、上位の結果（タイトル・URL・要約）を返す。"
            "PC上のファイルには無い最新情報や、一般的な知識・仕様を調べたい時に使う。"
            "副作用は無い（何も変更しない）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "検索キーワード",
                }
            },
            "required": ["query"],
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "指定したURLの内容を取得し、本文をテキストとして返す。"
            "search_webの結果に出てきたURLの中身を実際に読みたい時に使う。"
            "副作用は無い（何も変更しない）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "取得するURL（http://かhttps://で始まるもの）",
                }
            },
            "required": ["url"],
        },
    },
}

SUMMARIZE_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "summarize_text",
        "description": (
            "長い文章（ファイルの内容・検索結果・会話の抜粋など）を、"
            "要点を保ったまま短く要約する。副作用は無い（何も変更しない）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要約したい文章そのもの",
                },
                "instruction": {
                    "type": "string",
                    "description": "要約の観点（例: 'エラーの原因に絞って要約して'）。省略可",
                },
            },
            "required": ["text"],
        },
    },
}

CALCULATE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "四則演算・べき乗の数式を正確に計算する。桁数の大きい計算や、"
            "暗算で間違えやすい計算をする時は、自分で計算せず必ずこれを使うこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "計算したい数式（例: '(12345 + 678) * 2'）",
                }
            },
            "required": ["expression"],
        },
    },
}

GIT_DIFF_SUMMARY_TOOL = {
    "type": "function",
    "function": {
        "name": "git_diff_summary",
        "description": (
            "現在の作業ディレクトリのgit status/diffをまとめて取得する。"
            "コミット前の変更内容を確認したい時に使う。副作用は無い（何も変更しない）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# 「スキル」（副作用が無い/軽い、単独で完結する能力。README「役とスキルの違い」参照）
# 1つにつき、「いつ使うべきか／使うべきでないか」を短くまとめたノート。
# tool定義の"description"はOllamaに渡すJSONスキーマの一部であり、そこに全部
# 詰め込むと肥大化する上、role.jsonでこのskillを持たない役には一切見えない
# （toolsに入っていないtoolの説明はモデルに渡らないため）。ここは別枠にして
# scripts/role_loader.pyがロール読み込み時に、(a) そのskillを持つ役には
# 「使いどころ」として、(b) 持たないが引き継ぎ先が持つ役には「該当スキルが
# 必要な依頼が来たらhandoff_to_roleで引き継げ」として、両方に自動注入する。
# [NOTE] 新しいskillを追加する時は、実装(scripts/skills.py)・スキーマ(本ファイル
# 上部のTOOL定義+TOOL_REGISTRY)に加えて、必ずここにも1エントリ追加すること
# （README「新しいスキルを追加するには」参照）。追加を忘れると、そのskillを
# 持たない役は「そんなことはできません」と会話を終わらせるだけで、
# handoff_to_roleで引き継ぐべき場面だと気づけない（雑談役でsearch_webに
# ついて実機で確認した不具合と同じパターンが、他のskillでも起こり得る）。
SKILL_USAGE_NOTES = {
    "search_web": (
        "使うべき場合: 「調べて」「検索して」「今日の〜は」「最新の〜は」など、"
        "自分の知識だけでは答えられない・古い可能性がある情報が要る時。"
        "使うべきでない場合: PC上のファイルで完結する調査（read_file/execute_command）や、"
        "一般的な設計相談・雑談。"
    ),
    "fetch_url": (
        "使うべき場合: search_webの結果に出てきたURLや、ユーザーから提示されたURLの"
        "中身を実際に読みたい時。"
        "使うべきでない場合: まだURLが手元に無い時（先にsearch_webでURLを見つける）。"
    ),
    "summarize_text": (
        "使うべき場合: read_file/fetch_url/search_webで読んだ内容が長すぎて、"
        "そのまま扱うと会話が長くなりすぎる時。"
        "使うべきでない場合: 既に十分短い内容や、要約ではなく原文の正確な引用が必要な時。"
    ),
    "calculate": (
        "使うべき場合: 桁数の大きい計算や、暗算で間違えやすい四則演算・べき乗の計算をする時。"
        "使うべきでない場合: 変数を含む式やプログラムの実行が必要な時"
        "（calculateは四則演算・べき乗のみで、任意のコード実行はできない）。"
    ),
    "git_diff_summary": (
        "使うべき場合: コミット前に変更内容を確認したい時、レビューでどこが"
        "変わったか把握したい時。"
        "使うべきでない場合: gitで管理されていない一時ファイルの内容確認"
        "（そちらはread_fileを使う）。"
    ),
}


def build_handoff_tool(targets):
    """
    handoff_to_role のtool定義を動的に作る。
    targets: [{"role_id", "display_name", "specialty"}, ...]（role_loader.pyが
    role.jsonの "can_handoff_to" から解決して渡す）。
    role_idごとに専用のtoolを固定で持つのではなく、この1つのtoolに「誰を呼べるか」を
    enumとして持たせることで、役が増えてもtools.py側の変更が不要になる。
    """
    role_id_lines = "\n".join(
        f"- {t['role_id']}（{t['display_name']}）: {t['specialty']}" for t in targets
    )
    return {
        "type": "function",
        "function": {
            "name": "handoff_to_role",
            "description": (
                "会話だけでは対応できない、自分の専門外の作業だと判断した時に呼ぶ。"
                "呼び出し先はこの会話の履歴を直接は見られないため、instructionsには"
                "何をしてほしいかを、それだけ読んで分かるように具体的に書くこと。\n"
                f"引き継ぎ先の候補:\n{role_id_lines}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role_id": {
                        "type": "string",
                        "enum": [t["role_id"] for t in targets],
                        "description": "引き継ぐ先の役のID",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "引き継ぎ先への具体的な作業指示（会話の要点をまとめたもの）",
                    },
                    "reason": {
                        "type": "string",
                        "description": "なぜ自分では対応できず引き継ぎが必要なのか",
                    },
                },
                "required": ["role_id", "instructions"],
            },
        },
    }


# 名前 → tool定義。roles/<role>/role.json が "tools": ["read_file", ...] のように
# 名前だけでtoolを指定できるようにするための引き当て表（scripts/role_loader.pyが使う）。
# [NOTE] handoff_to_role はここには無い。役ごとに「誰を呼べるか」が違う動的なtoolなので、
#        role.jsonの "can_handoff_to" を見て role_loader.py が build_handoff_tool() で
#        個別に組み立てる。
TOOL_REGISTRY = {
    "remember": REMEMBER_TOOL,
    "return_to_caller": RETURN_TO_CALLER_TOOL,
    "execute_command": EXECUTE_COMMAND_TOOL,
    "edit_file": EDIT_FILE_TOOL,
    "write_file": WRITE_FILE_TOOL,
    "read_file": READ_FILE_TOOL,
    "search_web": SEARCH_WEB_TOOL,
    "fetch_url": FETCH_URL_TOOL,
    "summarize_text": SUMMARIZE_TEXT_TOOL,
    "calculate": CALCULATE_TOOL,
    "git_diff_summary": GIT_DIFF_SUMMARY_TOOL,
}


def strip_think_blocks(text: str) -> str:
    """<think>...</think> ブロック（reasoningモデルの思考過程）を取り除く。"""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think>.*", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = cleaned.replace("</think>", "")
    return cleaned.strip()


def strip_tool_call_json(text: str) -> str:
    """
    会話の抜粋から、tool呼び出しのJSONそのものを取り除き、短い説明に置き換える。

    [IMPORTANT] 引き継ぎ先に渡す「直近の会話の抜粋」(scripts.memory.
    render_recent_turns)に、呼び出し元が出した生のtool呼び出しJSONが
    そのまま載っていると、受け取った役がそれを【お手本として丸写し】する。
    実機で、雑談役の
      {"name":"handoff_to_role","arguments":{"role_id":"execute",...}}
    というJSONが抜粋に含まれた結果、引き継ぎ先のExecute役が同じJSONを
    そのまま出力して自分自身に引き継ぎ続け、MAX_CALL_DEPTHに達するまで
    モデルのロード/アンロードを繰り返す暴走を確認した。
    抜粋は「何を話したか」を伝えるためのものであり、機械向けの命令文を
    そのまま見せる必要は無いので、ここで落とす。
    """
    if not text:
        return text

    def _describe(obj):
        name = obj.get("name")
        return f"（{name} のtool呼び出しを実行）" if name else None

    def _try_parse(candidate):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        if "name" not in obj or "arguments" not in obj:
            return None
        return obj

    # 1) ```json ... ``` フェンスで囲まれたtool呼び出し
    def _replace_fence(match):
        obj = _try_parse(match.group(1).strip())
        if obj is None:
            return match.group(0)
        return _describe(obj) or match.group(0)

    text = re.sub(r"```(?:json)?\s*(\{.*?\})\s*```", _replace_fence, text, flags=re.DOTALL)

    # 2) フェンス無しで裸のまま書かれたtool呼び出し（波括弧の対応を数えて拾う）
    out = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    obj = _try_parse(text[start:i + 1])
                    out.append(_describe(obj) if obj else text[start:i + 1])
                    start = None
        elif depth == 0:
            out.append(ch)
    if start is not None:  # 閉じていない波括弧はそのまま残す
        out.append(text[start:])
    return "".join(p for p in out if p is not None).strip()


def tool_calls_to_actions(tool_calls):
    """Ollamaのtool_callsを、既存のアクション辞書形式のリストに変換する。"""
    actions = []
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        name = fn.get("name")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        if not isinstance(args, dict):
            args = {}

        if name == "execute_command":
            command = args.get("command")
            if command:
                actions.append({"type": "command", "content": command})

        elif name == "edit_file":
            if all(k in args for k in ("file", "search", "replace")):
                actions.append({
                    "type": "edit",
                    "file": args["file"],
                    "search": args["search"],
                    "replace": args["replace"],
                })

        elif name == "write_file":
            if all(k in args for k in ("file", "content")):
                actions.append({
                    "type": "write",
                    "file": args["file"],
                    "content": args["content"],
                })

        elif name == "read_file":
            file_path = args.get("file")
            if file_path:
                actions.append({"type": "read", "file": file_path})

        elif name == "remember":
            note = args.get("note")
            if isinstance(note, str) and note.strip():
                actions.append({"type": "remember", "note": note.strip()})

        elif name == "search_web":
            query = args.get("query")
            if isinstance(query, str) and query.strip():
                actions.append({"type": "search", "query": query.strip()})

        elif name == "fetch_url":
            url = args.get("url")
            if isinstance(url, str) and url.strip():
                actions.append({"type": "fetch_url", "url": url.strip()})

        elif name == "summarize_text":
            text = args.get("text")
            if isinstance(text, str) and text.strip():
                instruction = args.get("instruction")
                actions.append({
                    "type": "summarize",
                    "text": text,
                    "instruction": instruction if isinstance(instruction, str) else None,
                })

        elif name == "calculate":
            expression = args.get("expression")
            if isinstance(expression, str) and expression.strip():
                actions.append({"type": "calculate", "expression": expression.strip()})

        elif name == "git_diff_summary":
            actions.append({"type": "git_diff"})

    return actions


def _extract_json_objects(text):
    """
    テキストから、JSONオブジェクトらしき範囲を出てきた順に全て取り出す。
    ```json ... ``` フェンスが1つ以上あればそれぞれの中身を対象にする。
    フェンスが無ければ、波括弧の対応を数えてトップレベルの{...}を全て拾う
    （「最初の{〜最後の}」方式だと、1つの返答に複数のJSONブロックが
    並んだ時に全部まとめて壊れたJSONとして取れてしまうため）。
    """
    text = text.strip()
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        return [f.strip() for f in fenced if f.strip()]

    objects = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start:i + 1])
                    start = None
    return objects


_KNOWN_TOOL_NAMES = set(TOOL_REGISTRY) | {"handoff_to_role"}

_FUNC_CALL_NAME_RE = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*\{')


def _extract_function_call_style(text):
    """
    テキストから `funcName({...})` というプログラミング言語の関数呼び出し風の
    表記を探し、(name, json文字列) のペアを全て返す。モデルがtool呼び出し
    機能を使わず、こうした疑似コードを文章として書いてしまうことがある
    （例: `handoff_to_role({"role_id": "review", ...})`）ための救済フォールバック。
    誤検知を防ぐため、既知のtool名にマッチするものだけを対象にする。
    """
    results = []
    for m in _FUNC_CALL_NAME_RE.finditer(text):
        name = m.group(1)
        if name not in _KNOWN_TOOL_NAMES:
            continue
        brace_start = m.end() - 1  # '{' の位置
        depth = 0
        end = None
        for i in range(brace_start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        # 閉じ括弧の直後（空白を挟んで）が ')' であることを確認し、
        # 本当に関数呼び出しの形をしているかを見る。
        rest = text[end + 1:end + 5].lstrip()
        if not rest.startswith(")"):
            continue
        results.append((name, text[brace_start:end + 1]))
    return results


def tool_call_from_content(text):
    """
    tool_callsを返さず、{"name": ..., "arguments": {...}} 形式のJSONを
    contentにそのまま出力してしまうモデル向けの救済フォールバック。
    1つの返答に複数のtool呼び出しJSONが並ぶこともあるため、見つかった
    有効なもの全てをtool_calls形式のリストで返す。該当が無ければ None。
    """
    text = strip_think_blocks(text)
    candidates = _extract_json_objects(text)
    tool_calls = []
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        args = obj.get("arguments")
        if not name or not isinstance(args, dict):
            continue
        tool_calls.append({"function": {"name": name, "arguments": args}})

    if not tool_calls:
        # JSONの{"name":...,"arguments":{...}}形式が1つも無かった時だけ、
        # `funcName({...})` 形式も試す（通常のJSON形式が既にあるなら、
        # そちらを優先しこちらは見ない）。
        for name, json_str in _extract_function_call_style(text):
            try:
                args = json.loads(json_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            tool_calls.append({"function": {"name": name, "arguments": args}})

    return tool_calls or None


def handoff_from_tool_calls(tool_calls):
    """tool_callsからhandoff_to_roleの呼び出しを探し、{"role_id", "instructions", "reason"}を返す。無ければNone。"""
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        if fn.get("name") != "handoff_to_role":
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        if not isinstance(args, dict):
            continue
        role_id = args.get("role_id")
        if not isinstance(role_id, str) or not role_id.strip():
            continue
        instructions = args.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            continue
        reason = args.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        return {"role_id": role_id.strip(), "instructions": instructions, "reason": reason}
    return None


def return_to_caller_from_tool_calls(tool_calls):
    """tool_callsからreturn_to_callerの呼び出しを探し、{"summary"}を返す。無ければNone。"""
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        if fn.get("name") != "return_to_caller":
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        if not isinstance(args, dict):
            continue
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        return {"summary": summary}
    return None
