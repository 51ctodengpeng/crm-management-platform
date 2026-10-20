/*!
 * 省 / 市 / 区 三级联动
 * - 离线数据：static/data/regions.json（由 tools/build_regions.py 生成）
 * - 无框架依赖，支持编辑时回填历史文本（如「江苏省苏州市」）
 * - 无法解析的历史值自动切换为「手动填写」模式，不丢失原值
 */
(function () {
  'use strict';

  function parseRegion(value, data) {
    if (!value) { return null; }
    var norm = String(value).replace(/[\s\/,，、\-]+/g, '');
    for (var i = 0; i < data.length; i++) {
      var prov = data[i];
      if (norm.indexOf(prov.name) < 0) { continue; }
      for (var j = 0; j < prov.children.length; j++) {
        var city = prov.children[j];
        if (norm.indexOf(city.name) < 0) { continue; }
        for (var k = 0; k < city.children.length; k++) {
          var dist = city.children[k];
          if (dist && norm.indexOf(dist) >= 0) {
            return { province: prov.name, city: city.name, district: dist };
          }
        }
        return { province: prov.name, city: city.name, district: '' };
      }
      return { province: prov.name, city: '', district: '' };
    }
    return null;
  }

  function fillSelect(select, items, placeholder, selected) {
    select.innerHTML = '';
    var first = document.createElement('option');
    first.value = '';
    first.textContent = placeholder;
    select.appendChild(first);
    items.forEach(function (name) {
      var opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === selected) { opt.selected = true; }
      select.appendChild(opt);
    });
  }

  function init(root, data) {
    var provSel = root.querySelector('.rp-province');
    var citySel = root.querySelector('.rp-city');
    var distSel = root.querySelector('.rp-district');
    var manual = root.querySelector('.rp-manual');
    var toggle = root.querySelector('.rp-toggle');
    var hint = root.querySelector('.rp-hint');
    var selects = root.querySelector('.rp-selects');

    fillSelect(provSel, data.map(function (p) { return p.name; }), '省份', '');
    fillSelect(citySel, [], '城市', '');
    fillSelect(distSel, [], '区 / 县', '');

    function provinceNode(name) {
      for (var i = 0; i < data.length; i++) {
        if (data[i].name === name) { return data[i]; }
      }
      return null;
    }

    function cityNode(province, name) {
      if (!province) { return null; }
      for (var i = 0; i < province.children.length; i++) {
        if (province.children[i].name === name) { return province.children[i]; }
      }
      return null;
    }

    function syncCities(keepCity, keepDistrict) {
      var p = provinceNode(provSel.value);
      var cities = p ? p.children.map(function (c) { return c.name; }) : [];
      fillSelect(citySel, cities, '城市', keepCity || '');
      syncDistricts(keepDistrict);
    }

    function syncDistricts(keep) {
      var p = provinceNode(provSel.value);
      var c = cityNode(p, citySel.value);
      var districts = c ? c.children : [];
      // 直辖市市级与省级同名，区县直接列在市级下
      fillSelect(distSel, districts, districts.length ? '区 / 县' : '暂无', keep || '');
      distSel.disabled = districts.length === 0;
    }

    provSel.addEventListener('change', function () { syncCities('', ''); });
    citySel.addEventListener('change', function () { syncDistricts(''); });

    // ---- 手动模式 ----
    function setManual(on, value) {
      root.classList.toggle('is-manual', on);
      selects.classList.toggle('d-none', on);
      manual.classList.toggle('d-none', !on);
      manual.disabled = !on;
      provSel.disabled = on;
      citySel.disabled = on;
      distSel.disabled = on || distSel.options.length <= 1;
      toggle.textContent = on ? '改为三级选择' : '手动填写地区';
      if (on) {
        manual.value = value || '';
        manual.focus();
        hint.textContent = '当前为手动填写模式，保存后按填写内容显示。';
      } else {
        hint.textContent = '';
      }
    }

    toggle.addEventListener('click', function () {
      setManual(!root.classList.contains('is-manual'), root.dataset.value || '');
      if (!root.classList.contains('is-manual')) {
        var parsed = parseRegion(root.dataset.value || '', data);
        if (parsed) { apply(parsed); }
      }
    });

    function apply(parsed) {
      provSel.value = parsed.province || '';
      syncCities(parsed.city || '', parsed.district || '');
      distSel.value = parsed.district || '';
    }

    var parsed = parseRegion(root.dataset.value, data);
    if (root.dataset.value && !parsed) {
      setManual(true, root.dataset.value);
    } else if (parsed) {
      apply(parsed);
    }
  }

  function boot() {
    var roots = Array.prototype.slice.call(document.querySelectorAll('.region-picker'));
    if (!roots.length) { return; }
    var url = roots[0].dataset.url;
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.json();
      })
      .then(function (data) {
        roots.forEach(function (root) { init(root, data); });
      })
      .catch(function (err) {
        // 数据加载失败时降级为手动输入，保证仍能录入
        roots.forEach(function (root) {
          var manual = root.querySelector('.rp-manual');
          var selects = root.querySelector('.rp-selects');
          var hint = root.querySelector('.rp-hint');
          if (selects) { selects.classList.add('d-none'); }
          if (manual) { manual.classList.remove('d-none'); manual.disabled = false; }
          if (hint) { hint.textContent = '地区数据加载失败，已切换为手动填写。'; }
          if (window.console) { console.warn('regions.json 加载失败', err); }
        });
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
