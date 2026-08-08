# P4 — Odoo Addon Candidate Mapping

## 目的

検証済みの構造化データから、Odoo標準業務モデルを直接変更しない、レビュー用オーバーレイアドオン候補を作成します。

## 変換候補

| Odooオーバーレイモデル | 元データ | 状態 | 件数 |
|---|---|---|---:|
| `fg.p1p2.standard.configuration` | P2StandardConfiguration | approved candidate | 48 |
| `fg.p1p2.domain.value.config.link` | DomainValueConfigLink | approved candidate | 36 |
| `fg.p1p2.later.phase.concept` | LaterPhaseConcept | needs review | 93 |
| `fg.p1p2.supporting.anchor` | ExternalSupportingAnchor | approved candidate | 6 |
| `fg.p1p2.gap.item` | Fit & Gap report | report only | 10 |

## 集計

- 元グラフ: 300ノード・1,146リレーション
- 承認済み候補: 90件
- 要レビュー: 93件
- レポート専用Gap: 10件
- 自動生成から除外されたまま消失した項目: 0件

## 非破壊方針

`sale.order`、`stock.picking`、`mrp.production`、`account.move`などの標準業務モデルへ直接書き込みません。候補は`fg.p1p2.*`名前空間の独立モデルに保持し、参照、トレーサビリティ、レビュー、レポート作成に利用します。

後続工程の概念93件とFit & Gap 10件は、情報として保持しますが、`auto_generation_target = false`として自動コード生成から除外します。
