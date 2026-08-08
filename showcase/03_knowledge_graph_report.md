# P3 — Knowledge Graph Import

## 目的

Odooコードを生成する前に、構造化した要件、標準モデル、追加候補、業務シナリオの関係をNeo4jへ投入し、グラフの連続性と参照整合性を検証します。

## 実行結果

| 指標 | 件数 |
|---|---:|
| ノード | 839 |
| リレーション | 2,729 |
| dangling relationship | 0 |

### 主なノード

| ラベル | 件数 |
|---|---:|
| P3OverlayFieldCandidate | 446 |
| ExistingReference | 154 |
| MinorCustomCandidate | 105 |
| P3DomainValuePreservation | 86 |
| P3LaterPhaseLink | 48 |
| LaterPhaseConcept | 45 |
| P2StandardConfiguration | 43 |
| OdooStandardModel | 30 |
| Bundle | 24 |
| CrossAppMinorCustomCandidate | 19 |
| App | 6 |
| Scenario | 6 |

### 主な関係

| Relationship type | 件数 |
|---|---:|
| MINOR_CUSTOM_PRESERVES_DOMAIN_VALUE | 723 |
| MINOR_CUSTOM_REFINES_CONFIGURATION | 662 |
| MINOR_CUSTOM_EXTENDS_MODEL | 530 |
| LATER_PHASE_CONCEPT_ANCHORS_TO_MINOR_CUSTOM | 336 |
| SCENARIO_USES_MINOR_CUSTOM | 210 |
| BUNDLE_USES_MINOR_CUSTOM | 137 |
| APP_HAS_MINOR_CUSTOM | 131 |

## 実行順序

1. Neo4j dry-run
2. ノード・関係件数の検証
3. dangling relationshipの検査
4. 明示的な承認
5. Odooアドオン候補の生成

Odoo生成をグラフ検証より先に実行しないことで、誤った参照や推測による実装を早い段階で停止できます。
