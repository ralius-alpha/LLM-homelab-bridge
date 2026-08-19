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
    role.jsonのdisplay_name/specialtyだけを軽く覗き見る（tool名の検証等はしない）。
    handoff_to_roleのtool説明文や自己認識プロンプトに、引き継ぎ先の情報を
    埋め込むために使う。
    """
    config_path = os.path.join(base_dir, ROLES_DIRNAME, role_id, ROLE_CONFIG_FILENAME)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return {
        "role_id": role_id,
        "display_name": config.get("display_name", role_id),
        "specialty": config.get("specialty", ""),
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

    return {
        "display_name": config.get("display_name", role_id),
        "model": config["model"],
        "tools": tools,
        "prompt": prompt,
        "specialty": specialty,
        "can_handoff_to": can_handoff_to,
        "module": config.get("module"),
    }
