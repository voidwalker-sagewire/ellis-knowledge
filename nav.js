/**
 * Ellis shared navigation bar.
 * Include with: <script src="/tools/nav.js"></script>
 *
 * Injects a consistent top nav across all Ellis tools, and reads the SAME
 * localStorage keys (ellis_token, ellis_email) that pcs-scorer.html and
 * chat.html already write to. Since all tools live under the same origin
 * (ellis.sagewire.dev), logging in on ANY page that writes these keys makes
 * every other page that includes this script show the same logged-in state
 * — no separate login system needed, just a shared, visible indicator.
 *
 * This does NOT give the calculator/checklist/chart their own data
 * persistence — that's a separate, bigger task (and paddock data is moving
 * to Google Sheets, not SQLite, so building that here would be wasted work).
 * This only unifies navigation and login *visibility* across all five tools.
 */
(function () {
  var TABS = [
    { label: '💬 Chat', href: '/tools/chat.html', match: 'chat.html' },
    { label: '📋 PCS Score', href: '/tools/pcs-score', match: 'pcs-score' },
    { label: '🧮 Calculator', href: '/tools/grazing-calculator.html', match: 'grazing-calculator' },
    { label: '📝 Plan', href: '/tools/grazing-plan-checklist.html', match: 'grazing-plan-checklist' },
    { label: '📅 Chart', href: '/tools/grazing-chart.html', match: 'grazing-chart' },
  ];

  var currentPath = window.location.pathname;

  var style = document.createElement('style');
  style.textContent = `
    .ellis-nav {
      background: #0f140d;
      border-bottom: 3px solid #7fb069;
      padding: 10px 14px;
      display: flex;
      align-items: center;
      gap: 10px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      position: sticky;
      top: 0;
      z-index: 200;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
    .ellis-nav-logo {
      font-family: Oswald, -apple-system, sans-serif;
      font-size: 1.15rem;
      font-weight: 700;
      color: #7fb069;
      text-decoration: none;
      flex-shrink: 0;
      white-space: nowrap;
    }
    .ellis-nav-tabs {
      display: flex;
      gap: 4px;
      flex: 1;
      overflow-x: auto;
    }
    .ellis-nav-tab {
      background: #1a2314;
      border: 1px solid #3a4a32;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 0.72rem;
      color: #8a9a7d;
      text-decoration: none;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .ellis-nav-tab.active {
      background: #7fb069;
      color: #0f140d;
      font-weight: 600;
      border-color: #7fb069;
    }
    .ellis-nav-account {
      flex-shrink: 0;
      font-size: 0.68rem;
      color: #8a9a7d;
      display: flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .ellis-nav-account a {
      color: #7fb069;
      text-decoration: none;
      cursor: pointer;
    }
  `;
  document.head.appendChild(style);

  var tabsHtml = TABS.map(function (tab) {
    var isActive = currentPath.indexOf(tab.match) !== -1;
    return '<a class="ellis-nav-tab' + (isActive ? ' active' : '') + '" href="' + tab.href + '">' + tab.label + '</a>';
  }).join('');

  var email = localStorage.getItem('ellis_email');
  var token = localStorage.getItem('ellis_token');
  var accountHtml;
  if (token && email) {
    accountHtml = '<span>👤 ' + escapeHtml(email) + '</span><a id="ellis-nav-logout">Log out</a>';
  } else {
    accountHtml = '<a href="/tools/pcs-score">Log in</a>';
  }

  var nav = document.createElement('div');
  nav.className = 'ellis-nav';
  nav.innerHTML =
    '<a class="ellis-nav-logo" href="/tools/chat.html">🌾 ELLIS</a>' +
    '<div class="ellis-nav-tabs">' + tabsHtml + '</div>' +
    '<div class="ellis-nav-account">' + accountHtml + '</div>';

  document.body.insertBefore(nav, document.body.firstChild);

  var logoutLink = document.getElementById('ellis-nav-logout');
  if (logoutLink) {
    logoutLink.addEventListener('click', function () {
      localStorage.removeItem('ellis_token');
      localStorage.removeItem('ellis_email');
      window.location.reload();
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
})();
