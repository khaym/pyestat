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

`pyestat` はこれらを解決します。表ごとの準備なしに、各コードはラベルと、各値は単位と
結びついた形で返ります（既定の `rule="auto"`）。

```python
# e-Stat の生 VALUE セル: コードだけ — ラベルと単位は CLASS_INF にある
{"@cat01": "000", "@time": "2020000000", "$": "126146"}

# pyestat (rule="auto"): コードはラベルに解決され、値は単位を伴う
{"cat01": {"code": "000", "label": "男女計"},
 "time":  {"code": "2020000000", "label": "2020年",
           "normalized": "2020", "granularity": "yearly"},
 "value": {"value": "126146", "unit": "千人"}}
```

さらに、e-Stat が 1 つの観測を複数行に分けて返すとき（測定量ごとに 1 行）も、1 レコードに
畳み戻し、測定量ごとに 1 列にします。

```python
# e-Stat: 測定量ごとに別の行。time は共通
{"@time": "2020000000", "@tab": "001", "$": "16"}                     # 数量
{"@time": "2020000000", "@tab": "002", "$": "35220", "@unit": "千円"}  # 金額

# pyestat (rule="auto"): 1 レコードにまとめ、測定量を列へ
{"time": {"code": "2020000000", "label": "2020年",
          "normalized": "2020", "granularity": "yearly"},
 "数量": {"value": "16",    "unit": None},
 "金額": {"value": "35220", "unit": "千円"}}
```

LLM エージェントや研究者は、`CLASS_INF` を手で突き合わせたり行を組み直したりする作業が不要になり、多種多様に存在する e-Stat のフォーマットを理解していなくてもデータとして扱えるようになります。

### できること

**バラバラなデータを、そのまま分析に使える形へそろえる**

コードとラベルの突き合わせだけではありません。多種多様なフォーマットを使いやすくする変換処理が組み込まれています。

- e-Statでは測定値ごとに異なる行として複数行のデータが返ってきます。キーとなるカラムなのか1行に畳み込むべき測定値なのか自動的に判定して畳み込みます。
- バラバラな時間表記を、比べられる形にそろえる — e-Stat は年・年月・四半期を表ごとに
  別形式・不透明コード（`2020000000`、`1994000103`）で返す。pyestat は `2020` / `2020-03`
  / `1994-Q1` に正規化し、粒度（yearly / monthly / quarterly）を判定します。異なる粒度・異なる表を、
  時間軸でそのまま並べ替え・結合できるようになります。

  ```python
  # e-Stat: 四半期が不透明な time コードで届く（意味は CLASS_INF のラベル頼み）
  {"@time": "1994000103", ...}          # CLASS_INF ラベル: "1994年1～3月期"

  # pyestat: 比べて並べられる形に正規化し、粒度も付ける
  {"time": {"code": "1994000103", "label": "1994年1～3月期",
            "normalized": "1994-Q1", "granularity": "quarterly"}, ...}
  ```

- 小計・総計の行を落として、足し込める数字だけにそろえる — `aggregates="exclude"` で小計・
  総計行を落とし、`"only"` で集計行だけを残す。小計まで足しこんで数値が狂う事故を防ぎます。

**巨大な表から、必要な部分だけ取り出す**

- 数百万行を、必要な数百行だけに絞って取得 — `select` を使って品目・地域・期間などの条件で絞り込みができます。
  消費者物価指数（未絞り込みで約 1,300 万行）でも、`get_meta_info` が示すのと同じ軸 ID を指定すれば、必要な行だけが返ります。
- 大きな表を、メモリに載せきらずに取得 — `iter_stats_data_pages` が 1 ページずつ返す。
  `max_rows` で取得上限を設定でき（`TooManyRowsError`）、長い取得は `progress` コールバックで追えます。

**必要な表を見つける**

- 目的の表を、キーワードやコードでカタログから探す — `list_stats`（`searchWord` / `statsCode` など）。
- 取得前に、表の軸（絞り込みに使うコード）を確かめる — `get_meta_info`。

**手元のツールへ渡す**

- pandas などのために 1 フィールド 1 列にする — `to_flat()`。

pyestat がまだ畳まない表に当たった、あるいは独自の列名がほしいときは、短いルールを 1 つ書く
だけ — [独自ルールを書く](#独自ルールを書く) を参照。

全体を通じて、数値は書き換えません — 文字列のまま保たれ、e-Stat の抑制マーカー
（`-` / `***` / `X`）もそのまま残ります。e-Stat が論理エラーを HTTP 200 で返す癖は型付きの
`EstatApiError` として表面化し、切れた接続は自動でリトライされます。

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

### 巨大な表から必要なスライスだけ取得する

丸ごと取得するには大きすぎる表があります — 消費者物価指数は全品目・全地域・全期間で
数百万行に及びます。`select` は `get_meta_info` が示す軸 ID をキーにサーバー側で絞り込むので、
必要なスライスだけを取得できます。

```python
# 総合（全品目）× 全国 × 指数、年次の行のみ
resp = client.get_stats_data(
    "0003427113",  # 2020年基準 CPI — 未絞り込みで約1,300万行
    select={"cat01": "0001", "area": "00000", "tab": "1", "time": {"level": "1"}},
)
# 数百行が、上と同じ構造で返る — カタログ全体ではない
```

`select` の値はコード、コードのリスト、または `code` / `level`（単一レベルまたは範囲）/
`from` / `to`（コードの範囲を含む）のいずれかを設定するマッピングです（各キーは任意で、
例の `time: {"level": "1"}` のように単独でも使えます）。コードは e-Stat 自身のもので、
pyestat はカタログ照合をせずそのまま渡すため、誤ったコードはクライアント側では検出されず、
e-Stat が単に 0 行を返します。コードは `get_meta_info` から読み取ってください。

```python
for axis in client.get_meta_info("0003427113").class_objs:
    print(axis.id, axis.name, len(axis.classes))  # 軸 ID、名前、メンバ数
```

返り値は通常のレスポンスです — `to_flat()` で `pandas` に渡し、分析はそこから先で。

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
