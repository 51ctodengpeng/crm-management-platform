/**
 * 主题切换（亮色 / 夜间）
 *
 * - 偏好存在 localStorage 的 crm-theme，刷新、重启浏览器后自动恢复；
 * - 在 <head> 里先用内联脚本把 data-theme 打到 <html> 上，避免首屏闪白；
 * - 同时设置 data-bs-theme，让 Bootstrap 5.3 的表单、下拉、分页等原生组件一起变暗；
 * - 只改属性、不重排 DOM，因此与现有布局、交互逻辑互不影响。
 */
(function () {
  var KEY = 'crm-theme';
  var root = document.documentElement;

  function current() {
    return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function paint(theme) {
    var dark = theme === 'dark';
    root.setAttribute('data-theme', theme);
    root.setAttribute('data-bs-theme', theme);
    var buttons = document.querySelectorAll('[data-theme-toggle]');
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
      btn.setAttribute('title', dark ? '切换到亮色模式' : '切换到夜间模式');
      var icon = btn.querySelector('i');
      if (icon) { icon.className = dark ? 'bi bi-sun' : 'bi bi-moon-stars'; }
      var label = btn.querySelector('[data-theme-label]');
      if (label) { label.textContent = dark ? '亮色' : '夜间'; }
    }
  }

  function set(theme) {
    var next = theme === 'dark' ? 'dark' : 'light';
    try { localStorage.setItem(KEY, next); } catch (e) { /* 隐私模式下忽略 */ }
    paint(next);
  }

  function toggle() {
    set(current() === 'dark' ? 'light' : 'dark');
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest ? event.target.closest('[data-theme-toggle]') : null;
    if (!btn) { return; }
    event.preventDefault();
    toggle();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { paint(current()); });
  } else {
    paint(current());
  }

  window.CRMTheme = { get: current, set: set, toggle: toggle };
})();
