<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/khaym/pyestat/main/assets/logo-dark.png">
    <img src="https://raw.githubusercontent.com/khaym/pyestat/main/assets/logo.png" alt="pyestat" width="420">
  </picture>
</p>

<p align="center">
  <em>日本の公式統計ポータル
  (<a href="https://www.e-stat.go.jp/api/">e-Stat</a>) のデータを構造化するライブラリ —
  LLM やデータサイエンティストがそのまま使える形で。</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/pyestat/"><img src="https://img.shields.io/pypi/v/pyestat?color=082060" alt="PyPI version"></a>
  <a href="https://pypi.org/project/pyestat/"><img src="https://img.shields.io/pypi/pyversions/pyestat?color=082060" alt="Python versions"></a>
  <a href="https://github.com/khaym/pyestat/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="#なぜ-pyestat-か">なぜ</a> &bull;
  <a href="#インストール">インストール</a> &bull;
  <a href="#使い方">使い方</a> &bull;
  <a href="https://github.com/khaym/pyestat/blob/main/docs/AUTHORING_RULES.md">ルール作成</a> &bull;
  <a href="#ライセンス">ライセンス</a>
</p>

<p align="center">
  <a href="https://github.com/khaym/pyestat/blob/main/README.md">English</a> &bull; <b>日本語</b>
</p>

## なぜ pyestat か

e-Stat API が返す JSON は、元の XML を薄く再エンコードしたものです。次元コードは
`@` で始まるキーに、セルの値は `$` に隠れ、それらのコードに対応するラベルや
単位は別の `CLASS_INF` ブロックにあり、自分で突き合わせる必要があります。論理エラーは
HTTP 200 と非ゼロの `RESULT.STATUS` で返ってきます。既存の Python ラッパーは
「DataFrame を返す」ところで止まり、これらの非構造的なデータを呼び出し側にそのまま渡します。

`pyestat` はこれらを解決します。既定（`rule="auto"`）では各軸を分類し、表ごとにルールを
書かなくても自己記述的なセルを返します。

```python
# e-Stat の生 VALUE セル: コードだけ — ラベルと単位は CLASS_INF にある
{"@cat01": "000", "@time": "2020000000", "$": "126146"}

# pyestat (rule="auto"): コードはラベルに解決され、値は単位を伴う
{"cat01": {"code": "000", "label": "男女計"},
 "time":  {"code": "2020000000", "label": "2020年",
           "normalized": "2020", "granularity": "yearly"},
 "value": {"value": "126146", "unit": "千人"}}
```

LLM エージェントや研究者は、`CLASS_INF` を手で突き合わせる作業が不要になり、多種多様に存在する e-Stat のフォーマットを理解してなくてもデータとして扱えるようになります。

### できること

**探して取得**

- `list_stats` でカタログを検索（`searchWord` / `statsCode` など）。
- `get_meta_info` でダウンロード前に軸構成を確認。
- `iter_stats_data_pages` で数百万行を一度に取得せず 1 ページずつストリーミング — `max_rows` で
  取得前に件数を抑え（`TooManyRowsError`）、`progress` コールバックで進捗を追える。

**構造化**（`rule="auto"`、既定 — ルール不要）

- 次元コードを `{code, label}` に解決 — `CLASS_INF` を代わりに突き合わせ。
- 時間を正規化し、粒度（yearly / monthly / …）を付与。
- 行に分散した測定量を 1 レコードに畳み込み、主キーを自動判定 — 測定量軸が flat な表
  （GDP / CPI / 建築着工 …）が対象。階層クロス（貿易の measure × period）や複数分類軸の
  表は lossless な生のまま保全。
- `aggregates="exclude"` で小計/総計行を落とす（`"only"` で集計だけ）。
  `@parentCode` から判定する取得オプションで、どのモードでも指定可能。
- 自前の `RuleV2` で、ドメイン固有の列名や階層クロスの明示的 pivot（`where` / `key` /
  `unit_from`）。

**渡す**

- `to_flat()` で nested を 1 列 1 フィールドの flat 形へ（pandas 向け）。

全体を通じて、値はそのまま — 数値も文字列のまま、抑制マーカー（`-` / `***` / `X`）も
保全。e-Stat の HTTP200 論理エラーは型付きの `EstatApiError` として表面化し、一過性の
ネットワーク失敗は自動でリトライされます。

## ステータス

pyestat は 1.0 未満です。公開表面は速度の異なる 2 つの部分からなります。

- **Settled（安定）** — *利用する*側: ネストした `StatsDataResponse` の形
  （`to_flat()` 射影を含む）と `EstatError` 階層は 0.x を通じて維持されます。
- **Evolving（変化しうる）** — *記述する*側: `RuleV2` ルールスキーマは、組み込み
  カバレッジの拡大に伴い 0.x の間は変わりうります。

## インストール

```sh
uv add pyestat
# または
pip install pyestat
```

## 使い方

<https://www.e-stat.go.jp/api/> で `appId` を登録し、`EstatClient(app_id=...)` に
明示的に渡します。`ESTAT_APP_ID` 環境変数に保持して自分で読み込むのが一般的な慣習です。

```python
import os

from pyestat import EstatClient, EstatApiError

client = EstatClient(app_id=os.environ["ESTAT_APP_ID"])

try:
    response = client.get_stats_data(stats_data_id="0003448237")
except EstatApiError as exc:
    # e-Stat は論理エラーを HTTP 200 + STATUS != 0 で返す。
    print(f"e-Stat refused the query: {exc.status} {exc.message}")
else:
    print(response.stats_data_id)   # "0003448237"
    for row in response.values:
        # 既定の rule="auto" は自己記述的な *ネスト* セルを返す:
        # 各軸が {code, label}、time は normalized/granularity を加え、
        # 観測値は {value, unit}。
        print(row)
        # -> {"cat01": {"code": "000", "label": "男女計"},
        #     "time":  {"code": "2020000000", "label": "2020年",
        #               "normalized": "2020", "granularity": "yearly"},
        #     "value": {"value": "126146", "unit": "千人"}}
```

1 フィールド 1 列の形（pandas 向けなど）が良い場合は、`to_flat()` がネストしたセルを
おなじみの接尾辞付きの形へ射影します（損失なく、生の `rule=None` レスポンスに対しては
何もしません）。

```python
flat = response.to_flat()
# -> [{"cat01": "000", "cat01_label": "男女計",
#      "time": "2020", "time_code": "2020000000",
#      "time_label": "2020年", "time_granularity": "yearly",
#      "value": "126146", "unit": "千人"}, ...]

import pandas as pd
df = pd.DataFrame(flat)
```

代わりに `rule=None` を渡すと、e-Stat の生の行をそのまま得られます（`@` 始まりの次元は
素のキーに、`"$"` は `"value"` になります）。ラベルや正規化のない、平坦なスカラーです。

## 独自ルールを書く

`pyestat` は少数の表に組み込みルールを同梱し、それ以外は `rule="auto"` にフォールバック
します。別の構造化が欲しいとき、あるいはドメイン固有の列名が欲しいときは、自分の
`RuleV2` を渡します。

```python
from pyestat import EstatClient, RuleV2

custom = RuleV2.model_validate({
    "schema_version": "2",
    "match": {"role_pattern": ["value", "area", "time"]},
    "output": [
        {"column": "year",   "source": {"role": "time"}, "transform": "yearly"},
        {"column": "region", "source": {"role": "area"}},
        {"column": "value",  "source": {"role": "value"}},
    ],
})

client = EstatClient(user_rules=[custom])
```

ルールは欲しい **出力列** を宣言し、各列は分類器が推論した軸の *役割（role）* から値を
引きます。だから 1 つのルールが、同じ役割パターンを持つすべての表をカバーします。
`meta-axis` に分散した行のピボット、`to_flat()` 向けの列名の付け方、ディレクトリへの
ルールファイル配置は **[ルール説明](https://github.com/khaym/pyestat/blob/main/docs/AUTHORING_RULES.md)** で扱います。

> `RuleV2` スキーマは 0.x の間は変化しうります — [ステータス](#ステータス)を参照。

## エラー時の挙動

既定の `rule="auto"` 経路では、*ルール* の失敗が呼び出し側に届くかどうかは、その失敗した
ルールの作者が誰かで決まります。pyestat 由来ならフォールバック、ユーザー由来なら表面化
します。

- 適用できない組み込みルールは、例外を投げる代わりに損失のない生出力に縮退します。
  その失敗は内部的で、あなたには編集できないため、データの保全がクラッシュに勝ります。
- あなたが渡したルール（明示的な `rule=RuleV2(...)`、`user_rules=` のエントリ、
  `./pyestat_rules` 内のファイル）が適用できない場合は、型付きエラーを投げます。修正して
  再実行できるようにするためです。

つまり pyestat が未対応の表に対する `get_stats_data(id)` は、失敗せず使える生の行を返し、
一方であなた自身のルールの誤りは報告されます。

pyestat のすべてのエラーは `EstatError` を継承するので、`except EstatError` で粗く一括
捕捉できます。個別のケースに対処したいときは末端（`EstatApiError`、`TooManyRowsError`、
…）を捕捉してください。

## appId の設定

[使い方](#使い方)で基本の慣習を示しています — `app_id` を明示的に渡し、`ESTAT_APP_ID`
変数に保持します。その変数を環境にどう届けるかはプロジェクト次第です。代表的なパターン:

**シェルの export**（対話利用）:

```sh
export ESTAT_APP_ID="<your-app-id>"
python your_script.py
```

**`.env` ファイル + [python-dotenv](https://github.com/theskumar/python-dotenv)**
（ローカル開発、Jupyter）:

```sh
echo 'ESTAT_APP_ID=<your-app-id>' > .env
# コード（またはノートブックのセル）で:
```

```python
import os

from dotenv import load_dotenv
from pyestat import EstatClient

load_dotenv()
client = EstatClient(app_id=os.environ["ESTAT_APP_ID"])
```

**Docker / Compose**: `-e ESTAT_APP_ID=...` を渡すか、compose ファイルの
`environment:` に設定します。

**CI（GitHub Actions など）**: appId を暗号化シークレットとして保存し、ワークフローの
ステップで環境変数として注入します。

**本番**: 起動時にシークレットマネージャ
（AWS Secrets Manager / GCP Secret Manager / HashiCorp Vault / …）から取得し、
`EstatClient(app_id=...)` に渡します。

`pyestat` は環境を読まず、dotenv ローダーも同梱しません。シークレットの管理方法を縛らない
ためです。

## 開発

```sh
uv sync                              # 実行時 + 開発用の依存をインストール
cp .env.example .env                 # その後 ESTAT_APP_ID を記入
uv run pytest                        # ユニット + ライブ API テストを実行
uv run pytest -m "not integration"   # ユニットのみ（ネットワークなし）
```

`tests/test_get_stats_data_integration.py` のライブ統合テストは、`ESTAT_APP_ID` が未設定
なら自動でスキップされます。追加フラグなしでユニット一式が密閉的に保たれます。

## ライセンス

MIT ライセンス。詳細は [LICENSE](https://github.com/khaym/pyestat/blob/main/LICENSE) を参照してください。
