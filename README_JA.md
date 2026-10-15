# Face Motion v3 — 受信サンプル（UDP / TCP）

**iFacialMocap**・**iFacialMocapTr**・**Facemotion3d**から、BlendShapeの値と、頭の回転・位置、目の回転を受け取ります。

[English](README.md) · [仕様書](PROTOCOL_V3_JA.md) · [変更点](CHANGELOG.md) · [MITライセンス](LICENSE)

図（英語）：[接続の流れ](diagrams/fmv3_flow.svg) · [メッセージの構造](diagrams/fmv3_messages.svg)

Python 3.10以上で動き、`pip install`は不要です。実機のiPhone/iPadと通信するだけなら**`face_motion_v3.py`だけ**で足ります。macOSでは必要に応じて`python`を`python3`に置き換えてください。

## 1. UDPで受信する（推奨）

`PHONE_IP`をiPhone/iPadのLAN内アドレスに置き換えます。

```sh
python face_motion_v3.py --host PHONE_IP
```

iFacialMocapTrと**Facemotion3d**も同じコマンドです。Facemotion3dには**OtherライセンスまたはUnityライセンス**が必要です（どちらもない場合、送信は約10秒で止まります）。v3はどちらのライセンスでもOtherの出力（iFacialMocap互換の出力）で送るので、iFacialMocapと同じポートを使い、追加のオプションは要りません。

`State=STREAMING`が表示され、`frames=`が増え続ければ成功です。1フレームの全値を約1秒に1回、1行で表示します（受信はすべてのフレームで行っています）。`--log-every 0`で表示を止められます。終了は**Ctrl+C**です。

**2つのモード。** 受信側（PC）が`--mode`で選びます。このコマンドで始める送信（通常開始）では、選んだモードがそのままアプリに伝わります。

| | 可変モード（既定） | 固定52モード |
|---|---|---|
| 届くBlendShape | アプリが送るすべて（独自BlendShapeも含む）。数と名前はアプリや設定で変わります | ARKitの標準の52個だけ。順番と位置は常に同じです |
| 名前 | 最初に名前の一覧が届きます。通信の途中で一覧が変わることもあります | 送られません（順番は[仕様書](PROTOCOL_V3_JA.md#fixed52-names)で決まっています） |
| 受信側（PC）に必要な処理 | 名前の一覧を読み、一覧の変更に対応する | 決まった位置で読むだけ |

固定52モードで受けるときは、次のようにします。

```sh
python face_motion_v3.py --host PHONE_IP --mode fixed52
```

<details>
<summary>詳細：このサンプルが自動で行うこと（PINGとSCHEMA_ACK）</summary>

どちらのモードでも、受信側（PC）は3秒ごとにPING（受信を続けていることをiOSに知らせる短いメッセージ）を送ります（UDP・TCPとも）。UDPでは、iOSが最初に送るSCHEMA（数値の読み方と、可変モードでは名前の一覧）に、SCHEMA_ACK（受け取ったという返事）も返します。このサンプルはどちらも行います。

</details>

## 2. TCPで受信する

```sh
python face_motion_v3.py --host PHONE_IP --transport tcp
```

使い方はUDPと同じです。ネットワークでUDPが通らない場合に使ってください。

## 3. iOSアプリから開始する（手動開始）

先に受信側（PC）を起動します。受信側（PC）は何も送らずに待ちます。固定52で受けるときは、使うコマンドに`--mode fixed52`を付けます。

```sh
python face_motion_v3.py --listen                    # UDP、このPCのポート49983
python face_motion_v3.py --listen --transport tcp    # TCP、このPCのポート49984
```

次に、アプリにこのPCのLAN内アドレスを設定し、受信側（PC）と同じモードを選んで送信を始めます。アプリに`0.0.0.0`は入力しないでください。

- **iFacialMocap、iFacialMocapTr**：設定 → 「宛先設定」欄の「宛先IPアドレス」にアドレスを入れます。同じ欄の「詳細設定」で、「Protocol」を「Face Motion v3 UDP」（TCPでは「Face Motion v3 TCP/IP」）にし、「Face Motion v3 mode」でモードを選びます。トラッキング画面を開くと送信が始まります。
- **Facemotion3d**：設定 → 「ストリーミング設定」で、ソフトウェア名を「Other」、送信形式を「Face Motion v3」、通信形式を「UDP」（TCPでは「TCP/IP」）にし、右上のボタン（「v3: Variable」または「v3: ARKit 52」）でモードを選びます。「ライブ」ボタンで「Other」を選び、アドレスを入れて「Connect」を押すと送信が始まります。

選択肢の名前は、仕様書の[2章](PROTOCOL_V3_JA.md#udp)の「手動開始」の表にあります。モードの設定は手動開始だけに使われます。通常開始では、受信側（PC）の`--mode`がHELLO（受信側（PC）が最初に送る開始の依頼）でアプリに伝わります。

モードが違うと、受信側（PC）は`SCHEMA mode does not match the requested receiver mode`と表示し、そのSCHEMAを受け入れません。UDPではSCHEMA_ACKを返さないので、アプリは約10秒後に送信を止めます（`SCHEMA_ACK_TIMEOUT`）。TCPでは受信側（PC）が接続を閉じます。

iPhoneからの送信が止まっても、受信側（PC）は終了しません。何度か再試行した後もポートを開いたまま待ち（`State=WAITING`）、アプリからの次の開始を受け付けます。

## 4. ポート

| アプリ | iOSの待受（UDP / TCP） | このPCの待受（UDP / TCP） |
|---|---|---|
| iFacialMocap、iFacialMocapTr、Facemotion3d | 49983 / 49984 | 49983 / 49984 |

`--ios-port`は、PCが接続するiPhone側のポートを変えます。アプリが待ち受けるポートは表のとおりで変えられないので、`--ios-port`は模擬送信（7章）などに使います。`--pc-port`は、このPCが待ち受けるポートを変えます。変えたときは、アプリの手動開始の宛先にも同じポートを入れてください。どちらの側も通信方式ごとに1つのポートだけを使います。PC側のポートを別のプログラムが使っている場合、受信側（PC）はその旨を表示して終了します。別のポートへの自動切替はしません。

## 5. 対応アプリのバージョン

| アプリ | バージョン |
|---|---|
| iFacialMocap | 1.5.3以降 |
| iFacialMocapTr | 1.2.6以降 |
| Facemotion3d | 1.4.6以降（OtherライセンスまたはUnityライセンス） |

これらの版は**FMV3のrevision 6**（この通信方式の版）を使います。revision 5とrevision 4は、App Storeで公開する前のベータ版だけが使っていた版です。revisionが違うアプリと受信側（PC）は一緒に動かないので、両方を更新してください（違いは仕様書の[互換性の章](PROTOCOL_V3_JA.md#compat)）。このサンプルはv3専用で、v1/v2のテキスト形式は読みません。

## 6. 自分のプログラムで値を使う

値は表示された行からではなく、コールバックから読みます。BlendShapeの名前は、対応表（SCHEMA。数値の読み方と、可変モードではBlendShapeの名前の一覧）が届いたときに一度だけ調べ、フレームごとには何番目かで読みます。

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

- **可変モード**（既定）：BlendShapeの数と名前は、アプリや設定によって変わります。BlendShapeの一覧が違う録画データの再生を始めたときなど、通信中に一覧が変わることもあります（そのたびに`on_schema`が呼ばれます）。すべての名前は`frame.schema.blend_names`、すべての値は`frame.blend_values`にあります（`schema.mode`は`"variable"`）。
- **名前の書き方**（可変モード）：標準の52個は、どのアプリも`eyeBlink_L`の書き方（ARKitが返す文字列。仕様書の[固定52の表](PROTOCOL_V3_JA.md#fixed52-names)の「ARKitが返す文字列」の列）で送ります。例の`jawOpen`や`jawLeft`、`mouthLeft`のように、その列で（同じ）の名前はそのままです。Facemotion3dは、名前の置き換え設定をしている場合は置き換えた名前で送り、`FM_SAD`などの独自の名前や音声入力の名前も送ります。
- **固定52モード**：[ARKitの標準の52個](PROTOCOL_V3_JA.md#fixed52-names)が、常に同じ順番でInt32（4バイトの符号付き整数）として届きます（`schema_id = 1`）。アプリにないBlendShapeは0になり、独自BlendShapeは送られません。値には、アプリ側の倍率やキャリブレーションなどの調整が反映されます。名前は送られてこないので、このサンプルが標準の52個の名前を`frame.schema.blend_names`に入れます（`schema.mode`は`"fixed52"`、`app`と`profile`は空文字列）。この名前は、可変モードの`eyeBlink_L`ではなく、`eyeBlinkLeft`のような識別子の書き方です。
- 0未満や100超の値も正しい値です。受信処理で切り詰めないでください。
- `run()`は呼び出したスレッドを占有します。コールバックは短くし、値は描画処理へ渡してください。

<details>
<summary>詳細：trackingとplaybackの値、オプション、手動開始での設定値</summary>

- `frame.tracking`は、顔を追跡できている間`True`です。顔を見失っている間もフレームは届き、`False`になります。ライブでも録画データの再生中でも、今カメラで顔を追跡できているかを表します（アプリは録画データに追跡の状態を保存しません）。`frame.playback`は、録画データの再生中に`True`です（仕様書の[ヘッダーの`flags`](PROTOCOL_V3_JA.md#header)）。
- オプション：`mode="variable"`（既定）または`mode="fixed52"`、`transport="tcp"`、`listen_only=True`、`ios_port=`、`pc_port=`。固定52の例：`V3Client("PHONE_IP", mode="fixed52").run(on_frame, on_schema=on_schema)`。
- `fps=`と`udp_size=`（`--fps`、`--udp-size`）は、通常開始でHELLOに入れて送る値です。手動開始ではアプリ側の設定が使われ、受信側（PC）は有効範囲（1〜60 fps、576〜1200バイト）なら受け入れます。モードだけは受信側（PC）と一致する必要があります（3章）。

</details>

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

<details>
<summary>詳細：固定52、TCP、復旧などを試すとき</summary>

**固定52。** 通常開始では受信側（PC）だけに`--mode fixed52`を付けます（模擬送信のモードはHELLOで決まります）。手動開始では両方に付けます。

```sh
# 通常開始
python simulate_ios_v3.py --ios-port 51083                                    # ターミナル1
python face_motion_v3.py --host 127.0.0.1 --ios-port 51083 --mode fixed52     # ターミナル2
# 手動開始
python face_motion_v3.py --listen --mode fixed52                              # ターミナル1
python simulate_ios_v3.py --push-host 127.0.0.1 --mode fixed52                # ターミナル2
```

可変モードの模擬送信は3個のBlendShapeを送り、30フレーム後に4個目を追加します（対応表の変更）。固定52では標準の52個を送り、`--count`と`--change-after`は使いません。

| 試すこと | 追加するオプション |
|---|---|
| TCP | **両方の**コマンドに`--transport tcp` |
| 対応表を1つのままにする（可変モード） | 模擬送信に`--change-after 0` |
| 可変モードで、合成した名前を52個送る | 模擬送信に`--count 52`（固定52を選ぶものではありません） |
| 受信側（PC）の復旧 | 模擬送信に障害を起こすオプション（`--drop-first-schema`、`--mute-after`など） |

</details>

模擬通信の成功は、実際のアプリでの確認の代わりにはなりません。

## 8. うまくいかないとき

- **「port … is already in use」**：そのポートで別の受信プログラムか模擬送信が動いています。それを閉じるか、別の`--pc-port`を指定してください。TCP接続を閉じた直後は、OSが最大1分ほどポートを使用中にすることがあります。
- **`SCHEMA mode does not match the requested receiver mode`**：手動開始で、アプリと受信側（PC）のモードが違います。3章の場所で、アプリのモードを受信側（PC）の`--mode`に合わせてください。
- **`iOS refused the request: 'BUSY'`**（`UNSUPPORTED_MODE`など、別の文字列のこともあります）：アプリが開始を断りました。理由の一覧は仕様書の[ERRORの節](PROTOCOL_V3_JA.md#error)にあります。
- **値が届かない**：iPhoneのアドレス、通信方式、Wi-Fiネットワーク、4章のポートに対するPCのファイアウォール設定を確認してください。Facemotion3dでは、「設定 → その他の機能 → PCからの接続を拒否」がオフになっていることも確認してください（オンの間はTCPの接続ができません）。
- 問題を報告するときは、アプリ名とバージョン、端末、実行したコマンド、最後の`State=`行を添えてください。

<details>
<summary>詳細：revisionが違うと表示されたとき</summary>

- **revisionが違う**：手動開始で`iOS uses FMV3 contract revision 5; this receiver needs revision 6`と表示されたときは、アプリが古いベータ版（revision 5）です。通常開始で何も届かずに`State=WAITING`になるときも、原因の1つとして古いベータ版が考えられます。revision 5のアプリは新しいHELLOを不正なデータとして捨てる（TCPでは接続を閉じる）ので、理由は返ってきません。アプリを更新してください。

</details>

FMV3には認証も暗号化もありません。信頼できるLANで使い、これらのポートをインターネットに公開しないでください。

## 9. v3を作った理由

v1/v2は**テキスト**形式で、毎フレームすべてのBlendShapeの名前と値を送ります。読みやすく、始めやすい形式です。[公式のv1/v2資料](https://www.ifacialmocap.com/for-developer/)

v3は、組み込み機器で描画より文字列解析にCPU時間を使っている、という開発者からの相談がきっかけです。v3では、数値の型と、BlendShapeの名前の一覧（並び順。可変モードのみ）を**SCHEMA**で一度だけ送り、各**FRAME**はその順番のバイナリ数値と、頭・目の値だけを送ります。可変モード（既定）では、一覧が固定されるのは1つの対応表の間だけです。一覧が変わるときは新しい対応表が届くので、BlendShapeの追加や改名、独自BlendShapeにも対応できます。固定52モードでは名前の一覧を送らず、ARKitの標準の52個が常に同じ位置に入ります（1章の表）。

<details>
<summary>詳細：v1/v2との比較と、通信量</summary>

| | v1/v2（テキスト） | v3（対応表＋バイナリ） |
|---|---|---|
| データを目で読む | 簡単 | 復号が必要（このサンプルは復号した値を表示） |
| 最初の受信プログラム | 多くの場合すぐ作れる | 可変：対応表の保存と、分割されたSCHEMAの組み立て。固定52：決まった位置で読むだけ。両方：検証、UDPのSCHEMA_ACK、PING、復旧 |
| 1フレームごとの処理 | 文字列分割、名前検索、文字列→数値変換 | 位置で数値をコピー |
| 1フレームのデータ | 名前＋文字の数値 | 数値のみ |

**通信量が少ない。** 52個のBlendShapeを`i16`（2バイトの符号付き整数）で送ると、**FRAME 1つ192バイト**（ヘッダー40＋値104＋頭と目48）です。60fpsでは、FRAMEだけで**毎秒11,520バイト**です。これは計算値で、測定値ではありません。対応表のやり取り、制御メッセージ、ネットワークのヘッダーの分が少し加わります。通信量が少なくても、それだけで遅延が減ったりフレームレートが上がったりするとは限りません。

</details>

**組み込み機器での受信。** 可変モードでは、名前の一覧の保存と、分割されて届くSCHEMAの組み立てが必要です。固定52ではどちらも不要で、SCHEMAは合計66バイト、FRAMEは合計296バイトです。どちらも分割されずに1回で届きます。固定52のFRAMEは、60fpsで毎秒17,760バイトです（制御メッセージとネットワークのヘッダーを除く計算値）。どちらのモードでも、検証、UDPのSCHEMA_ACK、PINGは必要です。CPUとメモリは実機で測ってください。Pythonのファイルは参照実装とテスト用の道具です。C、C++など任意の言語で[仕様書](PROTOCOL_V3_JA.md)を実装してください。バイト列は、C99で書いた正解データ（`golden_vectors.h`・`golden_vectors.c`）を自分のプログラムと一緒にコンパイルして確認できます。`golden_vectors_check.c`はその例で、必要なのはCコンパイラーだけです：`cc -std=c99 -o golden_vectors_check golden_vectors.c golden_vectors_check.c && ./golden_vectors_check`

v3は追加の選択肢です。動いているv1/v2の連携を変える必要はありません。

## 10. ライセンス

このリポジトリのコードと文書は**[MITライセンス](LICENSE)**で公開しています。著作権表示と許諾表示を残せば、商用を含めて利用・改変・再配布できます。

ライセンスの対象はこのリポジトリで、**iOSアプリ本体ではありません**。アプリの購入・ライセンス条件はそのまま適用されます。Facemotion3dでこの機能を使うには**OtherライセンスまたはUnityライセンス**が必要です。
