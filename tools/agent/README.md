# ローカルエージェント構成

Intel Arc A770機で動く、複数の役(role)が交代で動くローカルLLMエージェント。
GPU(VRAM)が1本しかないため、複数の役を同時には動かせない。そのため
「役の切り替え＝別プロセスへの引き継ぎ」ではなく、**同一プロセス内の入れ子の関数呼び出し**
として実装している（詳細は後述）。

[NOTE] 実装の経緯・検証結果は [`planning/chat-history-summary.md`](../../planning/chat-history-summary.md)
にも記録がある。本ドキュメントは現在の仕様のリファレンス。

[設計目標] 役が増えても破綻しないこと。具体的には: (1) どの役からどの役へも
共通の形（`handoff_to_role` / `return_to_caller`）で引き継げる、(2) 引き継ぎ先は
実際の直近の会話を受け取れる、(3) 役をまたいでも1つの共通ログで会話の続きが追える、
(4) 各役は自分の専門性を認識し、専門外は無理に自分でやろうとせず適切な役に
任せる、の4点。詳細は「新しい役を追加するには」を参照。

---

## 役(role)一覧

「役」はモデルと初期プロンプト（＋使えるtool）の組み合わせに過ぎない。
その定義は `roles/<role_id>/` ディレクトリに切り出してあり、コードではなく
データ（`role.json` + `prompt.txt`）として持つ。

| 役 | 定義 | 実行する側のモジュール | できること |
|----|------|----------------------|-----------|
| 雑談役 (chat) | `roles/chat/` | `chat_agent.py` | 会話のみ。ファイル操作・コマンド実行は不可 |
| Execute役 (execute) | `roles/execute/` | `arc_agent.py` | コマンド実行・ファイル編集・調査系スキル・共有メモ書き込み |
| Review役 (review) | `roles/review/` | `arc_agent.py`（共用） | ファイルを読んで指摘するだけ。編集不可 |
| Plan役 (plan) | `roles/plan/` | `arc_agent.py`（共用） | 依頼を実行前に順序立てた計画に分解する。実行はしない |
| Writer役 (writer) | `roles/writer/` | `arc_agent.py`（共用） | コード・ログを説明文/記事に書き起こす。ファイル編集は不可 |
| Debug役 (debug) | `roles/debug/` | `arc_agent.py`（共用） | コマンドで再現・調査し原因を特定する。修正はしない |
| Test役 (test) | `roles/test/` | `arc_agent.py`（共用） | 既存のテストを実行し結果を報告する。修正はしない |

[NOTE] chat以外の6役はすべて同じ`arc_agent.py`を`module`として共用している。
違いは`role.json`の`tools`（例えばReview/Plan/Writer/Debug/Testは`edit_file`を
持たない）と`prompt.txt`の専門性の記述だけで、新しい実装コードは1行も増えて
いない。実装を増やさず、role.jsonの`tools`を絞るだけで専門特化した役を
作れることの実例（詳細は後述の「モジュール共用時のtool制限漏れ」も参照）。

### 役（role）とスキル（skill）の違い

「役」はモデル+プロンプト+専門性の組み合わせ（誰が、という単位）。
「スキル」は副作用が無い/軽い、単独で完結する能力の実装（何ができるか、という単位）で、
特定の役に紐づかない。例えばWeb検索は、それ専用の役（人格）を作る必要は無く、
どの役の`tools`にも`search_web`を足すだけで使い回せる。

- スキルの**スキーマ**（Ollamaに渡すtool定義のJSON）は他のtoolと同じく
  `scripts/tools.py`の`TOOL_REGISTRY`に登録する。
- スキルの**実装**（実際に何をするか）は`scripts/skills.py`に置く。現状4種類:
  - `web_search()` — DuckDuckGoのHTML版を検索（APIキー不要）
  - `fetch_url()` — 指定URLの本文をテキストで取得
  - `summarize_text()` — 呼び出し元が**既にロード済みのモデル**を使って要約する
    （新規にモデルをロードし直さないため、VRAM制約を破らない。model_nameは
    tool呼び出し時にモデル自身が指定するのではなく、呼び出し元コードが埋める）
  - `calculate()` — 四則演算・べき乗のみを許可するASTホワイトリスト方式の電卓
    （変数参照・関数呼び出し・属性アクセスは一切評価しない。任意のPythonコード
    実行はできない。汎用的な"run_python"のようなtoolは、execute_commandの
    承認フローを経ずに何でもできてしまい安全に置けないため、あえてこの
    電卓程度の機能に絞ってある）
  - `git_diff_summary()` — `git status`/`git diff`の生データを取得（要約は
    これを読んだ側のモデルが行う）
- `execute_command`/`edit_file`/`read_file`は`scripts/skills.py`には無い。
  承認フロー(exec_mode)・WORK_DIR・物理防御カウンタ等、Execute役の対話ループに
  深く統合されているため、意図的にそちらには移していない。スキルに向くのは、
  そういった状態を一切持たず、引数だけで完結する能力（調べるだけ、計算するだけ等）。

### 新しい役を追加するには

`roles/<role_id>/` に以下の2ファイルを置く。

```
roles/<role_id>/role.json
{
  "display_name": "...",
  "specialty": "この役の専門性（1行）。handoff_to_roleの説明文と、自分自身への
                自己認識プロンプトの両方に自動で使われる。",
  "model": "...",
  "tools": ["read_file", "remember", ...],
  "can_handoff_to": ["execute", ...],   // 他の役に引き継げるなら列挙。無ければ省略可
  "module": "some_role_module"          // 引き継ぎ先として呼ばれる役だけ必要（後述）
}

roles/<role_id>/prompt.txt   システムプロンプト本文
```

`tools` に書けるのは `scripts/tools.py` の `TOOL_REGISTRY` に登録済みのtool名だけ
（`scripts/role_loader.py` の `load_role()` が読み込み時に検証する）。
`handoff_to_role` はここには書かない。`can_handoff_to` を書くと自動的に付与される
（後述）。

[NOTE] 役の「定義」（モデル・プロンプト・使えるtool・引き継ぎ先・専門性）はファイル
だけで完結する。ただし tool呼び出しの実処理（`run_command`等）は実装コード側に依存
するため、既存のExecute役と全く違う種類の役（例: Web検索専用でファイル編集はしない
役）を追加する場合は、その役自身の実装（Pythonモジュール）を書く必要がある。

#### 引き継ぎ（handoff）は role_id 名指しではなく共通の形

役Aの `role.json` に `"can_handoff_to": ["execute", "review"]` と書くと、
`scripts/role_loader.py` が自動的に `handoff_to_role` という1つのtool
（`role_id` はenumで `execute`/`review` に制限される）をAのtool一覧に追加し、
Aのプロンプトの末尾にも「自分の専門性はこれ、それ以外はこの役に引き継げ」という
自己認識ブロックを自動で注入する。これにより：

- 呼び出す側（chat_agent.py等）は `handoff_to_role` という1つのtool呼び出しだけを
  見ればよく、`handoff_to_execute` のような役名固定のtoolを役の数だけ増やす必要が無い。
- 引き継ぎ先の実際の起動は `scripts/dispatch.py` の `invoke_role(base_dir, role_id, ...)`
  が担う。`role.json` の `"module"` を見て `importlib` でそのモジュールを動的に
  importし、`"entry"`（省略時は `start_interactive_chat`）という名前の関数を
  共通のシグネチャで呼ぶ。呼び出し元のコード（`chat_agent.py`）は、どの役を
  呼ぶ時も同じ`invoke_role()`しか使わないため、新しい役を追加してもこのファイルは
  変更不要（`roles/<role_id>/role.json`の`can_handoff_to`に追記するだけでよい）。
- 引き継ぎ先として呼ばれる役の実装は、次の契約を満たす関数を公開すること:
  ```python
  def start_interactive_chat(model_name, exec_mode, server_proc, *,
                              initial_message=None, is_nested=False,
                              log_path=None, role_id=None, **kwargs): ...
  # 戻り値: return_to_callerが呼ばれれば要約文字列、それ以外の終了ならNone
  ```
  [IMPORTANT] `role_id`は「複数の役が同じモジュールを共用できる」ようにするための
  引数。実装側はモジュール読み込み時に固定されたグローバルなroleを常に使うのではなく、
  `role_id`が渡されたらそちらの`roles/<role_id>/`定義を都度読み込むこと（`arc_agent.py`
  の実装を参照）。これを怠ると、tool一覧を絞ったつもりの役（例: Review役に`edit_file`を
  持たせない）が、実際にはモジュール既定の役と同じ全toolを使えてしまうという事故が起こる
  （Review役を追加した際に実際に踏んだ不具合。詳細は「既知の不具合と対策」参照）。
- 戻る側も `return_to_caller`（引数`summary`のみ）という1つのtoolに統一されている。
  「雑談役に戻る」という名前ではなく「呼び出し元に戻る」なので、将来Execute役以外の
  役からExecute役を呼ぶケースが増えても意味が破綻しない。

[NOTE] ストリーム応答の表示（点字スピナー・「思考中」→「出力中」のラベル切り替え・
トークン数表示）は`scripts/display.py`の`stream_chat_response()`に共通化してあり、
`chat_agent.py`/`arc_agent.py`どちらも同じものを使う。以前は`chat_agent.py`だけ
別の簡易実装（スピナー無し）を個別に持っていて表示が役ごとに揃っていなかったが、
これは役の「定義」の話ではなく実装の重複だったため、ここに一本化した。

起動はユーザーが `chat_agent.py` を実行するところから始まる:

```powershell
cd tools\agent
python chat_agent.py
```

`arc_agent.py` を直接起動することもできる（従来通りメニュー選択式で動く。手動でExecute役
だけを使いたい時用）。ただし通常はユーザーが直接起動するのは `chat_agent.py` のみでよい。

`arc_agent.py`単体起動時のメニューには `[t] テストモード` がある。`scripts.config.MODELS`
の決め打ちリスト（実機に入っていないモデルも並ぶ）ではなく、Ollamaに実際にpull済みの
モデルを`ollama list`相当のAPIで取得して一覧表示し、番号かモデル名を直接入力して起動できる。
`num_ctx`（コンテキスト長）もその場で自由に指定できる（空Enterで既定の8192）。
新しいモデルを試す・大きいコンテキストで挙動を見る、といった検証用。

---

## 全体の流れ（入れ子構造）

```
main() in chat_agent.py
  └─ run_chat_loop()                       ← 雑談役。ユーザーと直接対話する
       │  ユーザーの要望が実作業を要すると判断
       │  handoff_to_role(role_id="execute") が呼ばれる
       │  直近の会話の抜粋（要約ではなく実データ）を instructions に添えて渡す
       ▼
     run_role_and_wait() → scripts.dispatch.invoke_role()
       │  自分(雑談役)のモデルをVRAMから解放
       │  role.jsonの"module"を見てimportlibで動的import
       ▼
     arc_agent.start_interactive_chat(is_nested=True, log_path=共通ログ)  ← Execute役。同じプロセス
       │  コマンド実行・ファイル編集を行う（雑談役と同じログファイルに追記）
       │  作業完了と判断
       │  return_to_caller が呼ばれる → 要約文字列を return
       ▼
     invoke_role() に戻ってくる
       │  Execute役のモデルは自分の中でVRAM解放済み
       │  雑談役のモデルを再ロード
       ▼
     run_chat_loop() の続きに戻る            ← 要約を会話に注入して雑談続行
```

引き継ぎ先の`start_interactive_chat()`は普通のPython関数で、呼ばれたら実行し、
終わったら（`return_to_caller` が呼ばれるか、単に終了すれば）呼び出し元に戻ってくる。
別プロセスを起動することも、何かを `kill` することも一切ない。

### なぜプロセスを分けず、この形にしたか

最初はプロセスを分け、役の切り替えのたびに「前の役をkillして次の役を新規プロセスで起動する」
設計で作っていた（`active_role.json` でPIDを記録し、起動時に前の役を`taskkill`する等）。
しかし「役はモデルと初期プロンプトの違いでしかない」と気づき、入れ子の関数呼び出しに
置き換えた。GPU制約や起動失敗への配慮は、この形でもそのまま満たされる。

- **VRAM制約（同時に1モデルまで）**: 呼び出す前に自分のモデルをアンロードし、
  呼び出し先が終わるまで自分は関数呼び出しの中でブロックされたまま待つ。
  同時に2つのモデルがロードされることはない。
- **起動失敗への配慮**: 別プロセスを新規に起動するわけではないので、
  「起動したはずが失敗する」という事故そのものが起こり得ない。
- **副次効果**: コンソールウィンドウが新しく開いたり閉じたりすることが無くなった。
  Execute役が例外で落ちても、呼び出し元(雑談役)の`try/except`で捕まえて
  会話を継続できる（プロセスごと落ちる旧設計より頑健）。
- 役を追加する場合も、必要な役の関数を呼んで結果を受け取るだけでよく、
  入れ子は自然に深くなる（プロセス管理の仕組みを都度増やす必要が無い）。
  実際にReview/Plan/Writer/Debug/Testの5役を追加した際も、この呼び出し部分
  （`chat_agent.py`・`scripts/dispatch.py`）は無改修で済んだ。

---

## 状態ファイル（`tools/agent/` 直下、実行時生成・gitignore対象）

| ファイル | 役割 | 寿命 |
|---------|------|------|
| `shared_memory.md` | 全役共通の「忘れてはいけない事項」。追記型 | 永続（消えない。ユーザーが直接編集してもよい） |
| `logs/{role}_{timestamp}.log` | セッション全体の会話ログ。ターンごとに逐次追記 | 永続（古いものは手動で整理する想定） |

役の切り替え自体はプロセス内の関数呼び出しなので、以前あった `active_role.json`
（PID記録）や `relay.json`（引き継ぎメッセージ）は不要になり廃止した。

[NOTE] ログファイルは役ごとではなく、**トップレベルのセッション単位で1つ**。
`chat_agent.main()` が最初に1回 `start_session_log()` で作ったログファイルの
パス(`log_path`)を、引き継ぎのたびに `invoke_role()` → 引き継ぎ先の
`start_interactive_chat(..., log_path=...)` へそのまま渡し、引き継ぎ先も同じ
ファイルに追記する。以前は役ごとに別ファイル（`chat_*.log`と`execute_*.log`が
別々）だったため、雑談役に戻った時に何が起きたかを追うには2つのファイルを
タイムスタンプで突き合わせる必要があったが、今は1ファイルで完結する。
`scripts/memory.py`の`append_role_transition()`が引き継ぎ/復帰のたびに
`=== 引き継ぎ: chat → execute ===` / `=== 復帰: execute → chat ===` という
区切りをログに書き込むため、ファイルを開くだけで会話の続きが追える。

---

## tool一覧

Ollamaのtool calling（function calling）で実装している。モデルによっては構造化された
`message.tool_calls` を返さず、JSON文字列を`content`にそのまま出力することがあるため、
`scripts/tools.py` の `tool_call_from_content()` がその救済フォールバックとして働く。
1つの返答に複数のtool呼び出しJSON（```json```フェンスが複数）が並ぶこともあるため、
見つかった有効なものを全て抽出する（最初の1個だけを取り出す実装だと、2個以上並んだ時に
まとめて壊れたJSONとして読めてしまい、両方とも無視される不具合があった）。

### 役をまたいで共通のtool（`can_handoff_to`があれば自動付与）

| tool | 引数 | 用途 |
|------|------|------|
| `handoff_to_role` | `role_id`, `instructions`, `reason` | 指定した役を呼び出す（`role_id`は自分の`can_handoff_to`の範囲にenumで制限される） |
| `return_to_caller` | `summary` | 作業完了。自分を呼び出した側に会話を戻す（関数のreturn） |
| `remember` | `note` | 共有メモに書き残す |

### 雑談役固有

| tool | 引数 | 用途 |
|------|------|------|
| `handoff_to_role` | `role_id`（`execute`/`review`/`plan`/`writer`/`debug`/`test`）, `instructions`, `reason` | `roles/chat/role.json`の`can_handoff_to`がこの6つ |
| `remember` | `note` | 共有メモに書き残す |

### Execute役固有

| tool | 引数 | 用途 |
|------|------|------|
| `execute_command` | `command` | PowerShellコマンドを1つ実行 |
| `edit_file` | `file`, `search`, `replace` | SEARCH/REPLACE形式のピンポイント編集 |
| `read_file` | `file` | ファイルを行番号付きで読む（文字化け対策） |
| `search_web` | `query` | インターネット検索（スキル） |
| `fetch_url` | `url` | 指定URLの本文を取得（スキル） |
| `calculate` | `expression` | 正確な数式計算（スキル） |
| `git_diff_summary` | (無し) | git status/diffの取得（スキル） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 作業完了。呼び出し元に会話を戻す（関数のreturn） |

### Review役固有

| tool | 引数 | 用途 |
|------|------|------|
| `execute_command` | `command` | 調査目的のみ（`is_read_only_command()`の判定は変わらないため、変更系コマンドは承認要求される） |
| `read_file` | `file` | ファイルを行番号付きで読む |
| `search_web` | `query` | インターネット検索（スキル） |
| `fetch_url` | `url` | 指定URLの本文を取得（スキル） |
| `git_diff_summary` | (無し) | git status/diffの取得（スキル。差分レビュー用） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 確認完了。呼び出し元に会話を戻す（関数のreturn） |
| ~~`edit_file`~~ | - | `role.json`の`tools`に含めていないため使えない |

### Plan役固有

| tool | 引数 | 用途 |
|------|------|------|
| `read_file` | `file` | 計画を立てる上での現状把握用 |
| `search_web` | `query` | インターネット検索（スキル） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 立てた計画（番号付きステップ）を呼び出し元に返す |

### Writer役固有

| tool | 引数 | 用途 |
|------|------|------|
| `read_file` | `file` | 記事の材料になるファイルを読む |
| `search_web` | `query` | インターネット検索（スキル） |
| `fetch_url` | `url` | 指定URLの本文を取得（スキル） |
| `summarize_text` | `text`, `instruction` | 長い材料を要点だけに圧縮（スキル） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 書き上げた文章そのものを返す（要約ではなく成果物） |

### Debug役固有

| tool | 引数 | 用途 |
|------|------|------|
| `read_file` | `file` | 関連ファイル・ログを読む |
| `execute_command` | `command` | 再現・調査目的のみ（状態変更コマンドは使わない前提） |
| `search_web` | `query` | インターネット検索（スキル。エラーメッセージの意味等） |
| `fetch_url` | `url` | 指定URLの本文を取得（スキル） |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 判明した原因（または切り分け状況）を返す |

### Test役固有

| tool | 引数 | 用途 |
|------|------|------|
| `read_file` | `file` | テスト・設定ファイルの確認 |
| `execute_command` | `command` | テストの実行 |
| `remember` | `note` | 共有メモに書き残す |
| `return_to_caller` | `summary` | 成功/失敗件数・失敗内容を返す |

### スキル系tool一覧

| tool | 引数 | 用途 | 実装 |
|------|------|------|------|
| `search_web` | `query` | DuckDuckGoのHTML版を検索し、上位結果（タイトル・URL・要約）を返す | `scripts/skills.py`の`web_search()` |
| `fetch_url` | `url` | 指定URLの本文をテキストで取得 | `fetch_url()` |
| `summarize_text` | `text`, `instruction` | 呼び出し元のモデルで文章を要約 | `summarize_text()` |
| `calculate` | `expression` | 四則演算・べき乗を安全に計算（ASTホワイトリスト方式） | `calculate()` |
| `git_diff_summary` | (無し) | git status/diffの生データを取得 | `git_diff_summary()` |

いずれも副作用が無い（何も変更しない）。

[NOTE] 訂正: 以前ここに「雑談役からsearch_webへのルーティングは不安定」と書いて
いたが、これは誤りだった。原因は2つの独立したバグで、モデルの判断とは無関係
だった。詳細は「既知の不具合と対策」の該当項目を参照:
1. テスト入力（printf経由でシェルにパイプした日本語）自体が文字化けしており、
   モデルは実際には壊れた文字列を渡されていた（「読み解けません」という応答は
   むしろ正しい反応だった）。
2. 正しい入力で再テストしたところ、雑談役はほぼ毎回正しくhandoff_to_roleを
   呼んでいたが、まれにJSONの正規形式ではなくプログラミング言語風の
   `handoff_to_role({...})` という表記で書いてしまい、それが救済フォールバック
   でも拾えず無視されていた。tools.pyのフォールバックパーサーにこの表記も
   拾えるようにし、プロンプトにも正しい形式の例を足して解消した。
`search_web → fetch_url → return_to_caller` という複数tool・複数役をまたぐ
連鎖（雑談役→Review役、Review役内でsearch_web後にfetch_urlも自発的に呼ぶ）が
実機で通ることを確認済み。

いずれのtoolも1返答につき1回だけ呼ぶ設計（各役の`roles/<role_id>/prompt.txt`で明示）。

---

## モデル選定の経緯

[NOTE] 検証時点(Ollama 0.32.6)での結果。バージョンが上がれば変わる可能性がある。

雑談役のモデルは複数回入れ替えて検証した:

- **deepseek-r1:14b**: `handoff_to_execute`（旧tool名）を実際には呼ばず、言葉で
  「引き継ぎます」と説明するだけで終わることが3回中3回発生。判断はできているが、
  tool呼び出しという手続きに変換できていなかった。
- **llama3.1:8b**: tool呼び出し自体は構造化`tool_calls`で確実に返る。しかし
  「やぁ」のような雑談にまでtoolを呼んでしまう誤検知が、temperatureを0まで
  下げても直らなかった（tools有りだと何か呼ばなければと思い込む癖）。
  toolを1個に絞っても改善せず、むしろ全メッセージで誤発火するようになり悪化した。
- **qwen2.5-coder:14b**: 構造化`tool_calls`は返さないが、`tool_call_from_content()`の
  フォールバックで拾える形のJSONを出す。プロンプトを強化（「toolは例外処理」
  「rememberは価値ある事実がある時だけ」を明記）した上で検証したところ、
  雑談には反応せず・曖昧な依頼は聞き返し・明確な依頼は正しく引き継ぐ、と
  最もバランスが良かった。現在の既定モデル。

Execute役の既定モデルも同じ qwen2.5-coder:14b。これは `roles/execute/role.json`の
`"model"`が唯一の情報源で、引き継ぎ時に使うモデルも`scripts.dispatch.invoke_role()`が
そこから読む。
[NOTE] 以前は`chat_agent.py`が引き継ぎ先のモデルを`scripts.config.MODELS["6"]`という
別の決め打ちから取っており、`roles/execute/role.json`の`"model"`を書き換えても
実際の引き継ぎ先モデルは変わらないという不整合があった（役の定義が2箇所に分かれていた）。
`invoke_role()`が`role.json`だけを見るようにして解消した。
`roles/execute/prompt.txt` の手本セクションが「→ tool(args) を呼ぶ。」という擬似コード表記だった時は、
モデルがそれをそのまま文章として書き写すだけでtoolを呼ばない不具合があった。
手本は「実際にtool呼び出し機能を使う」ことを明記し、コピー可能な疑似コードを避ける形に直した。

[WARNING] 小型ローカルモデルは判断を誤ることがある（例: 相対パスでのファイル読み込みに
1回失敗しただけで「ファイルが存在しない」と誤った結論をremember/return_to_callerに
書いてしまうケースを確認済み）。共有メモや報告内容は鵜呑みにせず、重要な判断は
ユーザー自身で確認すること。

---

## 既知の不具合と対策

### 調査範囲の無限拡大 → 最終報告自体が失敗する（軽減済み・完全解決ではない）

Debug役の実機テストで発見。「rolesディレクトリの中身を調査して報告して」
という曖昧な依頼に対し、指定されたコマンドを1回実行した後も止まらず、
全役分の`role.json`/`prompt.txt`を（一部は2回）次々読みに行き、
`MAX_AUTO_STEPS`(12回)の上限に到達した。この時点で会話が非常に長く
なっており（実測でプロンプト処理が10,752トークンを超えて進行中）、
暴走停止後の最終報告生成（`_final_report()`、`num_ctx=16384`）の
Ollamaサーバー側リクエスト自体がHTTP 500で失敗した
（`ollama_server.log`で確認。Arc A770のVRAM不足が有力な原因）。

VRAM/プロセスの後片付け自体は`finally`により正常に行われることを
`tasklist`で確認済み（リークは無い）。ただし`_final_report()`の例外
捕捉が`urllib.error.URLError`だけに限定されていたため、サーバー側の
異常切断が別種の例外になった場合にエラーメッセージが失われる恐れが
あった。`except Exception`に広げ、必ず何が起きたかを画面とログに
残すよう修正。

根本的な対策として、Debug/Plan/Test役のプロンプトに「調査は依頼された
範囲に留める。手を広げすぎると最終報告自体が失敗しうる」という注記を
追加した。実機で「1回だけコマンドを実行して報告」という明確なスコープの
依頼を与えたところ、期待通り2ターンで完了することを確認した。

[NOTE] これはプロンプトでの軽減であり、根本的な解決ではない。会話が
本当に長くなった場合に`messages`を自動で刈り込む・要約するような
仕組みは無い。曖昧で範囲の広い依頼をDebug/Plan/Test役に投げると、
将来また同じ問題が起こりうる。

### rememberをメモ帳代わりに使い、本来の仕事をしない（軽減済み）

Plan/Writer役の実機テストで発見。両役とも、実際に計画を立てる/文章を書く
代わりに、作業の「各ステップ」を`remember`に1つずつ書き残すだけで
止まってしまうことがあった（Writer役は同一内容を3回繰り返して物理防御に
強制停止され、Plan役は毎回違う内容だったため停止されず、最後まで
`remember`を7回呼んだ末にようやく計画を返した）。
「今すぐ書けるのに、書く代わりに関係の薄いtoolを呼ぶ」という同系統の
問題が5役中2役で再現したため、Execute/Review/Plan/Writer/Debug/Testの
`remember`の説明と絶対ルールに「メモ帳ではない」「2回以上呼んでいたら
バグの兆候」という注記を追加した。

さらにPlan役では、`remember`ループが直ったあとも、計画を会話文として
書くだけで`return_to_caller`を1度も呼ばずに終える、という別の失敗が
残っていた（tool呼び出しゼロで会話文だけの返答は、呼び出し元には何も
伝わらない）。抽象的な警告文を何段階か強めても直らず、Execute役の
「お手本」パターンと同様に**具体的な出力例（正しい形式のJSON1個と、
誤りの例を並べたもの）**を追加して初めて安定した。この一連の経緯から、
このプロジェクトの小型モデルには「〜してはいけない」という抽象的な
指示より、「これが正解、これが不正解」という具体例の方が効きやすい
傾向がありそうだと分かる。

[NOTE] このデバッグの過程で `shared_memory.md`（gitignore対象、テストで
汚れていた）が、以前の壊れたテストで書き残された内容をそのまま次の
テストに引き継いでしまい、プロンプト修正の効果を覆い隠していたことも
判明した（お手本追加前の「直っていないように見えた」再テストの一部は、
実際には#古い共有メモの内容をそのまま話していただけだった）。
共有メモは全役のプロンプトに自動で注入されるため、テスト中に不可解な
挙動が続く場合は`shared_memory.md`の中身も疑うこと。

### 標準入力(stdin)の文字化け（修正済み）

`sys.stdout`はcp932対策でUTF-8にreconfigureしていたが、`sys.stdin`は
していなかった。標準入力がリダイレクト/パイプ経由（対話的なコンソールで
ない）の時、Pythonはコンソールの既定コードページ(cp932)で`input()`を
decodeしてしまい、UTF-8で書かれた日本語の入力が丸ごと文字化けしていた
（エラーにはならず、無言で化けた文字列がモデルにそのまま渡る）。
`chat_agent.py`/`arc_agent.py`双方の冒頭で`sys.stdin.reconfigure()`も
行うようにして解消した。

[IMPORTANT] このバグはテスト方法にも影響していた。日本語のテスト入力を
`printf '...' | python chat_agent.py`のようにシェル経由でパイプしていた
ケースでも同様の文字化けが起きており、「モデルがルーティングに失敗した」
ように見えたテスト結果のいくつかは、実際には壊れた入力を渡していただけ
だった（後述の「雑談役のtool呼び出し形式ゆれ」の発見経緯を参照）。

`reconfigure()`はストリームから1度でも読み込んだ後には呼べない
（`RuntimeError`）。`chat_agent.py`から入れ子で`arc_agent.py`が呼ばれる
時、`arc_agent`のimportは「雑談役が既にstdinから最初の入力を読んだ後」に
起きるため、`arc_agent.py`側の`reconfigure()`は必ず失敗する（雑談役側で
既に正しく設定済みなので実害は無い）。単体起動時は逆にこちらが最初の
`reconfigure()`になるので効く。どちらの場合でもクラッシュしないよう
try/exceptで囲んである。

### 雑談役のtool呼び出し形式ゆれ（修正済み）

雑談役に「来週の東京の天気について、ネットで調べて教えて」（正しくUTF-8で
渡した入力）を投げたところ、`handoff_to_role`を呼ぶべきと正しく判断した
上で、JSONの正規形式ではなく次のようなプログラミング言語の関数呼び出し風の
表記で書いてしまうことがあった:
```
handoff_to_role({
  "role_id": "review",
  "instructions": "...",
  "reason": "..."
})
```
これは`tool_calls_to_actions`が期待する`{"name": ..., "arguments": {...}}`
形式ではないため、救済フォールバック(`tool_call_from_content`)でも拾えず、
handoffが黙って無視されていた。
`scripts/tools.py`に`_extract_function_call_style()`を追加し、
`funcName({...})`形式（既知のtool名にマッチするもののみ、誤検知防止のため）
も拾えるようにした。加えて`roles/chat/prompt.txt`に正しいJSON形式の
具体例を追記。修正後、`search_web → fetch_url → return_to_caller`という
複数役をまたぐ連鎖が実機で正しく動くことを確認した。

### chat_agent.pyのnum_ctx未設定（修正済み）

`arc_agent.py`は元からAPI呼び出しに`num_ctx`を明示していたが、
`chat_agent.py`には無く、Ollamaの既定値（多くの場合2048〜4096）に依存
していた。雑談役のシステムプロンプトは役が増えるたびに大きくなっており
（6役分のtool定義＋自己認識ブロック込みで数千トークン規模）、既定値では
黙って切り詰められていた可能性がある（エラーにならないため気づきにくい）。
`arc_agent.py`と同じ`num_ctx=8192`を明示するよう修正。

### VRAM/メモリのリーク（修正済み）

`cleanup_processes()` が対象にしていたプロセス名が古く、実際にモデルを保持している
ランナープロセスの名前（`llama-server.exe`。検証時点のOllama 0.32.6）と一致していなかった。
そのため `ollama.exe` 本体を殺しても、ランナーだけ孤児化してVRAM/メモリを掴んだまま
残り続けていた（セッションを重ねるたびに蓄積し、数十GB分溜まった実績あり）。
`scripts/ollama.py` の `cleanup_processes()` / `startup_cleanup()` を修正済み。

### セッションが想定外に終了した場合の後片付け

`run_chat_loop()` / `start_interactive_chat()` は、想定していない例外（バグ等）で
関数を抜けた場合でも `finally` でVRAM解放だけは必ず行うようにしてある。

### execute_commandの文字化け（修正済み）

原因は2つ重なっていた。
1. **出力側**: 日本語Windowsでは、PowerShellの標準出力は既定でコンソールのコード
   ページ(cp932)で書き出される。以前はPython側で`utf-8`decodeを先に試みており、
   cp932のバイト列がたまたま`utf-8`として"エラー無く"decodeできてしまうケースで
   文字化けが起きていた（decode自体は成功するため、フォールバックのcp932側に
   落ちない）。
2. **入力側**: Windows PowerShell 5.1の`Get-Content`等は、UTF-8(BOM無し)のファイル
   を読む時、既定ではシステムのコードページ(cp932)として読んでしまう（BOM付き
   UTF-8/UTF-16でなければ自動判定されない）。このリポジトリのファイルはBOM無し
   UTF-8で保存されているため、`Get-Content`経由で読むと内部表現の時点で既に
   化けており、正しい日本語パターンで`Select-String`しても一致しない、という
   無言の不具合になっていた（エラーにならないため気づきにくい）。

`run_command()`が組み立てるPowerShellコマンドの先頭に、出力エンコーディング
（`[Console]::OutputEncoding` / `$OutputEncoding`）と、`Get-Content`等の既定
エンコーディング（`$PSDefaultParameterValues['*:Encoding']`）を両方`utf8`に
固定する前置きを追加して解消した。

### モジュール共用時のtool制限漏れ（修正済み）

Review役を追加する際に発見。`arc_agent.py`は元々Execute役専用に書かれており、
モジュールの先頭で `ROLE = load_role(BASE_DIR, "execute")` と一度だけ読み込んで
いた。Review役が同じ`arc_agent.py`を`module`として指定しても、関数内部が
このモジュール直下の`ROLE`（execute固定）を参照している限り、実際に使われる
プロンプトとtool一覧はexecuteのものになってしまい、Review役の`role.json`で
`edit_file`を外していても意味が無い（読み取り専用のつもりが編集可能なまま）
という事故になるところだった。
`start_interactive_chat()`に`role_id`引数を追加し、渡された場合はモジュール直下の
`ROLE`ではなく`load_role(BASE_DIR, role_id)`を都度読み込んで使うように修正。
`scripts/dispatch.py`の`invoke_role()`が常に`role_id`を渡すため、以後この種の
役は安全に追加できる。

### tool呼び出しを文章として書くだけで実行しない（プロンプトで軽減）

Plan/Writer/Debug/Test役を追加した際の実機テストで、Debug役が`execute_command`
成功後、次のターンで`return_to_caller`を実際には呼ばず、「return_to_caller」と
いう単語を文章の末尾に書くだけで終える挙動を1回確認した（`tool_call_from_content()`
が拾える形のJSONではないため、何も実行されない）。`is_nested=True`で戻り値が
無いまま入力待ちに落ち、標準入力がEOFだったためそのまま正常終了はしたが、
`return_to_caller`が呼ばれなかったので呼び出し元へは要約が渡らなかった。

これは以前`roles/execute/prompt.txt`の手本セクションで踏んだのと同じ系統の
問題（モデルがtool呼び出しを"文章として書き写すだけ"で済ませてしまう）。
該当4役 + Review役の【絶対ルール】に「文章で書くだけでは実行されない。
必ずtool呼び出し機能そのものを使うこと」という一文を追加したところ、再現
テストでは解消した。ただし1回の再現テストで直っただけであり、統計的に
解消したとまでは言えない。小型モデルの既知の不安定要素として引き続き注意する。

---

## ファイル構成

```
tools/agent/
├── chat_agent.py            雑談役 本体・プログラムの入口（ユーザーが起動するのはこれ）
├── arc_agent.py              Execute役 本体（scripts.dispatch経由で動的に呼ばれる）
├── README.md                    このファイル
├── roles/
│   ├── chat/
│   │   ├── role.json             雑談役の定義（モデル・専門性・使うtool名・引き継ぎ先）
│   │   └── prompt.txt            雑談役システムプロンプト
│   ├── execute/
│   │   ├── role.json             Execute役の定義（モデル・専門性・使うtool名・実装module）
│   │   └── prompt.txt            Execute役システムプロンプト
│   ├── review/
│   │   ├── role.json             Review役の定義（Execute役と同じmoduleだがtoolsを読み取り専用に絞る）
│   │   └── prompt.txt            Review役システムプロンプト
│   ├── plan/
│   │   ├── role.json             Plan役の定義（計画立案のみ。実行系toolを持たない）
│   │   └── prompt.txt            Plan役システムプロンプト
│   ├── writer/
│   │   ├── role.json             Writer役の定義（文章執筆のみ。編集系toolを持たない）
│   │   └── prompt.txt            Writer役システムプロンプト
│   ├── debug/
│   │   ├── role.json             Debug役の定義（原因調査のみ。edit_fileを持たない）
│   │   └── prompt.txt            Debug役システムプロンプト
│   └── test/
│       ├── role.json             Test役の定義（テスト実行のみ。edit_fileを持たない）
│       └── prompt.txt            Test役システムプロンプト
└── scripts/
    ├── config.py               定数（ファイル名等）
    ├── ollama.py               Ollamaサーバーのライフサイクル管理（起動・停止・VRAM解放）
    ├── tools.py                  tool定義・TOOL_REGISTRY・tool_calls⇔内部アクション形式の変換
    ├── skills.py                   役をまたいで再利用できる能力の実装（現状はweb_search）
    ├── display.py                 ストリーム応答の表示（Spinner・思考中/出力中表示）。役をまたいで共通
    ├── role_loader.py            roles/ からの役の読み込み。専門性・引き継ぎ先の解決も担う
    ├── dispatch.py                役の入れ子呼び出しの共通化（role.jsonの"module"を動的import）
    └── memory.py                 セッションログ・共有メモ(shared_memory.md)の読み書き
```
