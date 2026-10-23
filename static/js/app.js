/* =====================================================
   CARE SEC 転倒・異常検知システム 共通JS
   - 情報バーの時計
   - ハンバーガーメニュー(モバイル用)
   - Web Notifications API(OS通知)
   - 異常検知のポーリング(リアルタイム通知 / 対応者の共有表示)
   - 録画再生の検知マークジャンプ
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

  /* ---------- Web Notifications API(OS通知) ---------- */
  var notificationEnabled = localStorage.getItem('caresec_notifications') !== 'disabled';

  function requestNotificationPermission() {
    if (!('Notification' in window)) {
      console.log('このブラウザは通知に対応していません');
      return;
    }

    if (Notification.permission === 'granted') {
      return;
    }

    if (Notification.permission !== 'denied') {
      Notification.requestPermission().then(function (permission) {
        if (permission === 'granted') {
          console.log('通知が許可されました');
        }
      });
    }
  }

  function sendOSNotification(title, options) {
    if (!notificationEnabled) {
      return;
    }

    if (!('Notification' in window)) {
      return;
    }

    if (Notification.permission === 'granted') {
      // タブがバックグラウンドの時のみOS通知
      if (document.hidden) {
        var notification = new Notification(title, options);
        notification.onclick = function () {
          window.focus();
          if (options.data && options.data.url) {
            window.location.href = options.data.url;
          }
        };
      }
    }
  }

  // 初回訪問時に通知許可を要求
  if (notificationEnabled) {
    requestNotificationPermission();
  }

  // 通知設定変更イベント(設定ページから)
  window.addEventListener('storage', function (e) {
    if (e.key === 'caresec_notifications') {
      notificationEnabled = e.newValue !== 'disabled';
    }
  });

  /* ---------- 情報バーの時計 ---------- */
  var clock = document.getElementById('clock');
  if (clock) {
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var tick = function () {
      var d = new Date();
      clock.textContent =
        d.getFullYear() + '/' + pad(d.getMonth() + 1) + '/' + pad(d.getDate()) +
        ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ---------- トースト通知 ---------- */
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

  /* ---------- 通知音(WebAudioによる簡易ビープ) ---------- */
  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) { return; }
      var ctx = new Ctx();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.08, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.7);
      osc.start();
      osc.stop(ctx.currentTime + 0.7);
    } catch (e) { /* 音声不可環境は無視 */ }
  }

  /* ---------- 異常検知ポーリング ----------
   * body[data-alerts-api] に指定したURLへ5秒間隔でGETし、
   * 以下のJSON形式を返すことを想定:
   * {
   *   "open_count": 2,
   *   "alerts": [
   *     {"id": 1, "kind_label": "転倒", "status": "open",
   *      "status_label": "未対応", "responder_name": null,
   *      "location": "1F 廊下"}
   *   ]
   * }
   * → 新規異常があればトースト+通知音、対応者名を全画面で更新
   */
  var apiUrl = document.body.dataset.alertsApi;
  var badge = document.getElementById('alert-badge-count');
  var sidebarBadge = document.getElementById('sidebar-alert-count');
  var knownIds = {};
  var initialized = false;

  function applyAlertData(data) {
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
      // 新規の未対応異常 → 通知
      if (initialized && a.status === 'open' && !knownIds[a.id]) {
        var message = '🚨 異常検知: ' + a.kind_label + ' / ' + (a.location || '');
        
        // トースト通知(常に)
        toast(message);
        beep();
        
        // OS通知(タブがバックグラウンドの時のみ)
        sendOSNotification('CARE SEC - 異常検知', {
          body: a.kind_label + ' / ' + (a.location || ''),
          icon: '/static/img/thumb_fall.svg',
          tag: 'alert-' + a.id,
          data: {
            url: '/alerts/'
          }
        });
      }
      knownIds[a.id] = true;
    });
    initialized = true;
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

  /* ---------- 通知設定トグル ---------- */
  var notificationToggle = document.getElementById('notification-toggle');
  var notificationStatus = document.getElementById('notification-status');

  if (notificationToggle) {
    // 初期状態を設定
    var isEnabled = localStorage.getItem('caresec_notifications') !== 'disabled';
    notificationToggle.checked = isEnabled;
    updateNotificationStatus(isEnabled);

    // トグル変更時
    notificationToggle.addEventListener('change', function () {
      var enabled = this.checked;
      localStorage.setItem('caresec_notifications', enabled ? 'enabled' : 'disabled');
      updateNotificationStatus(enabled);

      if (enabled) {
        // 有効化時に通知許可を再要求
        if ('Notification' in window && Notification.permission !== 'granted') {
          Notification.requestPermission();
        }
      }
    });
  }

  function updateNotificationStatus(enabled) {
    if (notificationStatus) {
      notificationStatus.textContent = enabled ? '通知有効' : '通知無効';
      notificationStatus.style.color = enabled ? 'var(--color-success)' : 'var(--color-muted)';
    }
  }
})();
