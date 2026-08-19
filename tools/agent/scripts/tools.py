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


def tool_call_from_content(text):
    """
    tool_callsを返さず、{"name": ..., "arguments": {...}} 形式のJSONを
    contentにそのまま出力してしまうモデル向けの救済フォールバック。
    1つの返答に複数のtool呼び出しJSONが並ぶこともあるため、見つかった
    有効なもの全てをtool_calls形式のリストで返す。該当が無ければ None。
    """
    candidates = _extract_json_objects(strip_think_blocks(text))
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
