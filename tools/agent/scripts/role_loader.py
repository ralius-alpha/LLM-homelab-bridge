# -*- coding: utf-8 -*-
"""
役(role)の定義を、コードではなくファイルから読み込む。

役 = `roles/<role_id>/role.json`（モデル・使うtool名の一覧）+
     `roles/<role_id>/prompt.txt`（システムプロンプト）
の1セット。この2ファイルを `roles/<role_id>/` に置くだけで新しい役を定義できる
（ただし、その役を実際に呼び出す側のコードは別途必要。詳細はREADME参照）。

role.json の "tools" は tool名の文字列リストで、scripts.tools.TOOL_REGISTRY を
引いて実際のtool定義に変換する。dispatch（tool呼び出しの実処理）はここでは
関知しない。呼び出し元(arc_agent.py等)が持つ。
"""

import os
import json

from scripts.tools import TOOL_REGISTRY

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


def load_role(base_dir, role_id):
    """
    roles/<role_id>/ から役の定義を読み込む。
    戻り値: {"display_name", "model", "tools"（tool定義のlist）, "prompt"}
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

    return {
        "display_name": config.get("display_name", role_id),
        "model": config["model"],
        "tools": [TOOL_REGISTRY[name] for name in tool_names],
        "prompt": prompt,
    }
