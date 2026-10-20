/*!
 * 统一自定义弹窗：替代浏览器原生 confirm / alert，并与全站视觉保持一致
 *
 *   UI.confirm(message, {title, confirmText, cancelText, danger}) -> Promise<boolean>
 *   UI.alert(message, {title, confirmText})                       -> Promise<void>
 *   UI.toast(message, type)                                       -> void
 *
 * 表单声明式用法：<form data-confirm="确认删除？">…</form>
 * 由全局委托拦截提交，确认后再真正提交。
 */
(function () {
  'use strict';

  var root = null;
  var active = null;   // 当前打开实例的 { resolve, lastFocus }

  function ensureRoot() {
    if (root) { return root; }
    root = document.createElement('div');
    root.className = 'ui-dialog-layer';
    root.innerHTML =
      '<div class="ui-dialog-mask"></div>' +
      '<div class="ui-dialog" role="dialog" aria-modal="true" aria-labelledby="uiDialogTitle">' +
      '  <div class="ui-dialog-head">' +
      '    <span class="ui-dialog-icon"><i class="bi bi-question-circle"></i></span>' +
      '    <h6 class="ui-dialog-title" id="uiDialogTitle"></h6>' +
      '  </div>' +
      '  <div class="ui-dialog-body"></div>' +
      '  <div class="ui-dialog-foot">' +
      '    <button type="button" class="btn btn-light btn-sm ui-dialog-cancel">取消</button>' +
      '    <button type="button" class="btn btn-primary btn-sm ui-dialog-ok">确定</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(root);
    return root;
  }

  function open(options) {
    var opts = options || {};
    var layer = ensureRoot();
    var dialog = layer.querySelector('.ui-dialog');
    var mask = layer.querySelector('.ui-dialog-mask');
    var titleEl = layer.querySelector('.ui-dialog-title');
    var bodyEl = layer.querySelector('.ui-dialog-body');
    var iconEl = layer.querySelector('.ui-dialog-icon');
    var okBtn = layer.querySelector('.ui-dialog-ok');
    var cancelBtn = layer.querySelector('.ui-dialog-cancel');

    titleEl.textContent = opts.title || (opts.alertOnly ? '提示' : '操作确认');
    // 富内容（导出前说明等）用 html，纯文本提示仍走 textContent，避免意外解析
    if (opts.html) {
      bodyEl.innerHTML = opts.html;
      bodyEl.classList.add('is-rich');
    } else {
      bodyEl.textContent = opts.message || '';
      bodyEl.classList.remove('is-rich');
    }
    okBtn.textContent = opts.confirmText || '确定';
    cancelBtn.textContent = opts.cancelText || '取消';
    cancelBtn.style.display = opts.alertOnly ? 'none' : '';

    // 危险操作使用红色确认按钮，图标同步变红
    var danger = !!opts.danger;
    okBtn.className = 'btn btn-sm ui-dialog-ok ' + (danger ? 'btn-danger' : 'btn-primary');
    layer.classList.toggle('is-danger', danger);
    iconEl.innerHTML = danger
      ? '<i class="bi bi-exclamation-triangle"></i>'
      : (opts.alertOnly ? '<i class="bi bi-info-circle"></i>' : '<i class="bi bi-question-circle"></i>');

    var lastFocus = document.activeElement;

    return new Promise(function (resolve) {
      active = { resolve: resolve, lastFocus: lastFocus };
      layer.classList.add('is-open');
      document.body.classList.add('ui-dialog-open');

      function close(result) {
        layer.classList.remove('is-open');
        document.body.classList.remove('ui-dialog-open');
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        mask.removeEventListener('click', onCancel);
        document.removeEventListener('keydown', onKey);
        active = null;
        if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
        resolve(result);
      }
      function onOk() { close(true); }
      function onCancel() { close(false); }
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(false); }
        if (e.key === 'Enter') { e.preventDefault(); close(true); }
      }

      okBtn.addEventListener('click', onOk);
      cancelBtn.addEventListener('click', onCancel);
      mask.addEventListener('click', onCancel);
      document.addEventListener('keydown', onKey);
      window.setTimeout(function () { okBtn.focus(); }, 30);
    });
  }

  window.UI = {
    confirm: function (message, options) {
      return open(Object.assign({ message: message }, options || {}));
    },
    alert: function (message, options) {
      return open(Object.assign({ message: message, alertOnly: true }, options || {}));
    },
    toast: function (message, type) {
      var box = document.createElement('div');
      box.className = 'ui-toast ' + (type || 'info');
      box.textContent = message;
      document.body.appendChild(box);
      window.setTimeout(function () { box.classList.add('is-show'); }, 10);
      window.setTimeout(function () {
        box.classList.remove('is-show');
        window.setTimeout(function () { box.remove(); }, 250);
      }, 2600);
    }
  };

  // ---- 声明式确认：form[data-confirm] ----
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) { return; }
    var message = form.getAttribute('data-confirm');
    if (!message) { return; }
    e.preventDefault();
    var danger = form.getAttribute('data-confirm-danger') !== '0';
    UI.confirm(message, {
      danger: danger,
      title: form.getAttribute('data-confirm-title') || (danger ? '危险操作确认' : '操作确认'),
      confirmText: form.getAttribute('data-confirm-ok') || (danger ? '确认删除' : '确定')
    }).then(function (ok) {
      if (!ok) { return; }
      // 原生 submit() 不再触发 submit 事件，避免递归
      HTMLFormElement.prototype.submit.call(form);
    });
  }, true);

  // ---- 声明式确认：a[data-confirm-template] / a[data-confirm] ----
  // 链接类操作（如导出数据）无法用表单拦截，改为点击时先弹确认，确认后再跳转。
  document.addEventListener('click', function (e) {
    var link = e.target.closest ? e.target.closest('a[data-confirm], a[data-confirm-template]') : null;
    if (!link) { return; }
    if (link.getAttribute('data-confirm-done') === '1') { return; }   // 已确认过，放行
    e.preventDefault();

    var tplSel = link.getAttribute('data-confirm-template');
    var options = {
      title: link.getAttribute('data-confirm-title') || '导出确认',
      confirmText: link.getAttribute('data-confirm-ok') || '确认导出',
      cancelText: '取消',
      danger: link.getAttribute('data-confirm-danger') === '1'
    };
    var tpl = tplSel ? document.querySelector(tplSel) : null;
    if (tpl) {
      options.html = tpl.innerHTML;
    } else {
      options.message = link.getAttribute('data-confirm') || '确认执行该操作？';
    }

    UI.confirm(options.message || '', options).then(function (ok) {
      if (!ok) { return; }
      link.setAttribute('data-confirm-done', '1');
      if (link.target === '_blank') {
        window.open(link.href, '_blank', 'noopener');
      } else {
        window.location.href = link.href;
      }
      // 浏览器返回时页面可能来自缓存，标记要复位，否则第二次点击会跳过确认
      window.setTimeout(function () { link.removeAttribute('data-confirm-done'); }, 800);
    });
  }, true);
})();
