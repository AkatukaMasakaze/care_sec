# CARE SEC AWS構成図(コスト最適化版)

## システム構成図

```mermaid
graph TB
    subgraph "介護施設(ローカル)"
        CAM1[監視カメラ 1]
        CAM2[監視カメラ 2]
        CAM3[監視カメラ 3]
        CAM4[監視カメラ 4]
        GW[施設ゲートウェイ<br/>TLS終端]
    end

    subgraph "AWS (最小構成)"
        subgraph "EC2 インスタンス 1台<br/>t3.small ($15/月)"
            NGINX[Nginx<br/>SSL終端<br/>リバースプロキシ]
            MEDIA[MediaMTX<br/>メディアサーバー]
            DJANGO[Django アプリ<br/>CARE SEC]
            AI[AI処理モジュール]
            SQLITE[(SQLite DB<br/>暗号化済み<br/>EBS暗号化)]
        end

        S3[(S3 バケット<br/>録画データのみ<br/>$5/月)]
        ELB[Route 53<br/>DNSのみ<br/>$0.50/月]
    end

    subgraph "スタッフ端末"
        PC[スタッフPC/スマホ]
    end

    CAM1 -->|RTSP/TLS| GW
    CAM2 -->|RTSP/TLS| GW
    CAM3 -->|RTSP/TLS| GW
    CAM4 -->|RTSP/TLS| GW

    GW -->|VPN/IPsec| EC2

    EC2 -->|HTTPS:443| PC

    NGINX -->|/stream/| MEDIA
    NGINX -->|/api/, /admin/| DJANGO

    MEDIA -->|映像処理| AI
    AI -->|異常検知| DJANGO
    AI -->|録画| MEDIA

    MEDIA -->|録画保存| S3
    DJANGO -->|DB操作| SQLITE
    DJANGO -->|録画URL| S3
```

---

## データフロー図

```mermaid
sequenceDiagram
    participant CAM as 監視カメラ
    participant GW as 施設GW
    participant EC2 as EC2 (t3.small)
    participant S3 as S3
    participant STAFF as スタッフ

    CAM->>GW: RTSP/TLS (暗号化)
    GW->>EC2: VPN/IPsec

    EC2->>EC2: Nginx受信 (HTTPS:443)
    EC2->>EC2: MediaMTX (映像処理)
    EC2->>EC2: AI検知 (転倒/不動)
    EC2->>EC2: Django (アラート生成)

    EC2->>S3: 録画保存 (MP4)
    S3-->>EC2: 保存完了

    STAFF->>EC2: HTTPS (ブラウザ)
    EC2->>EC2: Django (画面表示)
    EC2->>EC2: SQLite (暗号化DB参照)
    EC2->>S3: 録画URL生成 (署名付き)
    S3-->>STAFF: 録画再生
```

---

## コスト構成

| リソース | 仕様 | 月額 |
|---|---|---|
| EC2 | t3.small (2vCPU, 2GB) | $15 |
| EBS | 20GB gp3 (暗号化) | $2 |
| S3 | Standard (録画) | $5 |
| Route 53 | ホストゾーン | $0.50 |
| **合計** | - | **約$22.50/月** |

---

## 暗号化構成

```
[SQLite DBファイル]
    ↓ django-encrypted-sqlite で暗号化
[暗号化DBファイル]
    ↓ EBSボリューム暗号化(AES-256)
[AWSディスク上]
```
