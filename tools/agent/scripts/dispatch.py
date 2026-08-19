# -*- coding: utf-8 -*-
"""
役(role)の入れ子呼び出しを共通化するモジュール。

以前は「雑談役がExecute役を呼ぶ」処理が chat_agent.py に直接書かれ、
`arc_agent.start_interactive_chat()` を名指しで呼んでいた。これだと役が
増えるたびに呼び出し側のコードを書き足す必要があり、「共通の形でどの役でも
呼べる」にならない。

ここでは role.json の "module" フィールド（呼び出し先を実装するPythonモジュール名）
を見て importlib で動的にimportし、共通の関数シグネチャで呼び出す。

[契約] 引き継ぎ先として呼ばれる役（role.jsonに"module"を持つ役）の実装は、
       次のシグネチャの関数を公開すること（関数名は role.json の "entry" で
       指定。省略時は "start_interactive_chat"）:

    def start_interactive_chat(model_name, exec_mode, server_proc, *,
                                initial_message=None, is_nested=False,
                                log_path=None, **kwargs): ...

       戻り値: 呼び出し元への報告文字列（return_to_callerが呼ばれた場合）、
              それ以外の終了ならNone。
"""

import importlib

from scripts.role_loader import load_role
from scripts.ollama import unload_all_models


def invoke_role(base_dir, role_id, server_proc, instructions, log_path):
    """
    role_idの役を入れ子で呼び出し、終わるまで待つ。
    呼び出す前に自分のモデルをアンロードするのは呼び出し元の責任
    （invoke_role自身は「何のモデルを呼び出し元が使っていたか」を知らない）。
    戻り値: 呼び出された役からの報告（要約文字列）。無ければNone。
    """
    role = load_role(base_dir, role_id)
    module_name = role.get("module")
    if not module_name:
        raise ValueError(
            f"roles/{role_id}/role.json に \"module\" がありません。"
            f"引き継ぎ先として呼ぶには module（実装しているPythonモジュール名）が必要です。"
        )

    module = importlib.import_module(module_name)
    entry_name = role.get("entry") or "start_interactive_chat"
    entry = getattr(module, entry_name)

    unload_all_models()
    return entry(
        role["model"], "safe", server_proc,
        initial_message=instructions, is_nested=True, log_path=log_path,
    )
