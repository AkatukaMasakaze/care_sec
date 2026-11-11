/**
 * CARE SEC カメラデバッグページ(static/js/debug_camera.js)
 * - サーバーマシン接続カメラの検知一覧表示と選択
 * - MJPEGストリームの開始/停止
 */
(function () {
  "use strict";

  var root = document.getElementById("debug-camera");
  if (!root) {
    return;
  }

  var devicesUrl = root.dataset.devicesUrl;
  var progressUrl = root.dataset.progressUrl;

  var statusEl = document.getElementById("device-status");
  var listEl = document.getElementById("device-list");
  var emptyEl = document.getElementById("device-empty");
  var errorEl = document.getElementById("device-error");
  var refreshBtn = document.getElementById("device-refresh");

  var progressEl = document.getElementById("detect-progress");
  var progressBar = document.getElementById("detect-progress-bar");
  var progressLabel = document.getElementById("detect-progress-label");
  var progressPercent = document.getElementById("detect-progress-percent");
  var progressTimer = null;

  // MJPEG接続は src を消しただけでは切れないため、1px画像へ差し替えて読み込みを中断させる
  var BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

  /* ---------- 全カメラ一括ライブ表示(カード形式) ---------- */
  var gridRoot = document.getElementById("camera-grid-root");
  var gridEl = document.getElementById("camera-grid");
  var gridEmptyEl = document.getElementById("grid-empty");
  var gridStatusEl = document.getElementById("grid-status");
  var gridToggleBtn = document.getElementById("grid-toggle");
  var gridErrorEl = document.getElementById("grid-error");
  var gridUrl = gridRoot ? gridRoot.dataset.streamUrl : "";
  var cards = {};          // デバイスキー -> {root, img, fields, device}
  var gridOn = true;       // 「すべて停止」で false になる

  var devices = [];

  function formatDevice(device) {
    if (device.busy) {
      return "使用中のため映像を確認できません(選択するとAIがこのカメラへ切り替わります)";
    }
    if (device.available === false) {
      return "開けるが映像なし(他アプリ使用中の可能性)";
    }
    var size = device.width && device.height ? device.width + "×" + device.height : "解像度不明";
    var fps = device.fps ? " / " + device.fps + "fps" : "";
    var backend = device.backend ? " / " + device.backend : "";
    var inUse = device.in_use ? " / 配信中" : "";
    var url = device.url ? " / " + device.url : "";
    return size + fps + backend + inUse + url;
  }

  function renderDevices() {
    listEl.innerHTML = "";
    devices.forEach(function (device) {
      var li = document.createElement("li");
      // 一覧は情報表示のみ(映像は下のカードで全カメラ同時に出す)
      var row = document.createElement("div");
      var unavailable = device.available === false && !device.busy;
      row.className = "debug-device" + (unavailable ? " is-unavailable" : "") + (device.busy ? " is-busy" : "");

      // 検知時に見た実フレームのサムネイル(どれがどのカメラか目視確認できる)
      if (device.snapshot_url) {
        var thumb = document.createElement("img");
        thumb.className = "debug-device__thumb";
        thumb.src = device.snapshot_url;
        thumb.alt = "index " + device.index + " の映像";
        thumb.loading = "lazy";
        // CSS依存だとブラウザのスタイルシートキャッシュで原寸表示になりレイアウトが崩れるため、
        // インラインで確定させる
        thumb.width = 88;
        thumb.height = 66;
        thumb.style.cssText = "width:88px;height:66px;object-fit:cover;flex:0 0 auto;" +
          "border:1px solid #cfd5db;border-radius:4px;background:#1f2328;";
        row.appendChild(thumb);
      }

      var indexEl = document.createElement("span");
      indexEl.className = "debug-device__index";
      indexEl.textContent = device.kind === "ip" ? "IP" : "index " + device.index;

      var nameEl = document.createElement("span");
      nameEl.className = "debug-device__name";
      nameEl.textContent = device.name;

      var metaEl = document.createElement("span");
      metaEl.className = "debug-device__meta";
      metaEl.textContent = formatDevice(device);

      row.appendChild(indexEl);
      row.appendChild(nameEl);
      row.appendChild(metaEl);
      li.appendChild(row);
      listEl.appendChild(li);
    });
    listEl.hidden = devices.length === 0;
    // 検知中は「0件」でも空欄メッセージを出さない
    emptyEl.hidden = devices.length !== 0 || detectPending;
  }

  /* ---------- カード(1カメラ分) ---------- */
  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function hms(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  }

  function streamable(device) {
    // 映像を取れない(他アプリ占有)デバイス自動で開くとハングするので出さない
    return device.available !== false && !device.busy;
  }

  function createCard(device) {
    var rootEl = document.createElement("div");
    rootEl.className = "camera-card";

    var head = document.createElement("div");
    head.className = "camera-card__head";

    var title = document.createElement("div");
    var nameEl = document.createElement("span");
    nameEl.className = "camera-card__name";
    nameEl.textContent = device.name || "カメラ";
    var keyEl = document.createElement("span");
    keyEl.className = "camera-card__key";
    keyEl.textContent = " " + (device.kind === "ip" ? "IP " + device.index : "index " + device.index);
    title.appendChild(nameEl);
    title.appendChild(keyEl);

    var shotEl = document.createElement("span");
    shotEl.className = "camera-card__shot";
    shotEl.textContent = "--:--:--";
    shotEl.title = "このフレームが撮像された時刻(秒まで)";

    head.appendChild(title);
    head.appendChild(shotEl);
    rootEl.appendChild(head);

    var fields = {};
    if (streamable(device)) {
      // 映像は「そのカメラの解析が走り始めてから」接続する。
      // 待機中のカメラへ先に接続すると、初フレームまでHTTPがハングし(実測60秒待ち)、
      // ブラウザの同一サーバー6接続上限を食って一覧のポーリングまで止まる。
      var wait = document.createElement("p");
      wait.className = "camera-card__placeholder";
      wait.textContent = "映像を待っています…";
      rootEl.appendChild(wait);

      var img = document.createElement("img");
      img.className = "camera-card__stream";
      img.alt = (device.name || "カメラ") + " のライブ映像";
      img.hidden = true;
      rootEl.appendChild(img);

      var meta = document.createElement("div");
      meta.className = "camera-card__meta";
      ["判定", "状態", "解析fps", "遅延", "無動作", "視聴"].forEach(function (label) {
        var box = document.createElement("span");
        box.appendChild(document.createTextNode(label + " "));
        var key = document.createElement("b");
        key.textContent = "-";
        fields[label] = key;
        box.appendChild(key);
        meta.appendChild(box);
      });
      rootEl.appendChild(meta);

      img.addEventListener("error", function () {
        if (!cards[device.index]) {
          return;
        }
        fields["判定"].textContent = "受信エラー";
        fields["判定"].className = "is-danger";
      });

      var card = { root: rootEl, img: img, wait: wait, fields: fields, shot: shotEl, device: device };
      cards[device.index] = card;
      // 接続は updateCards(解析が running になった時)で決める。ここでは開かない。
    } else {
      var ph = document.createElement("p");
      ph.className = "camera-card__placeholder";
      ph.textContent = device.busy
        ? "他のカメラが使用中で映像を確認できません"
        : "映像を取得できません(他アプリを閉じて再検知してください)";
      rootEl.appendChild(ph);
      cards[device.index] = { root: rootEl, img: null, wait: ph, fields: fields, shot: shotEl, device: device };
    }
    return cards[device.index];
  }

  function attachStream(card) {
    if (!card.img || card.attached) {
      return;
    }
    card.attached = true;
    card.img.src = gridUrl + "?device=" + encodeURIComponent(String(card.device.index)) + "&t=" + Date.now();
    card.img.hidden = false;
    if (card.wait) {
      card.wait.hidden = true;
    }
  }

  function detachStream(card, reason) {
    if (!card.attached) {
      return;
    }
    card.attached = false;
    // MJPEG接続は src を消すだけでは切れないため、1px画像へ差し替えて読み込みを中断させる
    card.img.src = BLANK;
    card.img.hidden = true;
    if (card.wait) {
      card.wait.hidden = false;
      card.wait.textContent = reason || "映像を待っています…";
    }
  }

  function destroyCard(key) {
    var card = cards[key];
    if (!card) {
      return;
    }
    detachStream(card);
    if (card.root.parentNode) {
      card.root.parentNode.removeChild(card.root);
    }
    delete cards[key];
  }

  function renderGrid(list) {
    if (!gridEl) {
      return;
    }
    var seen = {};
    (list || []).forEach(function (device) {
      var key = String(device.index);
      seen[key] = true;
      if (!cards[key]) {
        var card = createCard(device);
        gridEl.appendChild(card.root);
      } else {
        cards[key].device = device;
      }
    });
    Object.keys(cards).forEach(function (key) {
      if (!seen[key]) {
        destroyCard(key);
      }
    });
    var live = Object.keys(cards).filter(function (key) {
      return cards[key].attached;
    }).length;
    if (gridEmptyEl) {
      gridEmptyEl.hidden = Object.keys(cards).length !== 0;
    }
    setGridStatus(gridOn ? "配信中 " + live + "台" : "停止中", gridOn ? "resolved" : "");
  }

  function setGridStatus(text, tone) {
    if (!gridStatusEl) {
      return;
    }
    gridStatusEl.textContent = text;
    gridStatusEl.className = "chip" + (tone ? " chip--" + tone : "");
  }

  /* ---------- AI状態(1秒ポーリング)を各カードへ反映 ---------- */
  var aiSeen = false;      // /debug/camera/ai/ の応答を一度でも受け取ったか

  function attachAll() {
    Object.keys(cards).forEach(function (key) {
      if (gridOn && cards[key].img) {
        attachStream(cards[key]);
      }
    });
    renderGrid(devices);
  }

  function updateCards(rows) {
    var now = Date.now();
    var changed = false;
    (rows || []).forEach(function (row) {
      var card = cards[String(row.device)];
      if (!card || !card.fields["判定"]) {
        return;   // 映像を出していないカメラ(占有中など)はこの対象外
      }
      var f = card.fields;
      var running = row.phase === "running";
      var danger = row.status === "FALL !!!";

      // 解析が走り始めたカメラだけ接続する(待機中に開くと初フレームまでハングする)
      if (card.img && !card.attached) {
        if (gridOn && running) {
          attachStream(card);
          changed = true;
        } else if (gridOn) {
          card.wait.textContent = row.phase === "loading" ? "AIモデル準備中…"
            : (row.phase === "error" ? "映像が止まっています(再接続中)" : "このカメラはまだ解析されていません");
        }
      }

      if (!running) {
        f["判定"].textContent = row.phase === "loading" ? "モデル準備中"
          : (row.phase === "error" ? "停止(映像なし)" : "待機中");
        f["判定"].className = row.phase === "error" ? "is-danger" : "is-warn";
      } else {
        f["判定"].textContent = row.status_label || "-";
        f["判定"].className = danger || row.status === "FALL" ? "is-danger"
          : (row.status === "NORMAL" ? "is-ok" : (row.low_fps ? "is-warn" : ""));
      }
      f["状態"].textContent = row.state || "-";
      f["解析fps"].textContent = row.fps ? row.fps + " fps" : "-";
      f["無動作"].textContent = (row.still_seconds === null || row.still_seconds === undefined)
        ? "-" : row.still_seconds + "秒";
      f["視聴"].textContent = row.viewers ? row.viewers + "人" : "-";

      var age = row.frame_age;
      if (age === null || age === undefined) {
        // USBは専有読取スレッド不要(ドライバが常に最新1枚で上書き=行列自体できない)
        f["遅延"].textContent = "USB(行列なし)";
        f["遅延"].className = "";
        card.shot.textContent = hms(new Date(now));
        card.shot.className = "camera-card__shot";
        card.shot.title = "USBカメラは最新1枚が常に上書きされるため遅延が溜まりません";
      } else {
        var tone = age >= 1.5 ? "is-danger" : (age >= 0.5 ? "is-warn" : "is-ok");
        f["遅延"].textContent = age.toFixed(2) + " 秒";
        f["遅延"].className = tone;
        card.shot.textContent = hms(new Date(now - age * 1000));
        card.shot.className = "camera-card__shot " + tone;
        card.shot.title = "撮像時刻 = 現在時刻 - 映像の遅延(" + age + "秒)";
      }
      card.root.className = "camera-card" + (danger ? " is-danger" : "");
    });
    if (changed) {
      renderGrid(devices);
    }
  }

  /* ---------- 現在時刻(秒まで) ---------- */
  var pageDate = document.getElementById("page-date");
  var pageTime = document.getElementById("page-time");
  var pageTz = document.getElementById("page-tz");

  function tickClock() {
    var now = new Date();
    if (pageDate) {
      pageDate.textContent = now.getFullYear() + "/" + pad2(now.getMonth() + 1) + "/" + pad2(now.getDate());
    }
    if (pageTime) {
      pageTime.textContent = hms(now);
    }
    if (pageTz && !pageTz.textContent) {
      var zone = "";
      try {
        zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      } catch (e) {
        zone = "UTC" + (-now.getTimezoneOffset() / 60);
      }
      pageTz.textContent = zone ? "この端末の時刻(" + zone + ")" : "";
    }
  }
  tickClock();
  window.setInterval(tickClock, 250);

  function renderDeviceList(list) {
    devices = list || [];
    renderDevices();
    renderGrid(devices);
  }

  /* ---------- デバイス検知の進行バー ---------- */
  var detectPending = false;

  function updateProgress(data) {
    if (!progressEl) {
      return;
    }
    var total = data.total || 0;
    var current = data.current || 0;
    var percent = null;

    if (data.state === "done" || data.state === "error") {
      percent = 100;
    } else if (total > 0) {
      percent = Math.min(99, Math.round((current / total) * 100));
    }

    if (percent === null) {
      // ステップ数が未確定の間は不定進捗(スライドアニメーション)
      progressBar.className = "detect-progress__bar is-indeterminate";
      progressBar.style.width = "";
      progressPercent.textContent = "";
    } else {
      progressBar.className = "detect-progress__bar";
      progressBar.style.width = percent + "%";
      progressPercent.textContent = percent + "%";
    }

    var label = data.stage || "デバイスを検知しています…";
    if (data.state === "running" && total > 0) {
      label += "(" + Math.min(current + 1, total) + "/" + total + ")";
    }
    if (data.elapsed !== null && data.elapsed !== undefined && data.state === "running") {
      label += " / " + data.elapsed + "秒";
    }
    progressLabel.textContent = label;

    // 検知中に見つかった分は先に表示する(1台目の確認が重くても2台目を選べるように)
    if (data.devices && data.devices.length && JSON.stringify(data.devices) !== JSON.stringify(devices)) {
      renderDeviceList(data.devices);
      if (statusEl && !statusEl.hidden) {
        statusEl.textContent = "検知中ですでに分かったカメラを表示しています…";
      }
    }

    // 検知完了を待つ。ここで /debug/camera/devices/ を**引き直さない**こと。
    // 引き直すとキャッシュ(既定30秒)が切れた後なので検知がもう一度走り、
    // 完了→再発走→完了→…のループになってカメラを開き直す(実測で内蔵カメラが不安定化した)。
    // 進捗APIは完了時の最終一覧を already 返すので、それを使う。
    if (detectPending && data.state === "done") {
      detectPending = false;
      if (statusEl) {
        statusEl.hidden = true;
      }
      if (data.devices && data.devices.length) {
        renderDeviceList(data.devices);
      }
    }
  }

  function startProgressPolling() {
    if (!progressEl) {
      return;
    }
    progressEl.hidden = false;
    updateProgress({ state: "running", stage: "検知を開始しています…", current: 0, total: 0 });
    if (progressTimer) {
      return;
    }
    progressTimer = window.setInterval(function () {
      fetch(progressUrl, { headers: { Accept: "application/json" } })
        .then(function (res) {
          return res.json();
        })
        .then(updateProgress)
        .catch(function () {
          /* 進捗取得の失敗は表示に影響させない */
        });
    }, 400);
  }

  function stopProgressPolling(finalData) {
    if (progressTimer) {
      window.clearInterval(progressTimer);
      progressTimer = null;
    }
    if (finalData) {
      updateProgress(finalData);
    }
    // 完了表示を見せるため少し置いてからバーを隠す
    window.setTimeout(function () {
      if (progressEl) {
        progressEl.hidden = true;
      }
    }, 700);
  }

  function loadDevices(force) {
    statusEl.hidden = false;
    statusEl.textContent = force ? "デバイスを再検知しています…" : "デバイスを検知しています…";
    errorEl.hidden = true;
    startProgressPolling();
    fetch(devicesUrl + (force ? "?force=1" : ""), { headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data.pending) {
          // 検知はバックグラウンド進行中。進捗ポーリングが続き、見つかった分が表示される
          detectPending = true;
          renderDeviceList(data.devices);
          return;
        }
        detectPending = false;
        statusEl.hidden = true;
        stopProgressPolling({ state: "done", stage: "完了", current: 1, total: 1 });
        if (!data.ok) {
          devices = [];
          renderDevices();
          emptyEl.hidden = true;
          errorEl.textContent = data.error || "デバイス検知に失敗しました。";
          errorEl.hidden = false;
          return;
        }
        renderDeviceList(data.devices);
      })
      .catch(function () {
        detectPending = false;
        statusEl.hidden = true;
        stopProgressPolling({ state: "error", stage: "通信エラー", current: 1, total: 1 });
        errorEl.textContent = "デバイス検知APIとの通信に失敗しました。";
        errorEl.hidden = false;
      });
  }

  /* ---------- 一括表示の ON/OFF(全カメラ同時に接続する) ---------- */
  function setGridOn(on) {
    gridOn = on;
    Object.keys(cards).forEach(function (key) {
      var card = cards[key];
      if (!card.img) {
        return;
      }
      if (on) {
        // 解析中のカメラだけ張り直す(待機中へ接続するとハングする)
        if (card.attached || card.phase === "running") {
          attachStream(card);
        }
      } else {
        detachStream(card, "停止中(「すべて表示」で再開します)");
      }
    });
    if (gridToggleBtn) {
      gridToggleBtn.textContent = on ? "すべて停止" : "すべて表示";
    }
    if (gridErrorEl) {
      gridErrorEl.hidden = true;
    }
    renderGrid(devices);
  }

  if (gridToggleBtn) {
    gridToggleBtn.addEventListener("click", function () {
      setGridOn(!gridOn);
    });
  }

  refreshBtn.addEventListener("click", function () {
    loadDevices(true);
  });
  window.addEventListener("beforeunload", function () {
    // ページを離れるときは全 MJPEG接続を切る(サーバ側の視聴者数/描画負荷が下がる)
    setGridOn(false);
  });

  loadDevices(false);

  /* ---------- リアルタイムAI解析の状態表示(1秒ポーリング) ---------- */
  var aiRoot = document.getElementById("ai-status");
  if (!aiRoot) {
    return;
  }

  var aiStatusUrl = aiRoot.dataset.aiStatusUrl;
  var aiChip = document.getElementById("ai-chip");
  var aiState = document.getElementById("ai-state");
  var aiJudge = document.getElementById("ai-judge");
  var aiDanger = document.getElementById("ai-danger");
  var aiFoot = document.getElementById("ai-foot");
  var aiStill = document.getElementById("ai-still");
  var aiFrames = document.getElementById("ai-frames");
  var aiFps = document.getElementById("ai-fps");
  var aiDelay = document.getElementById("ai-delay");
  var aiRecording = document.getElementById("ai-recording");
  var aiDevices = document.getElementById("ai-devices");
  var aiDevicesNote = document.getElementById("ai-devices-note");
  var aiRetrainNote = document.getElementById("ai-retrain-note");
  var aiError = document.getElementById("ai-error");
  var aiLastAlert = document.getElementById("ai-last-alert");

  function setAiChip(text, tone) {
    aiChip.textContent = text;
    aiChip.className = "chip" + (tone ? " chip--" + tone : "");
  }

  function setValue(el, text, tone) {
    el.textContent = text;
    el.className = "ai-status-item__value" + (tone ? " is-" + tone : "");
  }

  /* ネットワークカメラは「撮像から何秒古いフレームを解析しているか」を出す。
     USBは専有読取スレッドを持たないので行列自体できない(=表示なし)。 */
  function delayLabel(age) {
    if (age === null || age === undefined) {
      return "USB(行列なし)";
    }
    return age + " 秒";
  }

  function delayTone(age) {
    if (age === null || age === undefined) {
      return "";
    }
    if (age >= 1.5) {
      return "danger";
    }
    return age >= 0.5 ? "warn" : "ok";
  }

  function renderAiDevices(devices) {
    if (!aiDevices) {
      return;
    }
    var body = aiDevices.querySelector("tbody");
    if (!devices || !devices.length) {
      aiDevices.hidden = true;
      return;
    }
    aiDevices.hidden = false;
    body.innerHTML = "";
    var errors = [];
    devices.forEach(function (row) {
      var tr = document.createElement("tr");
      function cell(text, cls) {
        var td = document.createElement("td");
        td.textContent = text;
        if (cls) {
          td.className = cls;
        }
        tr.appendChild(td);
      }
      var phase = row.phase || "stopped";
      var phaseLabel =
        phase === "running" ? "解析中"
        : (phase === "loading" ? "準備中"
        : (phase === "error" ? "停止" : "待機中"));
      cell(typeof row.device === "number" ? "index " + row.device : "IP " + row.device);
      cell(row.state || "-");
      cell(row.status_label || "-");
      if (row.low_fps) {
        cell((row.fps || 0) + " fps(低速/帯域不足)", "is-warn");
      } else {
        cell(row.fps ? row.fps + " fps" : "-");
      }
      cell(delayLabel(row.frame_age), delayTone(row.frame_age));
      cell(row.still_seconds !== undefined && row.still_seconds !== null ? row.still_seconds + "秒" : "-");
      cell(row.viewers ? row.viewers + "人" : "-");
      cell(phaseLabel, phase === "running" ? "is-ok" : (phase === "error" ? "is-danger" : ""));
      if (row.error) {
        tr.title = row.error;
        errors.push("index " + row.device + ": " + row.error);
      }
      body.appendChild(tr);
    });

    if (aiDevicesNote) {
      aiDevicesNote.hidden = errors.length === 0;
      aiDevicesNote.textContent = errors.join(" / ");
    }
  }

  function renderAiStatus(data) {
    aiSeen = true;
    if (data.enabled === false) {
      // AI監視がオフ = 解析スレッドが動かないので「running」を待っても映像は来ない。
      // その場合は生カメラ映像として全カードを接続する。
      aiRoot.hidden = true;
      attachAll();
      return;
    }
    aiRoot.hidden = false;

    var runningCount = (data.devices || []).filter(function (d) {
      return d.running;
    }).length;

    if (data.phase === "running") {
      var label = runningCount > 1
        ? runningCount + "台を常時解析中"
        : (data.persistent ? "常時解析中 " : "解析中 ") + (data.fps ? data.fps + "fps" : "");
      setAiChip(label, "resolved");
    } else if (data.phase === "loading") {
      setAiChip(data.model_ready ? "準備中" : "モデル読込中", "responding");
    } else if (data.phase === "error") {
      setAiChip("エラー", "open");
    } else if (runningCount > 0) {
      // このカメラは未解析でも、サーバーは他のカメラを常時解析している
      setAiChip("このカメラは待機中(他 " + runningCount + "台は解析中)", "");
    } else {
      setAiChip(data.persistent ? "常時待機中" : "待機中", "");
    }

    aiError.hidden = !data.error;
    if (data.error) {
      aiError.textContent = "AI解析エラー: " + data.error;
    }

    setValue(aiState, data.state || "-");

    var judge = data.status_label || "-";
    var judgeTone = "";
    if (data.status === "FALL !!!" || data.status === "FALL") {
      judgeTone = "danger";
    } else if (data.status === "NORMAL") {
      judgeTone = "ok";
    } else if (data.status && data.status.indexOf("WAITING") === 0) {
      judgeTone = "warn";
    }
    setValue(aiJudge, judge, judgeTone);

    if (data.danger_percent === null || data.danger_percent === undefined) {
      setValue(aiDanger, "--");
    } else {
      var dangerTone = data.danger_percent >= 75 ? "danger" : data.danger_percent >= 50 ? "warn" : "";
      setValue(aiDanger, data.danger_percent.toFixed(2) + "%", dangerTone);
    }

    setValue(aiFoot, data.foot_visible ? "画面内" : "画面外", data.foot_visible ? "ok" : "warn");

    if (data.stillness_threshold) {
      var stillTone = data.still_seconds >= data.stillness_threshold ? "danger" : "";
      setValue(aiStill, data.still_seconds + " / " + data.stillness_threshold + "秒", stillTone);
    } else {
      setValue(aiStill, data.still_seconds + "秒");
    }

    setValue(aiFrames, (data.sequence_frames || 0) + " / 30");
    setValue(aiFps, data.fps ? data.fps + " fps" : "-");
    if (aiDelay) {
      setValue(aiDelay, delayLabel(data.frame_age), delayTone(data.frame_age));
    }

    if (aiRecording) {
      if (data.recording_seconds_left !== null && data.recording_seconds_left !== undefined) {
        setValue(aiRecording, "録画中 " + data.recording_seconds_left + "秒", "danger");
      } else {
        setValue(aiRecording, "待機中");
      }
    }

    if (data.last_alert) {
      var when = data.last_alert.at ? new Date(data.last_alert.at) : null;
      var timeLabel = when && !isNaN(when.getTime()) ? when.toLocaleTimeString() : "";
      aiLastAlert.hidden = false;
      aiLastAlert.textContent =
        "直近のAI検知: " + data.last_alert.kind +
        " / 信頼度 " + Math.round((data.last_alert.confidence || 0) * 100) + "%" +
        (timeLabel ? " (" + timeLabel + ")" : "") +
        " → 異常検知一覧で対応できます";
    } else {
      aiLastAlert.hidden = true;
    }

    renderAiDevices(data.devices);
    updateCards(data.devices);
    renderRetrain(data.retrain);
  }

  /* サーバー起動時の自動再学習の最新結果 */
  function renderRetrain(info) {
    if (!aiRetrainNote) {
      return;
    }
    if (!info) {
      aiRetrainNote.hidden = true;
      return;
    }
    var parts = [];
    if (info.finished_at) {
      parts.push("自動再学習 最終: " + info.finished_at);
    }
    if (info.data_files !== undefined && info.data_files !== null) {
      parts.push("材料 " + info.data_files + "件");
    }
    if (info.fall_promoted === true) {
      parts.push("転倒モデル更新");
    } else if (info.fall_promoted === false) {
      parts.push("転倒モデルは現行維持");
    }
    if (info.state_promoted === true) {
      parts.push("状態モデル更新");
    } else if (info.state_promoted === false) {
      parts.push("状態モデルは現行維持");
    }
    if (info.elapsed_seconds !== undefined && info.elapsed_seconds !== null) {
      parts.push(info.elapsed_seconds + "秒");
    }
    if (info.error) {
      parts.push("エラー: " + info.error);
    }
    aiRetrainNote.textContent = parts.join(" / ");
    aiRetrainNote.hidden = parts.length === 0;
  }

  function loadAiStatus() {
    // device 無し = 全カメラの要約(+一番actively解析しているカメラの詳細)。
    // カード側は要約の devices を使うので、1台ずつ選ばなくても全台の状態がそろう。
    fetch(aiStatusUrl, { headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.json();
      })
      .then(renderAiStatus)
      .catch(function () {
        setAiChip("取得失敗", "open");
      });
  }

  loadAiStatus();
  window.setInterval(loadAiStatus, 1000);

  // AI状態が一度も返らない(取得失敗)ときは、生映像でも良いので表示する
  window.setTimeout(function () {
    if (!aiSeen) {
      attachAll();
    }
  }, 6000);
})();
