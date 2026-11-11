"""AIモデルの自動再学習(runserver 起動時にデータ変更があれば学習して反映する)

手動コマンド(`manage.py ai_*`)と同じ処理を、**サーバー起動時に自動**で実行します。

判定の流れ:
  1. `train_data/*.csv` の指紋(ファイル名・サイズ・更新時刻のハッシュ)を
     `train_dataset/retrain_state.json` の記録と比較
  2. 変わっていなければ **何もしない**(数秒で終わる。autoreloadの再起動でも安全)
  3. 変わっていたら:
     - データセット再生成 → 候補モデルを学習(`ai_models/candidates/`)
     - 同じ保持データで **現行モデルと候補を比較**
     - 劣化がなければ `--promote` 相当の反映(旧モデルは `ai_models/backup/` へ)
     - 結果を `retrain_state.json` に記録

`core/apps.py` の起動処理から、**常時稼働を開始する前**に呼ばれます
(学習はCPUを多く使うため、解析と同時走らせないため)。
"""
import time

from django.conf import settings

from core import training


def _log(message):
    print(f"[Retrain] {message}", flush=True)


def enabled():
    """自動再学習が有効か"""
    return bool(getattr(settings, "AI_AUTO_RETRAIN", True))


_enabled = enabled


def _threshold():
    """運用閾値(AI設定の転倒検知信頼度閾値)。DBが使えない環境では 0.5"""
    try:
        from core.models import AISettings

        return float(AISettings.load().fall_threshold)
    except Exception:  # noqa: BLE001
        return 0.5


def needs_retrain():
    """データが変わったか(またはモデルが未作成か)を返す"""
    import numpy as np  # noqa: F401  (依存の確認だけ行う)

    finger = training.data_fingerprint()
    if finger["files"] == 0:
        return False, finger, "収集CSVが無いため対象外"

    state = training.read_state()
    models_exist = (training.model_dir() / training.FALL_MODEL_FILENAME).exists()
    if not models_exist:
        return True, finger, "現行モデルが無いため初期学習"
    if state.get("fingerprint") != finger["hash"]:
        before = state.get("fingerprint", "(記録なし)")
        return True, finger, f"データ変更を検知({before} → {finger['hash']})"
    return False, finger, f"データ変更なし({finger['files']}ファイル)"


def run_startup_retraining(force=False):
    """起動時自動再学習のエントリ。戻り値は実行したかどうか"""
    if not _enabled() and not force:
        _log("自動再学習は無効です(AI_AUTO_RETRAIN=False)")
        return False

    need, finger, reason = needs_retrain()
    if not need and not force:
        _log(reason)
        return False

    _log(f"再学習を開始します({reason}) / 対象 {finger['files']}ファイル "
         f"{round(finger['bytes'] / 1048576, 1)}MB。数分かかる場合があります。")
    started = time.monotonic()
    results = {"fingerprint": finger["hash"], "data_files": finger["files"],
               "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "forced": bool(force)}
    try:
        results["fall"] = _retrain_fall()
        results["state"] = _retrain_state()
    except Exception as exc:  # noqa: BLE001 -学習失敗でもWebアプリ/解析は止めない
        results["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"再学習に失敗しました(現行モデルはそのままです): {results['error']}")

    results["elapsed_seconds"] = round(time.monotonic() - started, 1)
    results["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    training.write_state({**training.read_state(), **results,
                          "fingerprint": finger["hash"],
                          "last_fingerprint": finger["hash"]})
    _log(f"完了({results['elapsed_seconds']}秒)")
    return True


# ================= 転倒モデル =================
def _retrain_fall():
    import numpy as np

    X, y, rows, skipped = training.build_fall_dataset()
    if len(X) == 0:
        raise RuntimeError("train_data/*.csv からシーケンスを作れませんでした")
    np.save(training.dataset_path("X.npy"), X)
    np.save(training.dataset_path("y.npy"), y)

    threshold = _threshold()
    model, metrics, (X_test, y_test, _prob) = training.train_fall(
        X, y,
        epochs=int(getattr(settings, "AI_RETRAIN_EPOCHS_FALL", 50)),
        verbose=0,
    )
    candidate_path = training.candidate_dir() / training.FALL_MODEL_FILENAME
    model.save(candidate_path)
    candidate = training.evaluate_fall_at_threshold(model, X_test, y_test, threshold)

    baseline = None
    current = training.model_dir() / training.FALL_MODEL_FILENAME
    if current.exists():
        try:
            baseline = training.evaluate_fall_at_threshold(
                training.load_model(current), X_test, y_test, threshold)
        except Exception as exc:  # noqa: BLE001
            _log(f"現行転倒モデルの評価に失敗(比較不能): {exc}")

    promoted = _should_promote(baseline, candidate)
    if promoted:
        dst, backed_up = training.promote(training.FALL_MODEL_FILENAME)
        _log(f"転倒モデルを反映しました: {dst}")
    else:
        backed_up = None
        _log("転倒モデルは現行が同等以上のため反映を見送りました(候補は candidates/ に保持)")

    _log(f"  閾値{threshold:.2f} / 現行 acc={_pct(baseline, 'accuracy')} "
         f"見逃し={_pct(baseline, 'miss_rate')} → 候補 acc={_pct(candidate, 'accuracy')} "
         f"見逃し={_pct(candidate, 'miss_rate')}")
    return {
        "sequences": int(len(X)),
        "normal": int(np.sum(y == 0)),
        "fall": int(np.sum(y == 1)),
        "skipped": len(skipped),
        "threshold": threshold,
        "val_accuracy": metrics["val_accuracy"],
        "baseline": baseline,
        "candidate": candidate,
        "promoted": bool(promoted),
        "backup": str(backed_up) if backed_up else None,
    }


# ================= 状態モデル =================
def _retrain_state():
    import numpy as np

    X, y, rows, skipped = training.build_state_dataset()
    if len(X) == 0:
        raise RuntimeError("状態学習用のCSVが見つかりません(STATE_BY_SUFFIX の登録を確認)")
    np.save(training.dataset_path("state_X.npy"), X)
    np.save(training.dataset_path("state_y.npy"), y)
    np.save(training.dataset_path("state_labels.npy"), np.array(training.STATE_LABELS))

    labels = np.array(training.STATE_LABELS)
    model, metrics, (X_val, y_val, pred) = training.train_state(
        X, y, len(labels),
        epochs=int(getattr(settings, "AI_RETRAIN_EPOCHS_STATE", 50)),
        verbose=0,
    )
    candidate_path = training.candidate_dir() / training.STATE_MODEL_FILENAME
    model.save(candidate_path)
    np.save(training.candidate_dir() / training.STATE_LABELS_FILENAME, labels)
    candidate_acc = float(np.mean(pred == y_val))

    baseline_acc = None
    current = training.model_dir() / training.STATE_MODEL_FILENAME
    if current.exists():
        try:
            base_model = training.load_model(current)
            base_pred = np.argmax(base_model.predict(X_val, verbose=0), axis=1)
            baseline_acc = float(np.mean(base_pred == y_val))
        except Exception as exc:  # noqa: BLE001
            _log(f"現行状態モデルの評価に失敗(比較不能): {exc}")

    promoted = baseline_acc is None or candidate_acc >= baseline_acc - _margin()
    backed_up = None
    if promoted:
        dst, backed_up = training.promote(training.STATE_MODEL_FILENAME)
        training.promote(training.STATE_LABELS_FILENAME)
        _log(f"状態モデルを反映しました: {dst}")
    else:
        _log("状態モデルは現行が同等以上のため反映を見送りました(候補は candidates/ に保持)")

    _log(f"  状態 acc: 現行 {'-' if baseline_acc is None else format(baseline_acc * 100, '.2f') + '%'}"
         f" → 候補 {candidate_acc * 100:.2f}%")
    return {
        "sequences": int(len(X)),
        "classes": len(labels),
        "skipped": len(skipped),
        "val_accuracy": metrics["val_accuracy"],
        "baseline_accuracy": None if baseline_acc is None else round(baseline_acc, 4),
        "candidate_accuracy": round(candidate_acc, 4),
        "promoted": bool(promoted),
        "backup": str(backed_up) if backed_up else None,
    }


# ================= 判定・表示の補助 =================
def _margin():
    """許容する劣化幅(これ以内的低下なら反映する)"""
    return float(getattr(settings, "AI_RETRAIN_PROMOTE_MARGIN", 0.005))


def _should_promote(baseline, candidate):
    """現行より悪化していなければ反映する(正解率と見逃し率の両方で見る)"""
    if baseline is None or candidate is None:
        return True
    margin = _margin()
    acc_ok = (candidate["accuracy"] or 0) >= (baseline["accuracy"] or 0) - margin
    miss_ok = (candidate["miss_rate"] or 1) <= (baseline["miss_rate"] or 1) + margin
    fp_ok = (candidate["false_positive_rate"] or 1) <= (baseline["false_positive_rate"] or 1) + margin
    return bool(acc_ok and miss_ok and fp_ok)


def _pct(data, key):
    if not data or data.get(key) is None:
        return "-"
    return f"{data[key] * 100:.2f}%"


def last_run_summary():
    """画面表示用の最新実行情報(retrain_state.json を読む)"""
    state = training.read_state()
    if not state:
        return None
    return {
        "finished_at": state.get("finished_at"),
        "data_files": state.get("data_files"),
        "elapsed_seconds": state.get("elapsed_seconds"),
        "error": state.get("error"),
        "fall_promoted": (state.get("fall") or {}).get("promoted"),
        "state_promoted": (state.get("state") or {}).get("promoted"),
    }
