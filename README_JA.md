# Face Motion v3 — 受信サンプル（UDP / TCP）

**iFacialMocap**・**iFacialMocapTr**・**Facemotion3d**から、BlendShapeの値と頭・目の姿勢を受け取ります。

[English](README.md) · [仕様書](PROTOCOL_V3_JA.md) · [変更点](CHANGELOG.md) · [MITライセンス](LICENSE)

図：[接続の流れ](diagrams/fmv3_flow.svg) · [メッセージの構造](diagrams/fmv3_messages.svg)

Python 3.10以上で動き、`pip install`は不要です。実機のiPhone/iPadと通信するだけなら**`face_motion_v3.py`だけ**で足ります。macOSでは必要に応じて`python`を`python3`に置き換えてください。

## 1. UDPで受信する（推奨）

`PHONE_IP`をiPhone/iPadのLAN内アドレスに置き換えます。

```sh
python face_motion_v3.py --host PHONE_IP
```

iFacialMocapTrと**Facemotion3d**も同じコマンドです。Facemotion3dには**OtherライセンスまたはUnityライセンス**が必要です（どちらもない場合、送信は約10秒で止まります）。v3はどちらのライセンスでもOtherの出力（iFacialMocap互換の出力）で送るので、iFacialMocapと同じポートを使い、追加のオプションは要りません。

`State=STREAMING`が表示され、`frames=`が増え続ければ成功です。1フレームの全値を約1秒に1回、1行で表示します。受信はすべてのフレームで行っています。`--log-every 0`で表示を止められます。終了は**Ctrl+C**です。

## 2. TCPで受信する

```sh
python face_motion_v3.py --host PHONE_IP --transport tcp
```

使い方はUDPと同じです。ネットワークでUDPが通らない場合に使ってください。

## 3. iOSアプリから開始する（手動開始）

先に受信側（PC）を起動します。受信側（PC）は何も送らずに待ちます。

```sh
python face_motion_v3.py --listen                    # UDP、このPCのポート49983
python face_motion_v3.py --listen --transport tcp    # TCP、このPCのポート49984
```

次に、アプリのFace Motion v3設定に、このPCのLAN内アドレスとポートを入力して送信を開始します。アプリに`0.0.0.0`は入力しないでください。

iPhoneからの送信が止まっても、受信側（PC）は終了しません。何度か再試行した後もポートを開いたまま待ち（`State=WAITING`）、アプリからの次の開始を受け付けます。

## 4. ポート

| アプリ | iOSの待受（UDP / TCP） | このPCの待受（UDP / TCP） |
|---|---|---|
| iFacialMocap、iFacialMocapTr、Facemotion3d | 49983 / 49984 | 49983 / 49984 |

`--ios-port`は、PCが接続するiPhone側のポートを変えます。`--pc-port`は、このPCが待ち受けるポートを変えます。どちらの側も通信方式ごとに1つのポートだけを使います。PC側のポートを別のプログラムが使っている場合、受信側（PC）はその旨を表示して終了します。別のポートへの自動切替はしません。

## 5. 対応アプリのバージョン

| アプリ | バージョン |
|---|---|
| iFacialMocap | 1.5.3以降 |
| iFacialMocapTr | 1.2.6以降 |
| Facemotion3d | 1.4.6以降（OtherライセンスまたはUnityライセンス） |

これらの版は**FMV3 契約revision 5**を使います。それ以前のベータ版はrevision 4で、アプリと受信側（PC）が理由を表示して互いに拒否します。両方を更新してください。このサンプルはv3専用で、v1/v2のテキスト形式は読みません。

## 6. 自分のプログラムで値を使う

値は表示された行からではなく、コールバックから読みます。BlendShapeの名前は対応表（SCHEMA＝BlendShapeの名前の一覧と数値の読み方）が届いたときに一度だけ調べ、フレームごとには位置で読みます。

```python
from face_motion_v3 import V3Client

jaw = None

def on_schema(schema):
    global jaw
    jaw = schema.index_by_name.get("jawOpen")      # 名前は変わることがあるので、ここで調べ直す

def on_frame(frame):
    if jaw is not None and frame.tracking:
        value = frame.blend_values[jaw] / 100.0    # 整数のパーセント：-25 -> -0.25
        # frame.head = (rx, ry, rz, px, py, pz)、frame.right_eye / frame.left_eye = (rx, ry, rz)

V3Client("PHONE_IP").run(on_frame, on_schema=on_schema)
```

- BlendShapeの数は**52に固定されていません**。アプリや設定によって変わり、BlendShapeの一覧が違う録画データの再生を始めたときなど、通信中に一覧が変わることもあります（そのたびに`on_schema`が呼ばれます）。すべての名前は`frame.schema.blend_names`、すべての値は`frame.blend_values`にあります。
- 0未満や100超の値も正しい値です。受信処理で切り詰めないでください。
- `frame.tracking`は、顔を追跡できている間`True`です。顔を見失っている間もフレームは届き、`False`になります。ライブの値では、今カメラで顔を追跡できているかを表します。再生中は、そのフレームと一緒に記録された状態（録画したときに顔を追跡できていたか）を表し、その状態が記録されていない古い録画データの場合だけ、今のカメラの状態を表します。`frame.playback`は、録画データの再生中に`True`です（仕様書3.2節の`flags`）。
- `run()`は呼び出したスレッドを占有します。コールバックは短くし、値は描画処理へ渡してください。
- オプション：`transport="tcp"`、`listen_only=True`、`ios_port=`、`pc_port=`。`fps=`と`udp_size=`（`--fps`、`--udp-size`）は通常開始のHELLOで送る値です。iOSからの手動開始はアプリ側の設定を使い、有効な範囲（1〜60 fps、576〜1200バイト）ならどの値でも受け入れます。

## 7. iPhoneなしで試す

`simulate_ios_v3.py`は、合成した値（ARKitの値ではありません）でiOSアプリの代わりをします。`face_motion_v3.py`と同じフォルダに置き、そのフォルダでターミナルを2つ使います。

**通常開始。** 1台のPCでは、模擬のiPhoneに専用のポート（ここでは51083）が必要です。

```sh
python simulate_ios_v3.py --ios-port 51083                      # ターミナル1
python face_motion_v3.py --host 127.0.0.1 --ios-port 51083      # ターミナル2
```

**手動開始。** 模擬のiPhoneから送り始めます。

```sh
python face_motion_v3.py --listen                               # ターミナル1
python simulate_ios_v3.py --push-host 127.0.0.1                 # ターミナル2
```

TCPで試すときは、**両方の**コマンドに`--transport tcp`を追加します。模擬送信は3個のBlendShapeを送り、30フレーム後に4個目を追加します（対応表の変更）。`--change-after 0`で対応表を1つのままにでき、`--count 52`で52個を送ります。障害を起こすオプション（`--drop-first-schema`、`--mute-after`など）で、受信側（PC）の復旧を試せます。模擬通信の成功は、実際のアプリでの確認の代わりにはなりません。

## 8. うまくいかないとき

- **「port … is already in use」**：そのポートで別の受信プログラムか模擬送信が動いています。それを閉じるか、別の`--pc-port`を指定してください。TCP接続を閉じた直後は、OSが最大1分ほどポートを使用中にすることがあります。
- **`iOS refused the request: 'UNSUPPORTED_CONTRACT: requires 4'`**、または「SCHEMAがrevision 4のアプリのもののようだ」という表示：アプリが古いベータ版（revision 4）です。アプリを更新してください。
- **`iOS refused the request: 'BUSY'`**（または別の文字列）：アプリが開始を断りました。理由の一覧は仕様書の[3.8節](PROTOCOL_V3_JA.md#error)にあります。
- **値が届かない**：iPhoneのアドレス、通信方式、Wi-Fiネットワーク、4章のポートに対するPCのファイアウォール設定を確認してください。Facemotion3dでは、「設定 → その他の機能 → PCからの接続を拒否」がオフになっていることも確認してください（オンの間はTCPの接続ができません）。
- 問題を報告するときは、アプリ名とバージョン、端末、実行したコマンド、最後の`State=`行を添えてください。

FMV3には認証も暗号化もありません。信頼できるLANで使い、これらのポートをインターネットに公開しないでください。

## 9. v3を作った理由

v1/v2は**テキスト**形式で、毎フレームすべてのBlendShapeの名前と値を送ります。読みやすく、始めやすい形式です。[公式のv1/v2資料](https://www.ifacialmocap.com/for-developer/)

v3は、組み込み機器で描画より文字列解析にCPU時間を使っている、という開発者からの相談がきっかけです。v3では、BlendShapeの名前の一覧（並び順）と数値の型を**SCHEMA**で一度だけ送り、各**FRAME**はその一覧と同じ順番のバイナリ数値だけを送ります。一覧は*対応表ごと*に固定され、永久には固定されません。BlendShapeの追加や改名ができ、独自BlendShapeも含められます。

| | v1/v2（テキスト） | v3（対応表＋バイナリ） |
|---|---|---|
| データを目で読む | 簡単 | 復号が必要（このサンプルは復号した値を表示） |
| 最初の受信プログラム | 多くの場合すぐ作れる | 対応表・検証・ACK・復旧の実装が必要 |
| 1フレームごとの処理 | 文字列分割、名前検索、文字列→数値変換 | 位置で数値をコピー |
| 1フレームのデータ | 名前＋文字の数値 | 数値のみ |

**通信量が少ない。** 52個のBlendShapeを`i16`で送ると、**FRAME 1つ192バイト**（ヘッダー40＋値104＋頭と目48）です。60fpsでは、FRAMEだけで**毎秒11,520バイト**です。これは計算値で、測定値ではありません。対応表のやり取り、制御メッセージ、ネットワークのヘッダーの分が少し加わります。通信量が少なくても、それだけで遅延が減ったりフレームレートが上がったりするとは限りません。

**組み込み機器での受信。** v3では毎フレームの文字列解析がなくなりますが、受信側（PC）には対応表の保存、断片の組み立て、検証、UDPのACKが必要です。CPUとメモリは実機で測ってください。Pythonのファイルは参照実装とテスト用の道具です。C、C++など任意の言語で[仕様書](PROTOCOL_V3_JA.md)を実装し、`golden_vectors.json`でバイト列を確認できます。

v3は追加の選択肢です。動いているv1/v2の連携を変える必要はありません。

## 10. ライセンス

このリポジトリのコードと文書は**[MITライセンス](LICENSE)**で公開しています。著作権表示と許諾表示を残せば、商用を含めて利用・改変・再配布できます。

ライセンスの対象はこのリポジトリで、**iOSアプリ本体ではありません**。アプリの購入・ライセンス条件はそのまま適用されます。Facemotion3dでこの機能を使うには**OtherライセンスまたはUnityライセンス**が必要です。
