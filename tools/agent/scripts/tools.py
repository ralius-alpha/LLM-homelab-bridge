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

RETURN_TO_CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "return_to_chat",
        "description": (
            "依頼された作業が完了し、これ以上コマンド実行やファイル編集が不要になったと"
            "判断した時に呼ぶ。雑談役に会話を戻す。作業の途中で呼ばないこと。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "何をして、結果がどうなったかの要約。"
                        "雑談役はこの会話の履歴を見られないため、これだけ読んで"
                        "ユーザーに説明できるように具体的に書くこと。"
                    ),
                }
            },
            "required": ["summary"],
        },
    },
}

TOOLS = [
    REMEMBER_TOOL,
    RETURN_TO_CHAT_TOOL,
    {
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
    },
    {
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
    },
    {
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
    },
]

CHAT_TOOLS = [
    REMEMBER_TOOL,
    {
        "type": "function",
        "function": {
            "name": "handoff_to_execute",
            "description": (
                "会話だけでは対応できず、実際にファイルの読み書きやコマンド実行が"
                "必要な作業だと判断した時に呼ぶ。Execute役はこの会話の履歴を見られないため、"
                "instructionsには何をしてほしいかを、それだけ読んで分かるように具体的に書くこと。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": "Execute役への具体的な作業指示（会話の要点をまとめたもの）",
                    },
                    "reason": {
                        "type": "string",
                        "description": "なぜ雑談では対応できず引き継ぎが必要なのか",
                    },
                },
                "required": ["instructions"],
            },
        },
    },
]


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
    """tool_callsからhandoff_to_executeの呼び出しを探し、{"instructions", "reason"}を返す。無ければNone。"""
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        if fn.get("name") != "handoff_to_execute":
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        if not isinstance(args, dict):
            continue
        instructions = args.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            continue
        reason = args.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        return {"instructions": instructions, "reason": reason}
    return None


def return_to_chat_from_tool_calls(tool_calls):
    """tool_callsからreturn_to_chatの呼び出しを探し、{"summary"}を返す。無ければNone。"""
    for tc in tool_calls or []:
        fn = tc.get("function", {})
        if fn.get("name") != "return_to_chat":
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
