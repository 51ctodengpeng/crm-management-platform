/*!
 * 列表页批量操作：多选、全选、批量动作
 * 约定元素：form#bulkForm / #bulkBar / #bulkCount / #bulkAction / #checkAll
 *           .row-check（值=记录 ID）/ #bulkClear / 值下拉由 data-value-selects 指定
 * 确认与提示统一走 static/js/dialog.js 的自定义弹窗（UI.confirm / UI.alert）。
 */
(function () {
  'use strict';

  function ask(message, options) {
    if (window.UI && window.UI.confirm) {
      return window.UI.confirm(message, options);
    }
    return Promise.resolve(window.confirm(message));
  }

  function tell(message) {
    if (window.UI && window.UI.alert) {
      return window.UI.alert(message);
    }
    window.alert(message);
    return Promise.resolve();
  }

  function boot() {
    // 两种用法：项目/客户列表用 form#bulkForm（带批量动作下拉），
    // 对账中心用 form[data-bulk-form]（只有批量对账一个动作，不放下拉）
    var form = document.getElementById('bulkForm') || document.querySelector('form[data-bulk-form]');
    if (!form) { return; }

    var bar = document.getElementById('bulkBar');
    var countEl = document.getElementById('bulkCount');
    var actionSel = document.getElementById('bulkAction');
    var checkAll = document.getElementById('checkAll');
    var clearBtn = document.getElementById('bulkClear');
    var valueIds = (form.dataset.valueSelects || '').split(',').filter(Boolean);
    var valueSelects = valueIds.map(function (id) { return document.getElementById(id); });

    function rows() {
      return Array.prototype.slice.call(document.querySelectorAll('.row-check'));
    }
    function selected() {
      return rows().filter(function (cb) { return cb.checked; });
    }

    function refresh() {
      var all = rows();
      var n = selected().length;
      if (countEl) { countEl.textContent = n; }
      if (bar) { bar.classList.toggle('is-visible', n > 0); }
      if (checkAll) {
        checkAll.checked = n > 0 && n === all.length;
        checkAll.indeterminate = n > 0 && n < all.length;
      }
    }

    function syncValue() {
      var action = actionSel ? actionSel.value : '';
      valueSelects.forEach(function (sel) {
        if (!sel) { return; }
        var map = (sel.dataset.forActions || '').split(',');
        var on = map.indexOf(action) >= 0;
        sel.classList.toggle('d-none', !on);
        sel.disabled = !on;
        sel.name = on ? 'value' : '';
      });
    }

    rows().forEach(function (cb) { cb.addEventListener('change', refresh); });

    if (checkAll) {
      checkAll.addEventListener('change', function () {
        rows().forEach(function (cb) { cb.checked = checkAll.checked; });
        refresh();
      });
    }

    if (actionSel) { actionSel.addEventListener('change', syncValue); }

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        rows().forEach(function (cb) { cb.checked = false; });
        if (actionSel) { actionSel.value = ''; }
        syncValue();
        refresh();
      });
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();

      var picked = selected();
      if (!picked.length) {
        tell('请先勾选要操作的记录');
        return;
      }

      // 没有动作下拉 = 单一动作表单，勾选框已通过 form 属性关联，直接提交
      if (!actionSel) {
        HTMLFormElement.prototype.submit.call(form);
        return;
      }

      var action = actionSel.value;
      if (!action) {
        tell('请选择批量操作类型');
        return;
      }

      function submitNow() {
        // 清理上一次注入的隐藏域，避免重复提交
        Array.prototype.slice.call(form.querySelectorAll('input[data-bulk-id]'))
          .forEach(function (n) { n.remove(); });
        picked.forEach(function (cb) {
          var h = document.createElement('input');
          h.type = 'hidden';
          h.name = 'ids';
          h.value = cb.value;
          h.setAttribute('data-bulk-id', '1');
          form.appendChild(h);
        });
        // 原生 submit() 不再触发 submit 事件，避免二次弹窗
        HTMLFormElement.prototype.submit.call(form);
      }

      if (action === 'delete') {
        var tip = form.dataset.deleteTip || '确认删除选中的 {n} 条记录？此操作不可恢复！';
        ask(tip.replace('{n}', picked.length), {
          danger: true,
          title: '批量删除确认',
          confirmText: '确认删除',
          cancelText: '再想想'
        }).then(function (ok) { if (ok) { submitNow(); } });
      } else {
        submitNow();
      }
    });

    syncValue();
    refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
