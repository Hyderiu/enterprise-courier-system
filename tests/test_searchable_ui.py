from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(relative):
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def test_large_selects_are_automatically_searchable_and_white():
    script = source("web/static/js/search-select.js")
    css = source("web/static/css/app.css")
    assert "AUTO_SEARCH_THRESHOLD = 12" in script
    assert "select.options.length >= AUTO_SEARCH_THRESHOLD" in script
    assert "Object.values(option.dataset || {})" in script
    assert "background:#fff!important" in css


def test_manual_waybill_employee_selects_show_name_only_and_fit_each_row():
    template = source("web/templates/waybill_records.html")
    assert 'id="edit-owner" data-searchable' in template
    assert 'id="add-owner" class="wr-inline" data-searchable' in template
    assert 'data-placeholder="输入员工姓名"' in template
    assert "${esc(item.name)}</option>" in template
    assert "item.name)} · ${esc(item.department||'未填写部门')" not in template
    assert ".employee-select-cell .search-select-option" in template
    assert "white-space:nowrap" in template
    assert "refreshSearchableSelect?.($('edit-owner'))" in template


def test_billing_employee_picker_shows_name_only():
    template = source("web/templates/admin_monthly_exports.html")
    assert "state.users.map(x=>`<option value=\"${esc(x.name)}\"></option>`)" in template
    assert "employee?employee.name:(item.submitter_name||'')" in template
    assert "employee.department||'未填写部门'" not in template


def test_project_allocation_starts_blank_then_uses_crm_department_and_remains_editable():
    template = source("web/templates/index.html")
    assert "option.dataset.deptCode=deptCode" in template
    assert "option.dataset.deptName=deptName" in template
    assert "deptSelect.value=''" in template
    assert "deptSelect.value=match?.value || ''" in template
    assert "window.refreshSearchableSelect?.(deptSelect)" in template
    assert 'id="dept-select" data-searchable' in template
    assert 'id="project-select" data-searchable' in template


def test_address_cascades_are_searchable_and_programmatic_values_refresh():
    template = source("web/templates/index.html")
    selector = source("web/static/js/address-selector.js")
    for field in (
        "sender-province-list", "sender-city-list", "sender-district-list",
        "receiver-province-list", "receiver-city-list", "receiver-district-list",
    ):
        marker = f'id="{field}"'
        line = next(row for row in template.splitlines() if marker in row)
        assert "data-searchable" in line
    assert 'refresh(prefix, "province")' in selector
    assert 'refresh(prefix, "city")' in selector
    assert 'refresh(prefix, "district")' in selector


def test_quote_page_keeps_deppon_selectable_and_has_no_browser_timeout_abort():
    template = source("web/templates/index.html")
    assert 'quote_deferred: true' in template
    assert '以月结账单为准' in template
    assert 'setTimeout(() => controller.abort()' not in template


def test_quote_recommendation_stays_on_the_region_default_after_quotes_merge():
    template = source("web/templates/index.html")
    assert "function recomputeGlobalRecommendation()" in template
    assert "recomputeGlobalRecommendation();" in template
    assert "QUOTE_STATE.recommended_id = DEFAULT_RECOMMENDED_SERVICE_ID" in template
    assert "preferredServiceForQuote" not in template
    assert "系统默认推荐" in template
    assert "QUOTE_STATE.recommended_id = result.recommended_id" not in template
    assert '(force ? "&force=true" : "")' in template


def test_quote_refresh_triggers_only_route_weight_and_volume_fields():
    template = source("web/templates/index.html")
    marker = 'const quoteFields = ["item_weight", "item_volume", "sender_province", "sender_city"'
    assert marker in template
    quote_fields_block = template.split("const quoteFields =", 1)[1].split("];", 1)[0]
    assert '"item_name"' not in quote_fields_block
    assert 'pickup_start_time: f.elements.pickup_start_time?.value || null' in template
    assert 'pickup_end_time: f.elements.pickup_end_time?.value || null' in template
    assert "refreshQuotesAfterPickupChange()" in template
    assert "refreshServiceQuotes(true, true);" in template
    assert "正在按该时间重新查询运费和预计送达" in template


def test_sf_city_page_uses_manual_sender_required_highlights_and_submit_notice():
    template = source("web/templates/sf_city_order.html")
    assert "<span>寄件信息</span>" in template
    assert "<label>寄件人<em>*</em></label>" in template
    assert "current_user_name" not in template
    assert "current_user_phone" not in template
    assert "required-missing" in template
    assert "setTimeout(updateRequiredStyles,0)" in template
    assert "目前顺丰同城仅支持从公司<strong>三楼指定取件点</strong>寄出" in template
    assert "顺丰同城不提供现场包装服务" in template
    assert "确认并提交" in template
    assert 'id="product-type"' in template
    assert "{% for item in sf_city_product_types %}" in template
    assert '<option value="{{ item.value }}">{{ item.label }}</option>' in template
    assert "data.shop.product_types" not in template


def test_express_page_has_xiamen_sf_city_guide():
    template = source("web/templates/index.html")
    assert "{% if sf_city_available %}" in template
    assert "厦门市内急件，可优先考虑顺丰同城急送" in template
    assert "需要骑士尽快上门、当天快速送达" in template
    assert 'href="/sf-city"' in template


def test_sf_city_tip_uses_white_in_page_dialog_instead_of_browser_prompt():
    script = source("web/static/js/order-detail.js")
    styles = source("web/static/css/app.css")
    assert "requestSFCityTipAmount" in script
    assert 'id = "sf-city-tip-modal"' in script
    assert 'value="1" required' in script
    assert 'input.value = "1"' in script
    assert 'prompt("请输入本次增加的小费金额' not in script
    assert "showSFCityTipNotice" in script
    assert 'alert(`${data.message' not in script
    assert "od-tip-dialog" in styles
    assert "background:#fff" in styles
