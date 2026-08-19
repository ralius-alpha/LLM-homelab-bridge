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
import html
import urllib.request
import urllib.parse

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
