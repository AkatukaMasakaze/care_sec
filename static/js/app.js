/* =====================================================
   CARE SEC 転倒・異常検知システム 共通JS
   - 情報バーの時計
   - ハンバーガーメニュー(モバイル用)
   - 異常検知のリアルタイム通知(2026-11-17 再設計)
       画面中央の**大判バナー** + 警報音 + OS通知(Web Notifications)
   - Web Notifications API(OS通知)
   - 録画再生の検知マークジャンプ

   旧実装の問題(デモ名残):
     - OS通知のアイコンがデモ用 thumb_fall.svg / 詳細ではなく一覧へ飛ぶ
     - notificationEnabled を起動時にだけ読むため、設定ページで切り替えても
       同一タブの実態に反映されなかった
     - 画面内通知が340pxの小さなトーストで、見落とす(実況: 「動いていない」)
   ===================================================== */
(function () {
  'use strict';

  /* ---------- ハンバーガーメニュー ---------- */
  var hamburgerBtn = document.getElementById('hamburger-btn');
  var sidebar = document.getElementById('sidebar');
  var sidebarOverlay = document.getElementById('sidebar-overlay');

  function openSidebar() {
    if (hamburgerBtn) hamburgerBtn.classList.add('is-active');
    if (sidebar) sidebar.classList.add('is-open');
    if (sidebarOverlay) sidebarOverlay.classList.add('is-visible');
    if (hamburgerBtn) hamburgerBtn.setAttribute('aria-expanded', 'true');
  }

  function closeSidebar() {
    if (hamburgerBtn) hamburgerBtn.classList.remove('is-active');
    if (sidebar) sidebar.classList.remove('is-open');
    if (sidebarOverlay) sidebarOverlay.classList.remove('is-visible');
    if (hamburgerBtn) hamburgerBtn.setAttribute('aria-expanded', 'false');
  }

  if (hamburgerBtn) {
    hamburgerBtn.addEventListener('click', function () {
      var isOpen = hamburgerBtn.classList.contains('is-active');
      if (isOpen) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });
  }

  if (sidebarOverlay) {
    sidebarOverlay.addEventListener('click', closeSidebar);
  }

  // メニューリンクをクリックしたらサイドバーを閉じる(モバイル)
  if (sidebar) {
    var menuLinks = sidebar.querySelectorAll('.menu__item a');
    menuLinks.forEach(function (link) {
      link.addEventListener('click', function () {
        // ウィンドウ幅が900px以下の場合のみ閉じる
        if (window.innerWidth <= 900) {
          closeSidebar();
        }
      });
    });
  }

  // ウィンドウリサイズ時にサイドバーを閉じる(PCに戻った時)
  window.addEventListener('resize', function () {
    if (window.innerWidth > 900) {
      closeSidebar();
    }
  });

  /* ---------- 通知設定(送時点で localStorage を読む) ----------
   * 起動時に一度だけ読むと、設定ページのトグルを同一タブで切り替えても
   * 旧値に縛られた(実測バグ)。每次都チェックへ変えた。 */
  function notificationsOn() {
    return localStorage.getItem('care_alerts_enabled') !== 'false';
  }

  /* ---------- 通知アイコン ---------- */
  var NOTIFY_ICON = '/static/img/alert_icon.svg';

  function pad2(n) { return String(n).padStart(2, '0'); }

  function formatClock(ms) {
    var d = new Date(ms);
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function agoLabel(ms) {
    var sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (sec < 60) { return sec + '秒前'; }
    if (sec < 3600) { return Math.round(sec / 60) + '分前'; }
    return Math.round(sec / 3600) + '時間前';
  }

  /* ---------- 情報バーの時計 ----------
   * 2026-11-19 復旧: 通知再設計(第41章)で app.js を書き直したときこのブロックの
   * 移植が漏れ、トップバーが "----/--/-- --:--:--" のまま死んでいた。
   */
  var clock = document.getElementById('clock');
  if (clock) {
    var clockTick = function () {
      var d = new Date();
      clock.textContent = d.getFullYear() + '/' + pad2(d.getMonth() + 1) + '/' +
        pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' +
        pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
    };
    clockTick();
    setInterval(clockTick, 1000);
  }

  /* ---------- Web Notifications API(OS通知) ---------- */
  function requestNotificationPermission() {
    if (!('Notification' in window)) {
      console.log('このブラウザは通知に対応していません');
      return;
    }
    if (Notification.permission !== 'default') {
      return;
    }
    Notification.requestPermission().then(function (permission) {
      if (permission === 'granted') {
        console.log('通知が許可されました');
      }
    });
  }

  /* OS通知: 大判化の設計
   *  - タイトルに異常の種類、本文に場所/時刻/対応者
   *  - timestamp / renotify / vibrate 付き
   *  - 転倒は requireInteraction(ユーザーが操作するまで消えない)
   *  - クリックでその異常の**詳細**へ(一覧ではなく)
   *  - 画面が見えているときは大判バナーの方を出す(重複防止) */
  function sendOSNotification(a) {
    if (!notificationsOn()) {
      return;
    }
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }
    if (!document.hidden) {
      return;
    }

    var occurred = a.occurred_ms || Date.now();
    var isFall = a.kind === 'fall';
    var detailUrl = '/alerts/' + a.id + '/';
    var notification = new Notification('🚨 ' + a.kind_label + ' を検知しました', {
      body: (a.location || '場所不明')
        + (a.camera_name ? ' / ' + a.camera_name : '')
        + ' / ' + formatClock(occurred) + '(' + agoLabel(occurred) + ')'
        + '\n' + (a.responder_name ? '対応中: ' + a.responder_name : '対応者: まだいません'),
      icon: NOTIFY_ICON,
      badge: NOTIFY_ICON,
      tag: 'care-alert-' + a.id,
      timestamp: occurred,
      renotify: true,
      requireInteraction: isFall,
      vibrate: [300, 150, 300, 150, 600],
      data: { url: detailUrl },
    });
    notification.onclick = function () {
      window.focus();
      window.location.assign(detailUrl);
    };
  }

  /* ---------- 大判異常バナー(画面中央上部) ---------- */
  var bannerRoot = document.createElement('div');
  bannerRoot.id = 'alert-banner-root';
  bannerRoot.setAttribute('role', 'alert');
  bannerRoot.setAttribute('aria-live', 'assertive');
  document.body.appendChild(bannerRoot);

  var alarmRepeat = {};   // alertId -> [timeout id, ...]

  function stopAlarmRepeat(id) {
    (alarmRepeat[id] || []).forEach(window.clearTimeout);
    delete alarmRepeat[id];
  }

  function closeBanner(id) {
    var el = bannerRoot.querySelector('[data-alert-id="' + id + '"]');
    if (el) {
      el.classList.add('is-leaving');
      setTimeout(function () { el.remove(); }, 280);
    }
    stopAlarmRepeat(id);
  }

  function showAlertBanner(a) {
    var occurred = a.occurred_ms || Date.now();
    var isFall = a.kind === 'fall';

    var card = document.createElement('div');
    card.className = 'alert-banner' + (isFall ? ' alert-banner--fall' : '');
    card.dataset.alertId = String(a.id);

    var icon = document.createElement('span');
    icon.className = 'alert-banner__icon';
    icon.textContent = '🚨';
    icon.setAttribute('aria-hidden', 'true');

    var body = document.createElement('div');
    body.className = 'alert-banner__body';

    var title = document.createElement('p');
    title.className = 'alert-banner__title';
    title.textContent = '異常検知: ';
    var kind = document.createElement('b');
    kind.textContent = a.kind_label;
    title.appendChild(kind);

    var meta = document.createElement('p');
    meta.className = 'alert-banner__meta';
    meta.textContent = (a.location || '場所不明')
      + (a.camera_name ? ' / ' + a.camera_name : '')
      + ' / ' + formatClock(occurred) + '(' + agoLabel(occurred) + ')';

    var who = document.createElement('p');
    who.className = 'alert-banner__who';
    who.textContent = a.responder_name ? ('対応中: ' + a.responder_name) : '対応者: まだいません';

    body.appendChild(title);
    body.appendChild(meta);
    body.appendChild(who);

    var actions = document.createElement('div');
    actions.className = 'alert-banner__actions';

    var link = document.createElement('a');
    link.className = 'btn btn--danger alert-banner__cta';
    link.href = '/alerts/' + a.id + '/';
    link.textContent = '今すぐ対応';
    // クリックを合図に OS通知の許可も取りに行く(ブラウザはユーザー操作が必要なため)
    link.addEventListener('click', requestNotificationPermission);

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn btn--ghost btn--sm';
    closeBtn.textContent = '閉じる';
    closeBtn.addEventListener('click', function () { closeBanner(a.id); });

    actions.appendChild(link);
    actions.appendChild(closeBtn);

    card.appendChild(icon);
    card.appendChild(body);
    card.appendChild(actions);

    // 同じ異常のバナーが既にある場合は情報だけ更新して据え置き
    var existing = bannerRoot.querySelector('[data-alert-id="' + a.id + '"]');
    if (existing) {
      who && existing.replaceChild(who, existing.querySelector('.alert-banner__who'));
      return;
    }
    bannerRoot.appendChild(card);

    // 放置時の自動消滅: 転倒は長め(30秒)、その他15秒。音の再警告も同じ周期で止める
    var ttl = isFall ? 30000 : 15000;
    var timers = [];
    // 転倒のみ: 10秒・20秒後に再アラーム音(気づかない人を起こす)
    if (isFall) {
      [10000, 20000].forEach(function (delay) {
        timers.push(setTimeout(function () {
          if (bannerRoot.querySelector('[data-alert-id="' + a.id + '"]') && notificationsOn()) {
            alarmOnce();
          }
        }, delay));
      });
    }
    alarmRepeat[a.id] = timers;
    timers.push(setTimeout(function () { closeBanner(a.id); }, ttl));

    // 画面下部の小さなトースコンテナは使わない(大判バナーが本体)
  }

  /* ---------- 警報音(WebAudio: 2音3連発。前の「1音だけ」より大きく) ---------- */
  var audioCtx = null;

  function alarmOnce() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) {
        return;
      }
      if (!audioCtx) {
        audioCtx = new Ctx();
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
      var t0 = audioCtx.currentTime;
      for (var i = 0; i < 3; i += 1) {
        var start = t0 + i * 0.42;
        [880, 1174.66].forEach(function (freq, k) {
          var osc = audioCtx.createOscillator();
          var gain = audioCtx.createGain();
          osc.type = 'square';
          osc.frequency.value = freq;
          osc.connect(gain);
          gain.connect(audioCtx.destination);
          gain.gain.setValueAtTime(0.0001, start + k * 0.2);
          gain.gain.exponentialRampToValueAtTime(0.22, start + k * 0.2 + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, start + k * 0.2 + 0.19);
          osc.start(start + k * 0.2);
          osc.stop(start + k * 0.2 + 0.2);
        });
      }
    } catch (e) { /* 音声不可環境は無視 */ }
  }

  /* ---------- トースト(小). 異常検知は大判バナーへ移行したので他用途用 ---------- */
  function toast(message) {
    var container = document.getElementById('toast-container');
    if (!container) { return; }
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = message;
    container.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('is-visible'); });
    setTimeout(function () {
      el.classList.remove('is-visible');
      setTimeout(function () { el.remove(); }, 300);
    }, 8000);
  }

  /* ---------- 異常検知ポーリング ----------
   * body[data-alerts-api] に指定したURLへ5秒間隔でGETし、
   * 以下のJSON形式を返すことを想定:
   * {
   *   "open_count": 2,
   *   "ai_enabled": true,
   *   "alerts": [
   *     {"id": 1, "kind": "fall", "kind_label": "転倒", "status": "open",
   *      "status_label": "未対応", "responder_name": null,
   *      "location": "1F 廊下", "camera_name": "廊下カメラ",
   *      "occurred_ms": 1763349723000}
   *   ]
   * }
   * → 新規異常は 大判バナー + 警報音 + OS通知(隠れている時)
   */
  var apiUrl = document.body.dataset.alertsApi;
  var badge = document.getElementById('alert-badge-count');
  var sidebarBadge = document.getElementById('sidebar-alert-count');

  /* 「見た異常」の記録はlocalStorageに永続化する。
   * メモリだけ(旧実装)だと、ページ読み込み直後の初回ポーリングで
   * 「今の未解決全部=履歴」とみなされ、直前に作られた異常(=設定タブの
   * 異常検知デモ、他タブでの実検知)が通知されず飲み込まれていた。 */
  var KNOWN_KEY = 'care_seen_alert_ids';
  var seenIds = null;
  try {
    var rawSeen = JSON.parse(localStorage.getItem(KNOWN_KEY));
    if (Array.isArray(rawSeen)) {
      seenIds = rawSeen;
    }
  } catch (e) {
    seenIds = null;
  }

  function inSeen(id) {
    return seenIds !== null && seenIds.indexOf(id) >= 0;
  }

  function markSeen(id) {
    if (seenIds === null) {
      seenIds = [];
    }
    if (seenIds.indexOf(id) < 0) {
      seenIds.push(id);
      if (seenIds.length > 300) {
        seenIds = seenIds.slice(-300);   // 無限成長防止(直近300件)
      }
    }
    localStorage.setItem(KNOWN_KEY, JSON.stringify(seenIds));
  }

  function applyAlertData(data) {
    // このブラウザで初回(seen履歴がまだ無い)は、今の未解決を丸ごと履歴扱いにする
    // (=通知洪水防止)。判定はループ前に確定させ、ループ中に条件が変わらないようにする
    var bootstrap = seenIds === null;
    // AI監視の稼働表示を、サーバーから届いた最新の状態に合わせる
    var aiDot = document.getElementById('ai-monitor-dot');
    var aiLabel = document.getElementById('ai-monitor-label');
    if (aiDot && aiLabel && typeof data.ai_enabled === 'boolean') {
      // 緑の点(on) と 灰色の点(off) を切り替える
      aiDot.className = 'status-dot ' + (data.ai_enabled ? 'status-dot--on' : 'status-dot--off');
      aiLabel.textContent = data.ai_enabled ? '稼働中' : '停止中';
    }
    var openCount = data.open_count || 0;
    if (badge) { badge.textContent = openCount; }
    if (sidebarBadge) {
      sidebarBadge.textContent = openCount;
      sidebarBadge.hidden = openCount === 0;
    }
    (data.alerts || []).forEach(function (a) {
      // ステータスチップの更新
      var statusEl = document.querySelector('[data-alert-status="' + a.id + '"]');
      if (statusEl) {
        statusEl.textContent = a.status_label;
        statusEl.className = 'chip chip--' + a.status;
      }
      // 対応者名の更新(誰が対応するか全員に共有)
      var responderEl = document.querySelector('[data-alert-responder="' + a.id + '"]');
      if (responderEl) {
        responderEl.innerHTML = a.responder_name
          ? '対応中: <strong>' + a.responder_name + '</strong>'
          : '対応者: 未割り当て';
      }
      // 対応者が付いたらバナーの「対応中」行も更新
      var bannerWho = bannerRoot.querySelector('[data-alert-id="' + a.id + '"] .alert-banner__who');
      if (bannerWho && a.responder_name) {
        bannerWho.textContent = '対応中: ' + a.responder_name;
      }
      // 解決したらバナーは用済み(他の人が対応を完了した、を音なしで伝える)
      if (a.status === 'resolved' || a.status === 'false_positive') {
        closeBanner(a.id);
      }
      // 新規の未対応異常 → 大判バナー + 警報音 + OS通知
      // 「このブラウザがまだ一度も見ていないID」が条件(=ページを移動・再読込しても効く)。
      // なお**このブラウザで初めて**apiを引いた時(seenIds=null)は、
      // 今ある未解決をまとめて履歴として扱い、通知を洪水させない
      if (a.status === 'open' && !inSeen(a.id)) {
        if (!bootstrap) {
          if (notificationsOn()) {
            showAlertBanner(a);
            alarmOnce();
          }
          sendOSNotification(a);
        }
        markSeen(a.id);
      } else if (!inSeen(a.id)) {
        markSeen(a.id);
      }
    });
  }

  if (apiUrl) {
    var poll = function () {
      fetch(apiUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) { if (data) { applyAlertData(data); } })
        .catch(function () { /* オフライン等は無視 */ });
    };
    poll();
    setInterval(poll, 5000);
  }

  // 初回訪問時に通知許可を要求（一度だけ、許可/拒否済みはスキップ）
  if (notificationsOn() && 'Notification' in window && Notification.permission === 'default') {
    requestNotificationPermission();
  }

  /* ---------- 録画再生: AI検知マークへジャンプ ---------- */
  document.querySelectorAll('[data-seek]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var video = document.getElementById('recording-video');
      if (video) {
        video.currentTime = parseFloat(btn.dataset.seek);
        video.play();
      }
    });
  });

  /* ---------- 通知設定トグル(settings.html) ---------- */
  var notificationToggle = document.getElementById('notification-toggle');
  var notificationStatus = document.getElementById('notification-status');

  if (notificationToggle) {
    var isEnabled = notificationsOn();
    notificationToggle.checked = isEnabled;
    updateNotificationStatus(isEnabled);

    notificationToggle.addEventListener('change', function () {
      var enabled = this.checked;
      localStorage.setItem('care_alerts_enabled', enabled ? 'true' : 'false');
      updateNotificationStatus(enabled);

      if (enabled) {
        // 有効化時に通知許可を再要求
        requestNotificationPermission();
      } else {
        // 無効化したら、鳴りかけの再アラームと表示中の大判バナーも止める
        Object.keys(alarmRepeat).forEach(stopAlarmRepeat);
        Array.prototype.slice.call(bannerRoot.children).forEach(function (el) {
          el.classList.add('is-leaving');
          setTimeout(function () { el.remove(); }, 280);
        });
      }
    });
  }

  function updateNotificationStatus(enabled) {
    if (notificationStatus) {
      notificationStatus.textContent = enabled ? '通知有効' : '通知停止中';
      notificationStatus.style.color = enabled ? 'var(--color-success)' : 'var(--color-muted)';
    }
  }

  // 他タブでのトグル変更にも追従(止めたのに鳴り続けるのを防ぐ)
  window.addEventListener('storage', function (e) {
    if (e.key === 'care_alerts_enabled' && e.newValue === 'false') {
      Object.keys(alarmRepeat).forEach(stopAlarmRepeat);
    }
  });

  // 露出: デバッグ/検証用に大判バナーを直接出せる(dev console から叩ける)
  window.careSecDemoAlert = function (a) {
    showAlertBanner(a || {
      id: 9999, kind: 'fall', kind_label: '転倒', location: '1F 廊下',
      camera_name: '検証カメラ', occurred_ms: Date.now(),
    });
    alarmOnce();
  };
})();
