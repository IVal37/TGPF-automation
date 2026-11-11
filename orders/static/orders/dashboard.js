// Dashboard behaviour: tab switching and the 15-second auto-refresh of the
// Dispatch Queue and Restock tabs. The URLs come from data attributes on <body>.
var pageConfig = document.body.dataset;

document.querySelectorAll('.tab-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    document.querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
    document.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.remove('active'); });
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// Polls `url` every 15s and swaps the response HTML into `containerId`,
// updating the matching tab badge to a live count of `itemSelector`
// elements. Only that tab's content is touched — the rest of the page
// (and whichever tab you're actually looking at) is left alone.
// `shouldSkip(container)` lets a caller pause refreshes while the
// dispatcher has unsaved input inside the container (an in-progress
// edit, a checkbox selection not yet submitted) so a refresh never
// silently discards something they were in the middle of doing.
function autoRefresh(url, containerId, badgeId, itemSelector, shouldSkip) {
  var container = document.getElementById(containerId);
  var badge = document.getElementById(badgeId);
  var REFRESH_MS = 15000;

  function refresh() {
    if (shouldSkip(container)) return;

    fetch(url)
      .then(function (res) {
        // session expired: fetch followed a redirect to the login page, so
        // reload the whole page (which lands on the login form) instead of
        // pasting a login form into the tab
        if (res.redirected && res.url.indexOf(pageConfig.loginUrl) !== -1) {
          window.location.reload();
          return null;
        }
        return res.text();
      })
      .then(function (html) {
        if (html === null) return;
        container.innerHTML = html;
        var count = container.querySelectorAll(itemSelector).length;
        badge.textContent = count;
        badge.style.display = count > 0 ? '' : 'none';
      })
      .catch(function () { /* network hiccup — just try again next interval */ });
  }

  setInterval(refresh, REFRESH_MS);
}

// Dispatch Queue: don't clobber a time field the dispatcher is actively editing
autoRefresh(pageConfig.pendingUrl, 'dispatch-list', 'dispatch-badge', '.dispatch-card', function (container) {
  return container.contains(document.activeElement);
});

// Restock: don't wipe out checkboxes the dispatcher has already ticked
// but not yet submitted via "Restock Time"
autoRefresh(pageConfig.restockUrl, 'restock-list', 'restock-badge', '.order-row', function (container) {
  return container.contains(document.activeElement) || container.querySelector('input[type="checkbox"]:checked');
});
