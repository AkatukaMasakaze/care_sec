# CARE SEC カメラ映像パイプライン構築手順(Pi3 → EC2)

構成: Pi3 は映像を**送るだけ**(RTSPプッシュ)。EC2 側で MediaMTX が受け、
ai2 がローカルループバックで読んで解析・録画・通知まで全部やる。
接続は常に「施設 → AWS」の一方通行(施設側にポートを開かない)。

関連: `AWS Setup Guide.html` 第12章(全体像)/ 12.4(流れ図)/ 第8章(メディア保護)

---

## 0. 事前チェック(5分)

### Pi3 側
```bash
uname -m                 # armv7l = Pi3
lsb_release -rs          # 12=Bookworm(rpicam-vid) / 11=Bullseye(libcamera-vid) / 10以下=Buster(raspivid)
vcgencmd get_camera      # supported=1 detected=1 が正(detected=0 ならケーブル/有効化)
# カメラが映るか(2秒表示)
rpicam-hello -t 2s || libcamera-hello -t 2s || raspistill -t 2000
# 時刻とタイムゾーン(ログの時刻が狂わないように)
sudo timedatectl set-timezone Asia/Tokyo
# 施設のグローバルIPを確認(このIPをAWSのSGで許可する)
curl -s ifconfig.me; echo
```
- **可能なら有線LAN**。WiFiだと切断=映像断で介護の常時監視には不適
- 上の `curl ifconfig.me` の結果を控える(=SGに入れる `X.X.X.X/32`)

### EC2 側
```bash
curl -s ifconfig.me      # = Elastic IP(54.172.181.162)
```

---

## 1. EC2: MediaMTX(受信中継)をたてる 〔ubuntu帽子〕

```bash
cd /usr/local/src
VER=$(curl -s https://api.github.com/repos/bluenviron/mediamtx/releases/latest | grep -oP '"tag_name":\s*"v\K[0-9.]+')
echo "latest=$VER"
curl -sL -o mediamtx.tar.gz "https://github.com/bluenviron/mediamtx/releases/download/v${VER}/mediamtx_v${VER}_linux_amd64.tar.gz"
sudo tar xzf mediamtx.tar.gz -C /usr/local/bin/
/usr/local/bin/mediamtx --version          # バージョンが出ればOK

sudo mkdir -p /etc/mediamtx
sudo /usr/local/bin/mediamtx --dump-config | sudo tee /etc/mediamtx/mediamtx.yml >/dev/null
```

### 1.1 鍵を作って paths 設定

```bash
PW1=$(openssl rand -base64 15)     # cam1 publish鍵
PW2=$(openssl rand -base64 15)     # cam2 publish鍵
PWREAD=$(openssl rand -base64 15)  # ai2(read)鍵
sudo bash -c "cat > /etc/mediamtx/keys.txt <<EOF
PW1=$PW1
PW2=$PW2
PWREAD=$PWREAD
EOF"
sudo chmod 600 /etc/mediamtx/keys.txt

sudo bash -c "cat > /etc/mediamtx/mediamtx-paths.yml <<EOF
paths:
  cam1:
    publish:
      user: carecam
      passwords: [\"$PW1\"]
    read:
      user: careserve
      passwords: [\"$PWREAD\"]
  cam2:
    publish:
      user: carecam
      passwords: [\"$PW2\"]
    read:
      user: careserve
      passwords: [\"$PWREAD\"]
EOF"
echo "include: [ mediamtx-paths.yml ]" | sudo tee -a /etc/mediamtx/mediamtx.yml >/dev/null
```

### 1.2 systemd常駐

```bash
sudo bash -c "cat > /etc/systemd/system/mediamtx.service <<'EOF'
[Unit]
Description=MediaMTX (camera ingest)
After=network-online.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF"
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
sudo ss -ltn | grep 8554            # 0.0.0.0:8554 が待ち受けていればOK(実開放はSGが絞る)
```

### 1.3 AWSセキュリティグループ(コンソール)— 2パターン

**パターンS(施設IPが固定)**:

| タイプ | ポート | 送信元 |
|---|---|---|
| カスタムTCP | 8554 | **`<施設のグローバルIP>/32`**(Piで `curl -s ifconfig.me`) |

**パターンT(施設IPが動的 → 本運用の想定。推奨)**:

| タイプ | ポート | 送信元 | 意味 |
|---|---|---|---|
| カスタムUDP | 41641 | `0.0.0.0/0` | TailscaleのWireGuard直結用。**中身は鍵認証済みしか通らない**ので 0.0.0.0/0 は安全側の例外 |
| (8554は**開かない**) | — | — | 映像は全部トンネル内(100.64.x)を流れる。公网にはRTSPを一切晒さない |

---

## 1.5 パターンT: Tailscale で「動的IP問題」を消す(両端で計5分)

### EC2側 〔ubuntu帽子〕
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up            # 表示されるURLをブラウザで開いて承認
tailscale ip -4              # → 100.64.x.x(EC2のtailnet固定IP)を控える ★
```

### Pi3側
```bash
curl -fsSL https://tailscale.com/install.sh | sh
# 承認URLを開けないヘッドレスPiは、管理画面で生成した auth key で一発登録:
sudo tailscale up --authkey tskey-【GitHub同期用の別アカウントではなくtailnetで発行】
tailscale ip -4              # Pi側の 100.64.y.y
# 疎通テスト
ping -c 2 $(tailscale ip -4 | head -1)   # 自分のPiからEC2の100.64.x.xへ
ping -c 2 100.64.x.x                      # ★のEC2 IP
```

### MediaMTXはそのまま、Piの送り先だけ tailnet IP に
```
ExecStart=/usr/bin/rpicam-vid -t 0 --width 640 --height 480 --fps 10 --codec h264 --inline \
  -o rtsp://carecam:【cam1鍵】@100.64.x.x:8554/cam1
```
- 施設のグローバルIPがどう変わっても **Pi→EC2 の接続はTailscaleが再確立**する(`Restart=always` と併せて自己回復)
- MediaMTXのログに出る publisher の送信元は `100.64.y.y` になる

### 運用メモ(パターンT)
- tailnet IP(100.64.x)は**ノードごとに安定**。台帳URL・firewall設定はIP固定運用と同じ扱いで書ける
- Tailscale ACL(管理コンソール)で `tag:ec2, tag:pi` だけ相互通信可に絞るのが本番作法。デフォルトallow-allのままでも動く
- **device key の期限**: 既定180日。`tailscale up` 時に「Disable key expiry」を有効にするか、切れる前に更新(切れると再接続不能=映像断になる)
- hole punch 失敗時は DERP 中継で動く(映像は流れるが遅延増)。無料Personal枠で台数3ユーザー程度ならまず問題なし
- 将来「Pull型(EC2がPiのRTSPを開く)」に切り替えたくなっても、このtailnetのまま `rtsp://100.64.y.y:8554/…` でいける

---

## 2. Pi3: 送信サービスを入れる 〔piユーザー〕

OS 12(Bookworm)前提。11以下は末尾の代替行へ差し替え。

```bash
PW1=【1.1のcam1鍵】
sudo bash -c "cat > /etc/systemd/system/cam-push.service <<EOF
[Unit]
Description=CARE SEC camera push
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/rpicam-vid -t 0 --width 640 --height 480 --fps 10 --codec h264 --inline -o rtsp://carecam:${PW1}@54.172.181.162:8554/cam1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF"
# ※ sudo bash -c 内で ${PW1} を展開させたいので、上記は「ダブルクォート」が正しい。
#    鍵はファイルに残る(600にする: sudo chmod 600 /etc/systemd/system/cam-push.service)

sudo systemctl daemon-reload
sudo systemctl enable --now cam-push
systemctl status cam-push --no-pager | head -5     # active (running)
```

2台目がある場合: `/etc/mediamtx/keys.txt` の `PW2` を使い、`cam2` 名義で同じサービスをもう1個。

> **宛先の選び方**: パターンS(施設IP固定)= `54.172.181.162` / パターンT(動的IP・推奨)= EC2の **tailnet IP `100.64.x.x`**(1.5で控えた★)。それ以外は同一。

**代替(古いOS)**:
- Bullseye(11): `libcamera-vid -t 0 --width 640 --height 480 --fps 10 --codec h264 --inline -o rtsp://…`
- Buster(10): `raspivid -t 0 -w 640 -h 480 -fps 10 -pf high -o rtsp://…`

---

## 3. 疎通確認(この順で)

### 3.1 EC2: 届いているか
```bash
sudo journalctl -u mediamtx --no-pager | tail -8
# 期待: client <IP> is publishing to path 'cam1'
```

### 3.2 EC2: 読めるか(ffmpeg を1回だけ導入)
```bash
sudo apt install -y ffmpeg
PWREAD=$(grep PWREAD /etc/mediamtx/keys.txt | cut -d= -f2)
ffprobe -rtsp_transport tcp "rtsp://careserve:${PWREAD}@127.0.0.1:8554/cam1" 2>&1 | grep -E "Stream|Video"
# 期待: Video: h264 … 640x480 …
```

### 3.3 ai2 台帳登録(管理画面 or shell)
画面: カメラ管理 → 編集 → `stream_url`:
```
rtsp://careserve:【PWREAD】@127.0.0.1:8554/cam1
```
shellでやるなら:
```bash
sudo -u caresec -H bash
cd /srv/caresec && source .venv/bin/activate
python manage.py shell -c "
from core.models import Camera
PWREAD=open('/etc/mediamtx/keys.txt').read().split('PWREAD=')[1].split()[0]
c, created = Camera.objects.get_or_create(name='Pi3-cam1', defaults={'location':'棟A 居室1'})
c.stream_url = f'rtsp://careserve:{PWREAD}@127.0.0.1:8554/cam1'
c.save()
print('camera', c.id, c.name, 'created' if created else 'updated')
"
```

### 3.4 ai2 が解析を始めるか
```bash
sudo journalctl -u caresec --no-pager | grep -E "RealtimeAI|カメラ" | tail -6
# 期待: [RealtimeAI] 常時稼働を開始しました: index ['cam:…'] / 解析稼働中(fps=…)
```
ブラウザ `/debug/camera/` でも `cam:…` のカードと映像が出る。

---

## 4. つまずり早見表

| 症状 | 原因 | 処方 |
|---|---|---|
| journalctl mediamtx に publisher が出ない | SG 8554/施設IP/32 が違うIPになっている | `curl -s ifconfig.me` を施設側で再実行→SG更新 |
| Piの cam-push が `Connection refused` 連発 | EC2側 mediamtx が死んでいる/RTSPはTCPのみ想定 | `systemctl status mediamtx`。PiのExecStartに `--rtsp-transport tcp` は不要(rpicam-vid既定TCP) |
| publish OK / ffprobe がハング | UDP経路問題(EC2内loopbackでは起きないはず) | ffprobe/cv2は `rtsp_transport tcp` を明示(上のコマンドは既にtcp) |
| ai2がフレームを取れない | read鍵の不一致/URLのuser名ミス | keys.txt の PWREAD をコピペし直す。`cam:` キーで検知されるか /debug/camera/devices/ 参照 |
| fpsは出るが pose が 0/s | 映像が真っ暗/画角に人が居ない | 正常。MediaPipeは人を見てから動く |
| 施設IPが変わって全断 | 動的IPでパターンSを使っていた | **パターンT(Tailscale)に移行**。dstは100.64.xのまま不変で、トンネルが自動再確立する |

---

## 5. 運用メモ

- 鍵は `/etc/mediamtx/keys.txt`(EC2・600)と cam-push.service(600)にのみ。GitHubには**絶対にコミットしない**
- PiのSDカードは消耗品: 半年に1回クローン(`ddrescue`/Imager)を作ると復旧が早い
- 帯域目安: 640x480@10fps H.264 ≈ 0.8〜1.5Mbps/台。回線実測(iperf3)で台数を決める
- 映像の遅延: Pi符号化→MediaMTX→ai2 で体感0.3〜0.8秒。介護見守り用途では十分
