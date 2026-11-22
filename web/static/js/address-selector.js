(function () {
  "use strict";

  let divisions = [];
  const directMunicipalities = new Set(["北京市", "天津市", "上海市", "重庆市"]);

  function input(prefix, level) {
    return document.querySelector(`[name="${prefix}_${level}"]`);
  }

  function refresh(prefix, level) {
    window.refreshSearchableSelect?.(input(prefix, level));
  }

  function exact(items, name) {
    const value = String(name || "").trim();
    return (items || []).find(item => item.name === value) || null;
  }

  function citiesOf(province) {
    if (!province) return [];
    if (directMunicipalities.has(province.name)) {
      return [{code: province.code, name: province.name, children: (province.children || []).flatMap(x => x.children || [])}];
    }
    return province.children || [];
  }

  function fillList(prefix, level, items) {
    const field = input(prefix, level);
    const list = document.getElementById(`${prefix}-${level}-list`);
    if (!list || !field) return;
    const options = (items || []).map(item => {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = item.name;
      return option;
    });
    if (field.tagName === "SELECT") {
      const current = field.value;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = field.dataset.placeholder || "请选择";
      field.replaceChildren(placeholder, ...options);
      if ((items || []).some(item => item.name === current)) field.value = current;
      refresh(prefix, level);
      return;
    }
    list.replaceChildren(...options);
  }

  function updateCities(prefix, clearChildren) {
    const province = exact(divisions, input(prefix, "province").value);
    const cities = citiesOf(province);
    fillList(prefix, "city", cities);
    if (clearChildren) {
      input(prefix, "city").value = directMunicipalities.has(province?.name) ? province.name : "";
      input(prefix, "district").value = "";
      refresh(prefix, "city");
      refresh(prefix, "district");
    }
    updateDistricts(prefix, clearChildren && !directMunicipalities.has(province?.name));
  }

  function updateDistricts(prefix, clearDistrict) {
    const province = exact(divisions, input(prefix, "province").value);
    const city = exact(citiesOf(province), input(prefix, "city").value);
    fillList(prefix, "district", city?.children || []);
    if (clearDistrict) input(prefix, "district").value = "";
    refresh(prefix, "district");
  }

  function bind(prefix) {
    fillList(prefix, "province", divisions);
    input(prefix, "province").addEventListener("change", () => updateCities(prefix, true));
    input(prefix, "province").addEventListener("input", () => {
      if (exact(divisions, input(prefix, "province").value)) updateCities(prefix, true);
    });
    input(prefix, "city").addEventListener("change", () => updateDistricts(prefix, true));
    input(prefix, "city").addEventListener("input", () => {
      const province = exact(divisions, input(prefix, "province").value);
      if (exact(citiesOf(province), input(prefix, "city").value)) updateDistricts(prefix, true);
    });
    updateCities(prefix, false);
    updateDistricts(prefix, false);
  }

  window.setAddressSelection = async function (prefix, provinceName, cityName, districtName) {
    await window.addressSelectorReady;
    input(prefix, "province").value = provinceName || "";
    refresh(prefix, "province");
    updateCities(prefix, false);
    const province = exact(divisions, provinceName);
    input(prefix, "city").value = directMunicipalities.has(province?.name) ? province.name : (cityName || "");
    refresh(prefix, "city");
    updateDistricts(prefix, false);
    input(prefix, "district").value = districtName || "";
    refresh(prefix, "district");
  };

  window.validateAllAddressSelectors = function (options = {}) {
    const pairs = [["sender", "寄件地址"]];
    if (options.includeReceiver !== false) pairs.push(["receiver", "收件地址"]);
    for (const [prefix, label] of pairs) {
      const province = exact(divisions, input(prefix, "province").value);
      if (!province) return {ok: false, message: `${label}的省份必须从标准列表中选择`};
      const city = exact(citiesOf(province), input(prefix, "city").value);
      if (!city) return {ok: false, message: `${label}的城市必须从所选省份下的标准列表中选择`};
      const district = exact(city.children || [], input(prefix, "district").value);
      if (!district) return {ok: false, message: `${label}的区县必须从所选城市下的标准列表中选择`};
    }
    return {ok: true};
  };

  window.addressSelectorReady = fetch("/static/data/pcas-code.json")
    .then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then(data => {
      divisions = data;
      bind("sender");
      bind("receiver");
    })
    .catch(error => {
      console.error("行政区划数据加载失败", error);
      throw new Error("标准地址数据加载失败，请刷新页面后重试");
    });
})();
