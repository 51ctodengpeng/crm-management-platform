/*!
 * 筛选条件即选即生效（无「筛选 / 确认」步骤）
 *
 * 用法：<form method="get" data-auto-filter>
 *   - select / date / checkbox / radio：change 立即提交
 *   - 文本输入：停止输入 600ms 后提交；回车立即提交
 *   - 带 data-autofocus 的输入框在刷新后自动聚焦并定位到末尾，便于连续输入
 * 提交前会把空值控件禁用，保持 URL 干净（等价于「不限制该条件」）。
 */
(function () {
  'use strict';

  var DEBOUNCE = 600;

  function boot() {
    var forms = Array.prototype.slice.call(document.querySelectorAll('form[data-auto-filter]'));
    if (!forms.length) { return; }

    forms.forEach(function (form) {
      var timer = null;
      var submitting = false;

      function submitNow() {
        if (submitting) { return; }
        submitting = true;
        // 空值不参与查询：禁用后不进入 query string
        Array.prototype.slice.call(form.querySelectorAll('select, input')).forEach(function (el) {
          if (el.type === 'submit' || el.type === 'button') { return; }
          if (!el.value) { el.disabled = true; }
        });
        form.submit();
      }

      form.querySelectorAll('select').forEach(function (el) {
        el.addEventListener('change', submitNow);
      });

      form.querySelectorAll('input[type="date"], input[type="checkbox"], input[type="radio"]')
        .forEach(function (el) { el.addEventListener('change', submitNow); });

      form.querySelectorAll('input[type="text"], input[type="search"], input[type="number"]')
        .forEach(function (el) {
          el.addEventListener('input', function () {
            window.clearTimeout(timer);
            timer = window.setTimeout(submitNow, DEBOUNCE);
          });
          el.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
              e.preventDefault();
              window.clearTimeout(timer);
              submitNow();
            }
          });
        });

      // 回车在任意控件上都可立即触发（保持原有习惯）
      form.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && e.target.tagName === 'SELECT') {
          e.preventDefault();
          submitNow();
        }
      });

      var focusEl = form.querySelector('[data-autofocus]');
      if (focusEl && focusEl.value) {
        window.setTimeout(function () {
          focusEl.focus();
          var len = focusEl.value.length;
          try { focusEl.setSelectionRange(len, len); } catch (err) { /* number 类型不支持 */ }
        }, 80);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
