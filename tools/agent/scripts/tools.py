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

TOOLS = [
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

    return actions


def _extract_json_object(text):
    """テキストからコードフェンスを剥がし、最初のJSONオブジェクトらしき範囲を取り出す。"""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start:end + 1]


def tool_call_from_content(text):
    """
    tool_callsを返さず、{"name": ..., "arguments": {...}} 形式のJSONを
    contentにそのまま出力してしまうモデル向けの救済フォールバック。
    該当しなければ None を返す。
    """
    candidate = _extract_json_object(text or "")
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    args = obj.get("arguments")
    if not name or not isinstance(args, dict):
        return None
    return [{"function": {"name": name, "arguments": args}}]
