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

# すべての役が最初から持っている「一般的な能力」。
#
# [IMPORTANT] 設計の根幹。人間でも、調べ物・電卓・要約は誰でもやることであって、
# 「検索できる人」という専門職を立てたりはしない。役を分ける理由になるのは
# 特異なこと（その役の専門性）と、危険な権限（コマンド実行・ファイル編集）だけ。
#
# 以前はこれらのtoolを役ごとにバラバラに持たせていた（planにはsearch_webが
# あるがfetch_urlは無い、testには両方無い、writerにはsummarize_textがあるが
# calculateは無い…といった、根拠の無い差）。その結果、
# 【本来は存在しないはずの能力ギャップ】が人工的に生まれ、それが引き継ぎの
# 理由になってしまっていた。実機では、search_webを持っている3役が揃って
# 「私は検索できません」と言い合ってたらい回しする所まで悪化した。
#
# 引き継ぎは「toolが無いから」ではなく、「難しいと判断したとき」
# 「自分でやって失敗が続いたとき」に起きるべきもの。そのための土台として、
# 一般的な能力は全員に配る。
COMMON_SKILLS = ["search_web", "fetch_url", "summarize_text", "calculate", "remember"]


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
    # [IMPORTANT] role.jsonの"tools"はその役に固有のtoolしか書かない。
    # ここで返すのは「実際にその役が使えるtool一覧」でなければならない
    # （load_role と同じく COMMON_SKILLS を合成する）。合成を忘れると、
    # 引き継ぎ指示書に載る「あなたが使えるtool」から一般スキルが抜け落ち、
    # 受け取った役が「自分はsearch_webを持っていない」と誤解する——
    # まさに直したはずの能力の誤申告を再発させる（単体テストで検出済み）。
    declared = config.get("tools", [])
    tools = list(COMMON_SKILLS) + [t for t in declared if t not in COMMON_SKILLS]
    return {
        "role_id": role_id,
        "display_name": config.get("display_name", role_id),
        "specialty": config.get("specialty", ""),
        "tools": tools,
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


def _inject_preflight_check(prompt):
    """
    「動く前に、必要な情報が揃っているか自分で確かめる」という原則を全役に注入する。

    [IMPORTANT] これは特定の話題向けの指示ではない。「地名が無ければ聞き返す」
    のような個別対応をプロンプトに書き足していくと、書いた話題でしか効かず、
    プロンプトが例で膨れ上がる。必要なのは「対象が特定できているか」を
    自分で判断する習慣そのもの。
    同時に、聞き返しすぎも害になる（会話から分かることを毎回確認されると
    ユーザーの手間が増えるだけ）ので、両側を書く。
    """
    block = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "【動く前に: 必要な情報が揃っているかを確かめる】",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "[IMPORTANT] 作業やtool呼び出しを始める前に、それを実行するのに必要な"
        "情報が依頼の中に揃っているかを自分で確かめること。",
        "揃っていない例:",
        "- 対象が特定できない（どのファイル・どの場所・どの範囲・どの期間か決まらない）",
        "- 前提が複数あり得て、どれを選ぶかで結果が変わる",
        "- 依頼の言葉が曖昧で、複数の解釈ができる",
        "この状態のまま推測で進めると、見当違いのものを調べたり作ったりして"
        "やり直しになる。作業を始める前に、足りない点だけを短く質問すること"
        "（何が分かれば進められるのかを具体的に聞く）。",
        "[IMPORTANT] 確認のために聞き返すことは、依頼を断ることではない。"
        "「自分にはできません」と手を引くのは誤りだが、"
        "「どれを対象にすればよいですか」と対象を確かめるのは正しい行動。"
        "混同しないこと。確認が取れたら、その時点ですぐ作業に入る。",
        "",
        "[IMPORTANT] ただし聞き返しすぎないこと。直前までの会話から十分に"
        "判断できることを確認し直すのは、ユーザーの手間を増やすだけ。"
        "会話に既に出ている前提は引き継いで使うこと。"
        "「〜でよろしいですか？」を毎回付けるのも不要。",
        "判断の目安: 【推測が外れたらやり直しになるか】。"
        "外れても軽微なら、自分で妥当な前提を置いて進め、"
        "その前提を返答の中で一言添える。",
    ]
    return prompt + "\n\n" + "\n".join(block)


def _inject_skill_notes(prompt, tool_names, targets):
    """
    「自分に何ができるか」と「どういう時に他の役へ引き継ぐか」をプロンプトに注入する。

    [IMPORTANT] 設計の要。引き継ぎの判断基準は【toolの有無ではない】。
    一般的な能力(COMMON_SKILLS)は全役が最初から持っているので、
    「そのtoolを持っていないから引き継ぐ」という理由は原則として成り立たない。
    引き継ぐのは、(1)自分の権限では実行できない作業が要るとき、
    (2)自分で試したが失敗が続いて手詰まりになったとき、
    (3)明らかに他の役の専門性が要るとき、の3つ。

    以前は逆に「持っていないtoolがあれば引き継げ」と書いていた。役ごとに
    読み取り専用toolがバラバラに欠けていた時代の名残で、実在しない能力
    ギャップを引き継ぎの口実にしてしまっていた（実機で、search_webを
    持っている3役が揃って「私は検索できません」と言い合い、たらい回しの
    末にMAX_CALL_DEPTHで打ち止めになる所まで悪化した）。
    """
    have_lines = []
    for name in tool_names:
        note = SKILL_USAGE_NOTES.get(name)
        if note:
            have_lines.append(f"- {name}: {note}")

    # 自分に無く、引き継ぎ先が持っている「権限系」のtool。
    # 一般的なスキルは全員が持っているので、ここに残るのは本当に権限の差だけ。
    # [NOTE] return_to_caller / handoff_to_role は「能力」ではなく会話を
    # 受け渡すための制御用。雑談役は連鎖の起点で戻る先が無いため
    # return_to_callerを持たないが、それを「権限が無い作業」として
    # 挙げるのは誤解のもとなので除く。
    control_tools = {"return_to_caller", "handoff_to_role"}
    privileged = []
    for t in targets:
        for name in t.get("tools", []):
            if name in tool_names or name in SKILL_USAGE_NOTES or name in control_tools:
                continue
            privileged.append((name, t["role_id"]))
    owners = {}
    for name, rid in privileged:
        owners.setdefault(name, []).append(rid)

    if not have_lines and not owners:
        return prompt

    block = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "【自分にできること／他の役に頼るとき】",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if have_lines:
        block.append("あなたが使えるスキルと、その使いどころ:")
        block.extend(have_lines)
        block.append("")
    block.append(
        "[IMPORTANT] 上に挙げた一般的なスキル（検索・ページ取得・要約・計算など）は"
        "【すべての役が最初から持っている】。したがって「そのtoolを持っていないから」"
        "という理由で他の役に引き継ぐのは誤り。まず自分でやってみること。"
        "ユーザーに「ご自身で検索してください」のような案内をして終わるのは"
        "もっと悪い（それはあなたの仕事であり、ユーザーにやらせるものではない）。"
    )
    if owners:
        block.append("")
        block.append(
            "あなたには権限が無く、他の役だけができる作業:"
        )
        for name, rids in sorted(owners.items()):
            block.append(f"- {name}（{'・'.join(sorted(set(rids)))}役ができる）")
        block.append(
            "これらが必要になったら、名前を借りて自分で呼んでも実行されない。"
            "handoff_to_role で該当の役に引き継ぐこと。"
        )
    block.append("")
    block.append("他の役に引き継ぐのは、次のどれかに当てはまる時だけでよい:")
    block.append("1. 自分の権限では実行できない作業が必要なとき（上記）。")
    block.append("2. 自分で何度か試したが失敗が続き、手詰まりになったとき。")
    block.append("3. 明らかにその役の専門性が必要なとき。")
    block.append(
        "逆に、自分のスキルで片付くなら引き継がずに自分で終わらせること。"
        "引き継ぎはモデルの入れ替えを伴い時間がかかるので、"
        "「念のため」で渡さないこと。"
    )
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

    # role.json が宣言するのは「その役に固有のtool」だけでよい。
    # 一般的な能力(COMMON_SKILLS)は全役に自動で付与する。
    declared = config.get("tools", [])
    tool_names = list(COMMON_SKILLS)
    tool_names += [name for name in declared if name not in tool_names]

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
    prompt = _inject_preflight_check(prompt)

    return {
        "display_name": config.get("display_name", role_id),
        "model": config["model"],
        "tools": tools,
        "prompt": prompt,
        "specialty": specialty,
        "can_handoff_to": can_handoff_to,
        "module": config.get("module"),
    }
