(function () {
  const enhanced = new WeakSet();
  const AUTO_SEARCH_THRESHOLD = 12;

  function shouldEnhance(select) {
    return select?.matches?.('select')
      && !select.multiple
      && !select.hasAttribute('data-no-search')
      && (select.hasAttribute('data-searchable') || select.options.length >= AUTO_SEARCH_THRESHOLD);
  }

  function enhance(select) {
    if (!shouldEnhance(select) || enhanced.has(select)) return;
    enhanced.add(select);

    const wrapper = document.createElement('div');
    wrapper.className = 'search-select';
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'search-select-input';
    input.autocomplete = 'off';
    input.placeholder = select.dataset.placeholder || '输入关键词选择';
    input.required = select.required;
    select.required = false;
    input.setAttribute('aria-label', select.getAttribute('aria-label') || input.placeholder);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'search-select-toggle';
    toggle.setAttribute('aria-label', '展开选项');
    toggle.textContent = '⌄';

    const menu = document.createElement('div');
    menu.className = 'search-select-menu';
    menu.hidden = true;
    wrapper.append(input, toggle, menu);

    function options() {
      return [...select.options].filter(option => !option.disabled);
    }
    function selectedLabel() {
      return select.selectedOptions[0]?.textContent?.trim() || '';
    }
    function syncInput() {
      input.value = selectedLabel();
      input.disabled = select.disabled;
    }
    function choose(option) {
      select.value = option.value;
      input.value = option.textContent.trim();
      menu.hidden = true;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
    let blurTimer = null;
    function render(query = '') {
      const keyword = query.trim().toLocaleLowerCase();
      const matches = options().filter(option => {
        if (!keyword) return true;
        const searchable = [
          option.textContent, option.value, ...Object.values(option.dataset || {})
        ].join(' ').toLocaleLowerCase();
        return searchable.includes(keyword);
      });
      menu.replaceChildren();
      if (!matches.length) {
        const empty = document.createElement('div');
        empty.className = 'search-select-empty';
        empty.textContent = '没有匹配选项';
        menu.appendChild(empty);
      } else {
        matches.forEach(option => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'search-select-option' + (option.value === select.value ? ' active' : '');
          button.textContent = option.textContent.trim();
          button.addEventListener('mousedown', event => event.preventDefault());
          button.addEventListener('click', () => choose(option));
          menu.appendChild(button);
        });
      }
      menu.hidden = false;
    }

    input.addEventListener('focus', () => {
      clearTimeout(blurTimer);
      render(input.value === selectedLabel() ? '' : input.value);
    });
    input.addEventListener('click', () => {
      clearTimeout(blurTimer);
      if (menu.hidden) render(input.value === selectedLabel() ? '' : input.value);
    });
    input.addEventListener('input', () => render(input.value));
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { menu.hidden = true; syncInput(); }
      if (event.key === 'Enter' && !menu.hidden) {
        const first = menu.querySelector('.search-select-option');
        if (first) { event.preventDefault(); first.click(); }
      }
    });
    input.addEventListener('blur', () => {
      clearTimeout(blurTimer);
      blurTimer = setTimeout(() => {
      const exact = options().find(option => option.textContent.trim().toLocaleLowerCase() === input.value.trim().toLocaleLowerCase());
      if (exact) choose(exact); else syncInput();
      menu.hidden = true;
      }, 180);
    });
    toggle.addEventListener('mousedown', event => event.preventDefault());
    toggle.addEventListener('click', () => {
      clearTimeout(blurTimer);
      if (menu.hidden) { input.focus(); render(''); } else menu.hidden = true;
    });
    select.addEventListener('change', syncInput);
    select._searchSelectRefresh = () => { syncInput(); if (!menu.hidden) render(input.value); };
    new MutationObserver(select._searchSelectRefresh).observe(select, { childList: true, subtree: true, attributes: true });
    select.form?.addEventListener('reset', () => setTimeout(syncInput));
    syncInput();
  }

  function scan(root = document) {
    if (root.matches?.('select')) enhance(root);
    root.querySelectorAll?.('select').forEach(enhance);
  }
  window.refreshSearchableSelect = select => select?._searchSelectRefresh?.();
  scan();
  new MutationObserver(records => records.forEach(record => {
    if (record.target?.matches?.('select')) enhance(record.target);
    record.addedNodes.forEach(node => {
      if (node.nodeType === 1) scan(node);
    });
  })).observe(document.body, { childList: true, subtree: true });
  document.addEventListener('click', event => {
    document.querySelectorAll('.search-select-menu:not([hidden])').forEach(menu => {
      if (!menu.parentElement.contains(event.target)) menu.hidden = true;
    });
  });
})();
