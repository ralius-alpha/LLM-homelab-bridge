# LLM-homelab-bridge

> **Bridge** = 橋渡し ＆ 艦橋（司令室）
>
> LLM-homelab プロジェクトの **司令室** であり、
> 各種チャネルとの **橋渡し** を担う運営リポジトリ。

###############################
# [CRITICAL] このリポジトリは絶対に Public にしない #
###############################

本リポジトリには以下の機密情報を含む:

- 社内評価・プロモーション関連情報
- 上司・同僚に関する個人的見解
- 連載運営の本当の動機・戦略
- 業務上の機密に近い情報

**GitHub Settings の Visibility 変更を絶対に触らないこと。**
詳細は「絶対遵守ルール」セクションを参照。

---

## このリポジトリの正体（自分用メモ）

[WARNING] 未来の自分へ：このリポジトリの本当の意味を忘れるな

このリポジトリの名前 `bridge` には **二重の意味** を込めている。

### 意味①：橋渡し（Bridge）

個人プロジェクト `LLM-homelab` と外部チャネル（Zenn・社内掲示板・社内関係者）
との間を取り持つ、運営情報の中継地点。

### 意味②：艦橋（Bridge / 司令室）

このプロジェクト全体の **戦略司令室**。
戦略・計画・本音・振り返り・社内政治対応を、全部ここで指揮する。
外からは見えない、自分だけの作戦司令室。

---

## なぜこのリポジトリを分離したか

公開リポジトリ [`LLM-homelab`](https://github.com/ralius-alpha/LLM-homelab) と
**完全に物理分離** することで、以下を実現する。

1. **事故防止**: `.gitignore` 漏れ等で機密が公開される事故を物理的に防ぐ
2. **本音の確保**: 自分用の本音メモを遠慮なく書ける場所を確保
3. **戦略の継続性**: 戦略情報をバージョン管理で蓄積
4. **マインドの分離**: 公開モード / 戦略モードを切り替えやすくする

---

## 絶対遵守ルール

### [MUST NOT] やってはいけないこと

1. **このリポジトリを Public にしない**
    - GitHub Settings → Danger Zone を絶対に触らない
2. **Collaborator を追加しない**（自分のみ）
3. **このリポジトリの内容を `LLM-homelab` リポジトリにコピーしない**
4. **画面共有・スクショ時に映さない**
5. **AIツール（Claude Code等）に内容を読ませる時は機密前提で扱う**

### [MUST DO] やるべきこと

1. **公開前チェックリスト** ([`playbook/publication-checklist.md`](./playbook/publication-checklist.md)) を毎回実行
2. **月次振り返り** ([`retrospective/monthly-reviews/`](./retrospective/monthly-reviews/)) を記録
3. **戦略変更時は [`planning/`](./planning/) を更新**
4. **コミット前に `git status` で内容確認**

---

## ディレクトリ構成と役割

| ディレクトリ         | 艦内施設     | 役割                              |
|----------------------|--------------|-----------------------------------|
| `planning/`          | 作戦室       | 戦略・計画・全体方針              |
| `connections/`       | 通信室       | 社内関係者との連携メモ            |
| `crossings/`         | 出撃ゲート   | 公開チャネル別の運用手順          |
| `retrospective/`     | 航海日誌     | 月次振り返り・学び                |
| `playbook/`          | 運用マニュアル室 | チェックリスト・SOP           |
| `drafts-internal/`   | 資料庫       | 社内向け資料ドラフト              |
| `private/`           | 機密保管庫   | 最秘匿情報（FB記録・本音）        |

---

## 主要ドキュメントへのナビ

### まず読むべきもの

- [全体戦略](./planning/overall-strategy.md) - なぜこの連載をやるのか（本音）
- [公開前チェックリスト](./playbook/publication-checklist.md) - 公開時の必須確認事項
- [クロスポストチェックリスト](./crossings/cross-posting-checklist.md) - 各チャネル公開手順

### 状況別に読むもの

| 状況                       | 読むファイル                                                          |
|----------------------------|-----------------------------------------------------------------------|
| 詰まった時                  | [`playbook/when-stuck.md`](./playbook/when-stuck.md)                 |
| 炎上・批判があった時        | [`playbook/crisis-management.md`](./playbook/crisis-management.md)   |
| PLとの面談前                | [`connections/pl-communication.md`](./connections/pl-communication.md) |
| 評価面談前                  | [`drafts-internal/promotion-appeal.md`](./drafts-internal/promotion-appeal.md) |

---

## 関連リポジトリ

| リポジトリ                                                                    | 種別        | 役割                     |
|-------------------------------------------------------------------------------|-------------|--------------------------|
| [LLM-homelab](https://github.com/ralius-alpha/LLM-homelab)   | Public      | 連載コンテンツ本体       |
| **LLM-homelab-bridge**                                                        | **Private** | このリポジトリ・運営司令室 |

---

## セキュリティ設定（初期設定済みか確認）

| 項目                                     | 状態    |
|------------------------------------------|---------|
| リポジトリ Visibility: Private          | `[-]`   |
| Collaborators: 自分のみ                  | `[-]`   |
| Branch protection: main保護（誤操作防止） | `[-]`   |
| 2FA: 有効                                | `[-]`   |
| Pinned に表示していない                  | `[-]`   |
| ローカルディレクトリは Public と物理分離 | `[-]`   |

**チェック方法**: 設定完了したら `[-]` を `[DONE]` に更新する。

---

## 更新ルール

- **計画変更時**: `planning/` を即更新
- **月次**: `retrospective/monthly-reviews/YYYY-MM.md` を追加
- **重要イベント時**: 該当ファイルに追記
- **思いついた本音**: `private/personal-notes.md` に書き殴る

---

## 司令官心得（自分への戒め）

1. **完璧主義を捨てる**: 本音メモは整理されてなくていい
2. **継続を優先する**: 書き溜め型運用を守る
3. **公開と戦略を混ぜない**: 物理分離を死守する
4. **計画倒れも織り込む**: 保険を張ってから進む
5. **楽しむ**: 個人プロジェクトであることを忘れない

---

*"Bridge to the future, from the bridge of the ship."*
*未来への橋を、艦橋から架ける。*

---

## English Translation

Status: `[IN PREPARATION]`

英語勉強のため個人的な翻訳活動をしています。気長にお待ちを。

(English translation is in preparation as a personal English learning activity.
Please be patient.)

[NOTE] 本リポジトリは Private なので、英訳の緊急性は Public リポジトリより低い。
Public 側の英訳が一段落してから着手する予定。

### Translation Progress

Status transitions: `[IN PREPARATION]` → `[IN PROGRESS]` → `[PARTIAL]` → `[COMPLETED]`

- [ ] Repository Identity (Two meanings of Bridge)
- [ ] Rationale for Separation
- [ ] Absolute Rules (MUST NOT / MUST DO)
- [ ] Directory Structure
- [ ] Documentation Navigation
- [ ] Related Repositories
- [ ] Security Checklist
- [ ] Update Rules
- [ ] Commander's Mindset