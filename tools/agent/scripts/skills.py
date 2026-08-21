# -*- coding: utf-8 -*-
"""
役(role)をまたいで再利用できる「スキル」の実装を集めるモジュール。

[役とスキルの違い] 役はモデル+プロンプト+専門性の組み合わせ（誰が、という単位）。
スキルは副作用が無い/軽い、単独で完結する能力の実装（何ができるか、という単位）で、
どの役のtool一覧にも名前を足すだけで使い回せる。

[NOTE] execute_command/edit_file/read_fileは承認フロー(exec_mode)やWORK_DIR、
物理防御カウンタ等、Execute役の対話ループに深く統合されているため、意図的に
ここには移していない。ここに置くのは、そういった状態を一切持たず、引数だけで
完結する能力のみ。
"""

import re
import ast
import json
import html
import operator
import subprocess
import urllib.request
import urllib.parse

from scripts.config import OLLAMA_HOST

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_RESULT_LINK_RE = re.compile(
    r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', re.DOTALL
)
_RESULT_SNIPPET_RE = re.compile(
    r'<a class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text):
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _resolve_ddg_redirect(href):
    """DuckDuckGoの中継URL（//duckduckgo.com/l/?uddg=...）から実URLを取り出す。"""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


def web_search(query, max_results=5):
    """
    DuckDuckGoのHTML版エンドポイント（https://html.duckduckgo.com/html/）を検索し、
    タイトル・URL・スニペットをテキストで返す。APIキー不要。
    [NOTE] 公開ページのHTML構造に依存する簡易スクレイピングのため、DuckDuckGo側の
    ページ構造が変わると壊れうる。安定運用が必要になったら公式APIへの切り替えを検討。
    """
    query = (query or "").strip()
    if not query:
        return "[SEARCH ERROR] 検索キーワードが空です。"

    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = res.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"[SEARCH ERROR] 検索に失敗しました: {e}"

    links = _RESULT_LINK_RE.findall(body)
    snippets = _RESULT_SNIPPET_RE.findall(body)

    if not links:
        return f"[SEARCH] 「{query}」の検索結果が見つかりませんでした。"

    lines = [f"[SEARCH RESULTS for '{query}']"]
    for i, (href, title_html) in enumerate(links[:max_results]):
        title = _strip_tags(title_html)
        real_url = _resolve_ddg_redirect(href)
        snippet = _strip_tags(snippets[i]) if i < len(snippets) else ""
        lines.append(f"\n{i + 1}. {title}\n   {real_url}\n   {snippet}")

    return "\n".join(lines)


def fetch_url(url, max_chars=8000):
    """
    指定URLの内容を取得し、HTMLタグを除いたプレーンテキストとして返す。
    JS実行後のDOMではなく、生のHTMLをそのまま取得するだけの簡易実装
    （web_search が見出し/スニペットまでしか返さないのに対し、実際のページ
    本文まで読みたい時に使う）。
    """
    url = (url or "").strip()
    if not url:
        return "[FETCH ERROR] URLが空です。"
    if not (url.startswith("http://") or url.startswith("https://")):
        return "[FETCH ERROR] http:// か https:// で始まるURLを指定してください。"

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            content_type = res.headers.get("Content-Type", "")
            body = res.read()
    except Exception as e:
        return f"[FETCH ERROR] 取得に失敗しました: {e}"

    if content_type and "text" not in content_type and "html" not in content_type:
        return f"[FETCH ERROR] テキスト以外のコンテンツのようです（Content-Type: {content_type}）。"

    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        text = body.decode("latin-1", errors="replace")

    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    plain = _strip_tags(text)
    # [IMPORTANT] 以前は改行の圧縮(\n{3,} → \n\n)しかしていなかったため、
    # 「空白だけの行」がそのまま残り続けた。ナビゲーションやレイアウト用の
    # 空要素が多いページでは max_chars の大半を空白が占めてしまい、
    # 肝心の本文が切り捨てられる（weathernews.jpで、8084字取得したのに
    # 気温の表記が1つも含まれない状態を実測で確認した）。
    # 行単位で空白を潰し、空行を捨ててから字数制限をかける。
    lines = (re.sub(r"[ \t　 ]+", " ", ln).strip() for ln in plain.splitlines())
    plain = "\n".join(ln for ln in lines if ln)

    if not plain:
        return f"[FETCH] {url} から本文らしきテキストを抽出できませんでした。"

    if len(plain) > max_chars:
        plain = plain[:max_chars] + "\n...(長いため省略)"

    return f"[FETCHED CONTENT from {url}]\n{plain}"


def summarize_text(text, model_name, instruction=None):
    """
    現在ロード中のモデル(model_name)を使ってテキストを要約する。
    [IMPORTANT] 新たに別モデルをロードするのではなく、呼び出し元(役)が既に
    ロード済みのモデルをそのまま流用する設計。これによりVRAM制約（同時に
    1モデルまで）を破らない。model_nameは呼び出し側のコードが埋める引数で、
    tool呼び出し時にモデル自身が指定するものではない。
    """
    text = (text or "").strip()
    if not text:
        return "[SUMMARIZE ERROR] 要約対象のテキストが空です。"
    if not model_name:
        return "[SUMMARIZE ERROR] 使用するモデルが指定されていません。"

    instruction = instruction or "以下の文章の要点を落とさず、簡潔に日本語で要約してください。"
    prompt = f"{instruction}\n\n---\n{text}\n---"

    payload = json.dumps({
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            data = json.loads(res.read().decode("utf-8"))
        summary = (data.get("response") or "").strip()
        return summary or "[SUMMARIZE ERROR] モデルから空の応答が返りました。"
    except Exception as e:
        return f"[SUMMARIZE ERROR] 要約に失敗しました: {e}"


# calculate(): 四則演算・べき乗のみを許可するASTホワイトリスト方式。
# 名前参照・関数呼び出し・属性アクセスは一切評価しないため、任意コード実行には
# ならない（"run_python"のような汎用コード実行スキルは、承認フロー無しで
# ファイル/ネットワークに触れられてしまい安全に置けないため、あえてこの
# 電卓程度の機能に絞ってある）。
_CALC_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CALC_UNARYOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_BINOPS:
        return _CALC_BINOPS[type(node.op)](_calc_eval(node.left), _calc_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_UNARYOPS:
        return _CALC_UNARYOPS[type(node.op)](_calc_eval(node.operand))
    raise ValueError("数値の四則演算・べき乗以外は使えません（変数・関数呼び出し等は不可）。")


def calculate(expression):
    """数式（四則演算・べき乗）だけを安全に評価する。任意のPythonコード実行はしない。"""
    expression = (expression or "").strip()
    if not expression:
        return "[CALC ERROR] 式が空です。"
    try:
        tree = ast.parse(expression, mode="eval")
        result = _calc_eval(tree.body)
        return f"{expression} = {result}"
    except Exception as e:
        return f"[CALC ERROR] 計算できませんでした: {e}"


def git_diff_summary(cwd, max_chars=8000):
    """
    git status/diffの生データをまとめて返す（要約そのものはこの関数ではなく、
    これを読んだ呼び出し元のモデルが行う想定）。gitリポジトリで無い場合や
    gitが無い場合は、そのままエラー内容を返す。
    """
    def _run(args):
        try:
            result = subprocess.run(
                ["git"] + args, cwd=cwd, capture_output=True, timeout=15)
            try:
                out = result.stdout.decode("utf-8")
            except UnicodeDecodeError:
                out = result.stdout.decode("cp932", errors="replace")
            if result.returncode != 0:
                try:
                    err = result.stderr.decode("utf-8")
                except UnicodeDecodeError:
                    err = result.stderr.decode("cp932", errors="replace")
                return out + (f"\n[stderr]\n{err}" if err.strip() else "")
            return out
        except Exception as e:
            return f"(取得失敗: {e})"

    status = _run(["status", "--short"])
    stat = _run(["diff", "--stat"])
    diff = _run(["diff"])

    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n...(長いため省略)"

    parts = ["[GIT STATUS]", status.strip() or "(変更なし)", "", "[GIT DIFF STAT]", stat.strip() or "(差分なし)"]
    if diff.strip():
        parts += ["", "[GIT DIFF]", diff]
    return "\n".join(parts)
