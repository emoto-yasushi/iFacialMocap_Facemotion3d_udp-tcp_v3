# Face Motion v3 — Python2本の通信サンプル

**iFacialMocap・iFacialMocapTr・Facemotion3dから、BlendShapeと頭・左右の目の数値をUDPまたはTCPで受信します。**

[English](README.md)

## 対応アプリと最低バージョン

| アプリ | 対応バージョン |
|---|---|
| **iFacialMocap** | **1.5.3以降** |
| **iFacialMocapTr** | **1.2.6以降** |
| **Facemotion3d** | **1.4.6以降** |

**このサンプルは、上記の各バージョン以降のアプリのみ対応しています。それ以前のバージョンには対応していません。** 最低バージョン未満のアプリをお使いの場合は、アプリを更新してから実行してください。

| ファイル | 使う場面 |
|---|---|
| **`face_motion_v3.py`** | 受信プログラム。**実際のiPhone／iPadと通信するなら、この1本だけで動きます。** |
| **`simulate_ios_v3.py`** | iPhoneなしで試すための模擬送信プログラム。使う場合は、上の受信ファイルと同じフォルダへ置きます。 |

**Python 3.10以上。`pip install`は不要です。** ZIPを展開し、このフォルダを作業場所にしてターミナルを開いてください。Macで`python`が動かなければ、以下のコマンドを`python3`に置き換えます。UDP・TCPはどちらか一方を選び、同じポートで受信プログラムを二重起動しないでください。

## 1. 実際のiPhone／iPadから受信する

iOSアプリが、**標準ポートのFMV3 `contract_revision = 4`** に対応している必要があります。このサンプルでは旧v1/v2の文字列データは読めません。実機と通信するときは、**模擬送信プログラムは起動しません。**

`PHONE_IP`をiPhone／iPadのLAN内IPアドレスへ置き換えます。

```sh
python face_motion_v3.py --host PHONE_IP --transport udp
# TCPの場合は、末尾のudpをtcpへ置き換えます。
```

Facemotion3dでは、`--app facemotion3d`を追加してください。開始要求HELLOや、UDPの対応表ACKは受信プログラムが自動で処理します。

**成功の目印：** `State=STREAMING`が出て、`frames=`の数が増えます。約1秒に1回、1フレームの**全項目**を1行で表示します。受信・解析は正常な各フレームで行い、表示だけを間引きます。ターミナルの幅によっては見た目が折り返されます。終了は**Ctrl+C**。`--log-every 0`は値の表示だけを止める指定です。

## 2. iPhoneなしで試す — ターミナルを2つ使う

**同じフォルダでターミナルを2つ開き、次の順番で起動**してください。

**ターミナル1：iPhoneの代わりに合成データを送信**

```sh
python simulate_ios_v3.py --transport udp --port 51083 --count 52 --change-after 0
```

**ターミナル2：そのデータを受信**

```sh
python face_motion_v3.py --host 127.0.0.1 --transport udp --port 51083
```

TCPを試す場合は、**両方のコマンドの`udp`を`tcp`に変更**します。終了するときは、**両方のターミナルでCtrl+C**を押してください。

`127.0.0.1`は同じPCを指します。**51083は模擬送信だけのテスト用ポートであり、iOSの標準ポートを変更したわけではありません。** 同じPC上の送信・受信で待受ポートが衝突しないように指定しています。受信側のローカルポートは通常どおりUDP49983／手動TCP待受49986です。

この例では**52項目の合成データ**を送ります。ARKitの標準52項目を忠実に再現するものではありません。`jawOpen`は変化し、ほかの多くの値は意図的に固定しています。`count=52`で受信数を確認できます。`--change-after 0`は対応表の変更を無効にする指定です。この指定を外すと、既定では30フレーム送信後に独自項目が1つ増えます。

模擬送信は**iOS／ARKitのエミュレーターではなく、カメラも使いません。** ACK待ち・再送処理はファイル内へ統合済みで、`schema_delivery.py`は不要です。

## 3. iOSのIP手入力モードから送信する

まずPCを待機させ、その後iOSアプリのv3手動送信で、**PCのLAN内IPと受信ポート**を指定します。

```sh
python face_motion_v3.py --listen --transport udp   # PCのUDP49983へ送信
# または
python face_motion_v3.py --listen --transport tcp   # PCのTCP49986へ接続
```

`0.0.0.0`を送信先IPとして入力しないでください。UDPは対応表にACKを返し、TCPはアプリ層ACKなしで接続を使います。復旧の再試行を使い切っても、PCは受信待機を続けます。

| アプリ | iOS側UDP | PC側UDP | iOS側の直接TCP待受 | PC側の手動TCP待受 |
|---|---:|---:|---:|---:|
| iFacialMocap | 49983 | 49983 | 49984 | 49986 |
| Facemotion3d | 49993 | 49983 | 49994 | 49986 |

`--port`はiOS／模擬送信側の接続先、`--listen-port`は受信プログラムのローカルポートです。通常TCPではPCから張った1本の接続で送受信します。PC49986は手動接続の受入用で、通常通信の返答用に2本目の接続を張るという意味ではありません。

## 4. 自分のプロジェクトで値を使う

`face_motion_v3`から`V3Client`を読み込み、**ログ文字列を解析せず、コールバックの数値を使います。** 対応表を受け取ったときに項目の位置を調べ、各フレームではその位置から値を取り出します。

```python
from face_motion_v3 import V3Client

jaw_index = None

def on_schema(schema):
    global jaw_index
    jaw_index = schema.index_by_name.get("jawOpen")

def on_frame(frame):
    if jaw_index is not None and frame.tracking:
        jaw = frame.blend_values[jaw_index] / 100.0
        # jawを自分の描画処理へ渡す。受信ループを長時間止めない。

V3Client("PHONE_IP", transport="udp").run(on_frame, on_schema=on_schema)
```

項目数は**52固定ではありません**。名前は`frame.schema.blend_names`、同じ順番の全値は`frame.blend_values`、頭と目は`frame.head`・`frame.right_eye`・`frame.left_eye`です。負値も扱い、`-25`は係数`-0.25`に相当します。受信段階で負値や100超を切り捨てません。`.run()`は呼び出したスレッドを使い続けるため、GUIへの統合では受信と画面更新のスレッドを適切に分けてください。

## 詳細資料・困ったとき

[正式な通信仕様](PROTOCOL_V3.md) ／ [送信データ構造図](diagrams/FMV3_PC_to_iOS_EN.png) ／ [受信データ構造図](diagrams/FMV3_iOS_to_PC_EN.png) ／ [画像の英語テキスト](diagrams/DIAGRAM_TEXT_EN.md) ／ [期待バイト列](golden_vectors.json)

資料・画像・期待バイト列は**実行に必要なファイルではありません**。期待バイト列は別言語で実装する際の答え合わせ用です。保守用の`tests/`や過去の`test_results/`は、この小規模サンプルから外しています。模擬通信の成功だけで実機試験が完了したことにはなりません。

ポート使用中のエラーが出たら、既存の受信アプリを通常の方法で終了してください。値が届かなければ、iOSの対応ビルド・IP・通信方式・ファイアウォールを確認します。問い合わせには、アプリ名／ビルド、端末／OS、コマンド、最後に進んだ状態（`SCHEMA`・`WAIT_FRAME`・`STREAMING`・`RECOVERING`等）を添えてください。

認証・暗号化はありません。信頼できるLANで使用し、ポートをインターネットへ公開しないでください。

**ライセンス：** 配布者による利用条件は未選択です。今回のファイル整理で新しい利用許諾を設定していません。
