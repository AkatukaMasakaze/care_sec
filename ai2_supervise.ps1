# ai2 サーバー常駐ランナー(クラッシュ自動復帰+終了記録)
# 使い: powershell -File ai2_supervise.ps1   (止める: この窓で Ctrl+C)
$py   = "C:\Users\student\code\ai2\.venv\Scripts\python.exe"
$root = "C:\Users\student\code\ai2"
$dir  = "C:\Users\student\AppData\Local\Temp\opencode\ai2_srv"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$supLog = Join-Path $dir "supervise.log"

function Say($m) { $line = "{0} {1}" -f (Get-Date -Format "MM-dd HH:mm:ss"), $m; Add-Content -Encoding UTF8 $supLog $line; Write-Host $line }

Say "SUPERVISOR start"
$n = 0
$quick = 0          # 30秒以内の即死が連続した回数(カメラスタック中毒の指標)
while ($true) {
  $n++
  $log = Join-Path $dir ("run{0:d2}.log" -f ($n % 100))   # 直近100周分だけ上書き保存
  Say "RUN #$n -> $log"
  $t0 = Get-Date
  & $py -u "$root\manage.py" runserver 127.0.0.1:8000 --noreload 2>&1 | Out-File -Encoding utf8 $log
  $code = $LASTEXITCODE
  $secs = [int]((Get-Date) - $t0).TotalSeconds
  Say "EXIT #$n code=$code alive=${secs}s"
  if ($secs -ge 180) { $quick = 0 } else { $quick++ }

  # ネイティブクラッシュの終了コードを記録 (0xc0000374=ヒープ破損 他)
  if ($code -ne 0 -and $code -ne -1073741510 -and $code -ne 1) {
    $hex = "0x{0:x8}" -f ($code -band 0xffffffff)
    Say "CRASH-LIKE code=$hex"
  }

  # カメラスタックが死んだハンドルで固まることがあるので解放確認してから再起動
  $waited = 0
  while ($waited -lt 180) {
    & $py -X utf8 -c @"
import time, cv2, sys
free = True
for idx in (0, 1):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    n = 0
    if cap.isOpened():
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 1.0:
            if cap.grab(): n += 1
    cap.release()
    if n == 0: free = False
sys.exit(0 if free else 1)
"@ 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "CAMERA-FREE ( waited ${waited}s )"; break }
    Start-Sleep -Seconds 15
    $waited += 15
    Say "CAMERA-BUSY ${waited}s"
  }
  if ($quick -ge 3) {
    Say "ABORT: 3連続で${secs}秒以内の即死(カメラスタック中毒の可能性大)。PC再起動または管理者PowerShellで 'Restart-Service FrameServer -Force' の后再起動してください"
    break
  }
  Say "RESTART after crash/exit"
  Start-Sleep -Seconds 5
}
