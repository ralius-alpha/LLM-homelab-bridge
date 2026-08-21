# -*- coding: utf-8 -*-
"""
役(role)の定義を、コードではなくファイルから読み込む。

役 = `roles/<role_id>/role.json`（モデル・使うtool名の一覧・専門性・引き継ぎ先）+
     `roles/<role_id>/prompt.txt`（システムプロンプト）
の1セット。この2ファイルを `roles/<role_id>/` に置くだけで新しい役を定義できる。

role.json の主なフィールド:
  - "model": 使うOllamaモデル名
  - "tools": tool名の文字列リスト（scripts.tools.TOOL_REGISTRYで実際のtool定義に変換）
  - "specialty": この役の専門性（1行）。handoff_to_roleのtool説明文と、
    自分自身のプロンプトへの自己認識の注入に使う。
  - "can_handoff_to": この役が引き継げる先の role_id のリスト。空/省略なら
    handoff_to_roleは付与されない。
  - "module": この役を実際に呼び出す時にimportするPythonモジュール名
    （scripts.dispatch.invoke_roleが使う）。引き継ぎ先として呼ばれる役にのみ必要。

dispatch（tool呼び出しの実処理）はここでは関知しない。呼び出し元が持つ。
役を実際に起動する側の関数シグネチャの取り決めは scripts/dispatch.py と
README を参照。
"""

import os
import json

from scripts.tools import TOOL_REGISTRY, build_handoff_tool

ROLES_DIRNAME = "roles"
ROLE_CONFIG_FILENAME = "role.json"
ROLE_PROMPT_FILENAME = "prompt.txt"


def list_role_ids(base_dir):
    """roles/ 直下で role.json を持つディレクトリ名の一覧を返す。"""
    roles_dir = os.path.join(base_dir, ROLES_DIRNAME)
    if not os.path.isdir(roles_dir):
        return []
    return sorted(
        name for name in os.listdir(roles_dir)
        if os.path.isfile(os.path.join(roles_dir, name, ROLE_CONFIG_FILENAME))
    )


def _peek_role_meta(base_dir, role_id):
    """
    role.jsonのdisplay_name/specialty/toolsだけを軽く覗き見る（tool名の検証等はしない）。
    handoff_to_roleのtool説明文や自己認識プロンプトに、引き継ぎ先の情報を
    埋め込むために使う。toolsは「引き継ぎ先がsearch_web等の特定skillを
    持っているか」の判定（_inject_skill_hint）に使う。
    """
    config_path = os.path.join(base_dir, ROLES_DIRNAME, role_id, ROLE_CONFIG_FILENAME)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return {
        "role_id": role_id,
        "display_name": config.get("display_name", role_id),
        "specialty": config.get("specialty", ""),
        "tools": config.get("tools", []),
    }


def _inject_specialty(prompt, specialty, targets):
    """自分の専門性と、引き継ぎ先の一覧をプロンプト末尾に注入する。両方無ければ何もしない。"""
    if not specialty and not targets:
        return prompt
    block = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "【自分の専門性】",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if specialty:
        block.append(f"あなたの専門: {specialty}")
    if targets:
        block.append(
            "これ以外の専門性が必要だと判断したら、無理に自分でやろうとせず、"
            "handoff_to_role で適切な役に引き継ぐこと。できるだけ自分だけで"
            "抱え込まないこと。"
        )
        block.append("引き継ぎ先の候補:")
        for t in targets:
            block.append(f"- {t['role_id']}（{t['display_name']}）: {t['specialty']}")
    return prompt + "\n\n" + "\n".join(block)


def _inject_skill_hint(prompt, tool_names, targets):
    """
    自分がsearch_webを持っておらず、引き継ぎ先の中にsearch_webを持つ役がいる場合、
    「調べて」系の依頼をユーザーに投げ返さず必ずhandoff_to_roleで引き継ぐよう
    明示的に注入する。

    [NOTE] これは全roleのprompt.txtに同じ内容を書き写すのを避けるための共通の
    仕組み。search_webを持たない役が増えても、各prompt.txtを個別に編集する
    必要はなく、ここが自動的に対応する。雑談役でこの注意が無いと、モデルが
    「私は直接検索できません」と答えるだけでhandoff_to_roleを呼ばずに終わる
    不具合を実機で繰り返し確認した（ユーザーに検索手順を教えて代わりにやらせる
    のと同種のアンチパターン）。
    """
    if "search_web" in tool_names:
        return prompt
    search_capable = [t for t in targets if "search_web" in t.get("tools", [])]
    if not search_capable:
        return prompt
    # reviewは読み取り専用の調査役なので、居れば例として優先的に使う
    # （search_webを持つ役が複数ある場合、execute等の変更系の役より自然）。
    preferred = next((t for t in search_capable if t["role_id"] == "review"), None)
    first_target = (preferred or search_capable[0])["role_id"]
    block = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "【ネット検索が必要な依頼への対応（重要）】",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "あなた自身はsearch_webというtoolを持っていない。ユーザーの依頼に"
        "「調べて」「検索して」「今日の天気は」「最新の〜は」のような、"
        "あなた自身の知識だけでは答えられない・古い可能性がある情報を"
        "求める言葉が含まれていたら、「自分では検索できません」で会話を"
        "終わらせないこと。必ずtool呼び出し機能でhandoff_to_role（"
        f"role_id=\"{first_target}\"）を実際に呼び出して調べさせること。"
        "「〜と検索してみてください」のようにユーザー自身に検索させる案内で"
        "終わるのは誤り（それはあなたの仕事であり、ユーザーにやらせるものではない）。",
        "正しい返答の一例（これは説明用の記述であり、そのまま文章として書き写す"
        "ものではない。実際にはtool呼び出し機能そのものを使うこと）:",
        "```json",
        '{"name": "handoff_to_role", "arguments": {"role_id": "%s", '
        '"instructions": "（ユーザーが調べてほしい内容を具体的に）", '
        '"reason": "自分はsearch_webを持っていないため"}}' % first_target,
        "```",
    ]
    return prompt + "\n\n" + "\n".join(block)


def load_role(base_dir, role_id):
    """
    roles/<role_id>/ から役の定義を読み込む。
    戻り値: {"display_name", "model", "tools"（tool定義のlist）, "prompt",
             "specialty", "can_handoff_to", "module"}
    """
    role_dir = os.path.join(base_dir, ROLES_DIRNAME, role_id)
    config_path = os.path.join(role_dir, ROLE_CONFIG_FILENAME)
    prompt_path = os.path.join(role_dir, ROLE_PROMPT_FILENAME)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt = f.read().strip()

    tool_names = config.get("tools", [])
    unknown = [name for name in tool_names if name not in TOOL_REGISTRY]
    if unknown:
        raise ValueError(
            f"roles/{role_id}/role.json: 未知のtool名です: {unknown}. "
            f"使えるtool名: {sorted(TOOL_REGISTRY)}"
        )

    tools = [TOOL_REGISTRY[name] for name in tool_names]

    can_handoff_to = config.get("can_handoff_to", [])
    targets = [_peek_role_meta(base_dir, rid) for rid in can_handoff_to]
    if targets:
        tools.append(build_handoff_tool(targets))

    specialty = config.get("specialty", "")
    prompt = _inject_specialty(prompt, specialty, targets)
    prompt = _inject_skill_hint(prompt, tool_names, targets)

    return {
        "display_name": config.get("display_name", role_id),
        "model": config["model"],
        "tools": tools,
        "prompt": prompt,
        "specialty": specialty,
        "can_handoff_to": can_handoff_to,
        "module": config.get("module"),
    }
