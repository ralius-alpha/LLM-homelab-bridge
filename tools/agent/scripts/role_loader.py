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

from scripts.tools import TOOL_REGISTRY, build_handoff_tool, SKILL_USAGE_NOTES

ROLES_DIRNAME = "roles"
ROLE_CONFIG_FILENAME = "role.json"
ROLE_PROMPT_FILENAME = "prompt.txt"


def role_tool_names(base_dir, role_id):
    """
    role_id が role.json で宣言しているtool名の一覧を返す（読み込みに失敗したら空）。
    引き継ぎ指示書の冒頭に「引き継ぎ先自身の能力」を明記するために使う
    （scripts.memory.build_handoff_brief 参照）。
    can_handoff_to があれば handoff_to_role も実際には使えるので併せて載せる。
    """
    try:
        meta = _peek_role_meta(base_dir, role_id)
    except (OSError, ValueError):
        return []
    names = list(meta.get("tools", []))
    if meta.get("can_handoff_to"):
        names.append("handoff_to_role")
    return names


def roles_providing_tool(base_dir, role_ids, tool_name):
    """
    role_ids の中から、tool_name を実際に持っている役のIDだけを返す。

    雑談役などが「自分は持っていないtool」を直接呼ぼうとした時に、
    「そのtoolはどの役が持っているのか」を呼び出し元(chat_agent.py)が
    ユーザー向け/モデル向けの案内に使うためのもの。
    """
    owners = []
    for rid in role_ids or []:
        try:
            meta = _peek_role_meta(base_dir, rid)
        except (OSError, ValueError):
            continue
        if tool_name in meta.get("tools", []):
            owners.append(rid)
    return owners


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
    埋め込むために使う。toolsは「引き継ぎ先が特定skillを持っているか」の
    判定（_inject_skill_notes）に使う。
    """
    config_path = os.path.join(base_dir, ROLES_DIRNAME, role_id, ROLE_CONFIG_FILENAME)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return {
        "role_id": role_id,
        "display_name": config.get("display_name", role_id),
        "specialty": config.get("specialty", ""),
        "tools": config.get("tools", []),
        "can_handoff_to": config.get("can_handoff_to", []),
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


def _inject_skill_notes(prompt, tool_names, targets):
    """
    SKILL_USAGE_NOTES（scripts/tools.py）に載っている全skillについて、
    (a) 自分が持っているものは「使いどころ」を、
    (b) 自分が持っておらず、引き継ぎ先の誰かが持っているものは「必ず
        handoff_to_roleで引き継げ」という指示を、
    それぞれプロンプトに自動注入する。

    [NOTE] これは全roleのprompt.txtに同じ内容を書き写すのを避けるための共通の
    仕組み。新しいskillが増えても、SKILL_USAGE_NOTESに1エントリ足すだけで
    このロード処理が自動的に全roleへ反映する（各prompt.txtは個別編集不要）。
    元々はsearch_web専用のその場しのぎの分岐だった: 雑談役がsearch_webを
    持たないままユーザーに「調べて」と言われると、「私は直接検索できません」
    と答えるだけでhandoff_to_roleを呼ばずに終わる不具合を実機で繰り返し
    確認した（ユーザーに検索手順を教えて代わりにやらせるのと同種の
    アンチパターン）。これをsearch_webだけ直すのではなく、全skill共通の
    仕組みとして汎化してある。
    """
    have_lines = []
    for name in tool_names:
        note = SKILL_USAGE_NOTES.get(name)
        if note:
            have_lines.append(f"- {name}: {note}")

    missing_hints = []
    for name, note in SKILL_USAGE_NOTES.items():
        if name in tool_names:
            continue
        # reviewは読み取り専用の調査役なので、居れば引き継ぎ先の例として優先する
        # （複数の役が同じskillを持つ場合、execute等の変更系の役より自然）。
        capable = [t for t in targets if name in t.get("tools", [])]
        if not capable:
            continue
        preferred = next((t for t in capable if t["role_id"] == "review"), None)
        target_id = (preferred or capable[0])["role_id"]
        missing_hints.append((name, note, target_id))

    if not have_lines and not missing_hints:
        return prompt

    block = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "【スキルの使いどころ】",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if have_lines:
        block.append("あなた自身が使えるスキル:")
        block.extend(have_lines)
    if missing_hints:
        if have_lines:
            block.append("")
        block.append(
            "あなた自身は持っていないが、下記のようなスキルが必要だと"
            "判断したら、「自分にはできません」で会話を終わらせないこと。"
            "必ずtool呼び出し機能でhandoff_to_roleを実際に呼び出して"
            "該当の役に引き継ぐこと（ユーザー自身にやらせる案内で"
            "終わるのは誤り。それはあなたの仕事であり、ユーザーに"
            "やらせるものではない）。"
        )
        block.append(
            "[IMPORTANT] 下記のtoolを【あなたが直接呼ぶことはできない】"
            "（あなたのtool一覧に入っていない）。名前を借りて直接呼び出しても"
            "何も実行されず、依頼は果たされないまま終わる。使えるのは"
            "handoff_to_roleだけであり、引き継ぎ先の役がそのtoolを実行する。"
        )
        for name, note, target_id in missing_hints:
            block.append(f"- {name}（{target_id}役が持っている）: {note}")
        example_name, _, example_target = missing_hints[0]
        block.append(
            "正しい返答の一例（これは説明用の記述であり、そのまま文章として"
            "書き写すものではない。実際にはtool呼び出し機能そのものを"
            "使うこと）:"
        )
        block.append("```json")
        block.append(
            '{"name": "handoff_to_role", "arguments": {"role_id": "%s", '
            '"instructions": "（依頼内容を具体的に）", '
            '"reason": "自分は%sを持っていないため"}}' % (example_target, example_name)
        )
        block.append("```")
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
    prompt = _inject_skill_notes(prompt, tool_names, targets)

    return {
        "display_name": config.get("display_name", role_id),
        "model": config["model"],
        "tools": tools,
        "prompt": prompt,
        "specialty": specialty,
        "can_handoff_to": can_handoff_to,
        "module": config.get("module"),
    }
