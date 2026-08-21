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
                                log_path=None, role_id=None,
                                call_chain=None, root_request=None, **kwargs): ...

       戻り値: 呼び出し元への報告文字列（return_to_callerが呼ばれた場合）、
              それ以外の終了ならNone。

       role_id: 複数の役が同じモジュールを共用する場合がある（例:
       roles/execute と roles/review が両方とも arc_agent.py を使うが、
       tool一覧やプロンプトは別）。実装側はrole_idを見て、モジュール内で
       決め打ちにせず roles/<role_id>/ の定義を都度読み込むこと。

       call_chain: ここまでの呼び出し履歴（role_idのリスト。例:
       ["chat", "plan"]）。役同士が対等に呼び合える構造では、無限に
       たらい回しが続く危険があるため、実装側は「自分が何代目か」
       「誰から呼ばれてここに至ったか」をプロンプトに見せ、かつ一定の深さを
       超えたらhandoff_to_role自体を使えなくする、といった安全策を持つこと
       （arc_agent.pyの実装を参照）。

       root_request: ユーザーが最初に出した依頼の原文。引き継ぎのたびに
       instructionsは各役のモデルが書き直すため、連鎖が深くなるほど元の依頼から
       ずれていく（実機で「今日の東京の天気」→「最新の天気情報」→
       「適切なAPIを呼び出して」と変質するのを確認）。原文だけは書き換えずに
       持ち回り、引き継ぎ指示書に併記する（scripts.memory.build_handoff_brief）。
"""

import importlib

from scripts.role_loader import load_role
from scripts.ollama import unload_all_models


def invoke_role(base_dir, role_id, server_proc, instructions, log_path,
                call_chain=None, root_request=None):
    """
    role_idの役を入れ子で呼び出し、終わるまで待つ。
    呼び出す前に自分のモデルをアンロードするのは呼び出し元の責任
    （invoke_role自身は「何のモデルを呼び出し元が使っていたか」を知らない）。
    戻り値: 呼び出された役からの報告（要約文字列）。無ければNone。

    root_request: ユーザーが最初に出した依頼の原文。引き継ぎのたびに
    instructionsがモデルの言い換えで書き換わり、元の依頼から乖離していくため
    （実機で「今日の東京の天気」→「最新の天気情報」→「適切なAPIを呼び出して」と
    変質するのを確認）、原文だけは書き換えずに連鎖の最後まで持ち回る。
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
        role_id=role_id, call_chain=call_chain, root_request=root_request,
    )
