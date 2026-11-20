(function () {
  const statusLabels = {
    pending: "待审核", approved: "已通过", rejected: "审核失败", ordered: "待揽收",
    success: "在途", delivered: "已签收", failed: "异常单", cancelled: "已取消"
  };
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[ch]));
  const value = (raw, fallback = "—") => raw === null || raw === undefined || raw === "" ? fallback : esc(raw);
  const time = raw => raw ? esc(String(raw).replace("T", " ").slice(0, 16)) : "—";
  const money = (raw, fallback = "—") => {
    const number = Number(raw);
    return raw !== null && raw !== undefined && raw !== "" && Number.isFinite(number) ? `¥${number.toFixed(2)}` : fallback;
  };
  const present = raw => raw !== null && raw !== undefined && raw !== "";
  const address = (o, prefix) => ["province", "city", "district", "address"]
    .map(key => o[`${prefix}_${key}`]).filter(Boolean).join("");
  const field = (label, content, wide = false, nowrap = false) => `<div class="od-field${wide ? " od-field-wide" : ""}${nowrap ? " od-field-nowrap" : ""}"><dt>${esc(label)}</dt><dd>${content}</dd></div>`;
  const trackTime = raw => {
    const normalized = String(raw || "").replace("T", " ");
    return {
      clock: esc(normalized.slice(11, 19) || "--:--"),
      date: esc(normalized.slice(0, 10).replaceAll("-", "/") || "—")
    };
  };
  const trackCopy = item => {
    const status = String(item.status || "").trim();
    const detail = String(item.detail || "").trim();
    if (!detail) return esc(status || "物流节点");
    if (!status || detail.includes(status)) return esc(detail);
    return `${esc(status)}&nbsp;&nbsp;${esc(detail)}`;
  };

  function requestSFCityTipAmount() {
    let modal = document.getElementById("sf-city-tip-modal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "sf-city-tip-modal";
      modal.className = "od-tip-modal";
      modal.hidden = true;
      modal.innerHTML = `<div class="od-tip-backdrop" data-tip-cancel></div>
        <section class="od-tip-dialog" role="dialog" aria-modal="true" aria-labelledby="sf-city-tip-title">
          <h3 id="sf-city-tip-title">增加骑士小费</h3>
          <p>当前订单仍在查找骑士。增加小费可提高接单概率，提交后将计入本次同城订单费用。</p>
          <form class="od-tip-form">
            <label for="sf-city-tip-amount">本次增加金额（元）</label>
            <input id="sf-city-tip-amount" type="number" min="1" max="200" step="1" value="1" required>
            <div class="od-tip-error" aria-live="polite"></div>
            <div class="od-tip-actions"><button type="button" class="btn btn-secondary" data-tip-cancel>取消</button><button type="submit" class="btn btn-primary">确认增加</button></div>
          </form>
        </section>`;
      document.body.appendChild(modal);
    }
    const input = modal.querySelector("#sf-city-tip-amount");
    const error = modal.querySelector(".od-tip-error");
    const form = modal.querySelector(".od-tip-form");
    modal.hidden = false;
    input.value = "1";
    error.textContent = "";
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return new Promise(resolve => {
      const close = result => {
        modal.hidden = true;
        form.onsubmit = null;
        modal.querySelectorAll("[data-tip-cancel]").forEach(item => { item.onclick = null; });
        document.removeEventListener("keydown", onKeydown);
        resolve(result);
      };
      const onKeydown = event => { if (event.key === "Escape") close(null); };
      form.onsubmit = event => {
        event.preventDefault();
        const amount = Number(input.value);
        if (!Number.isInteger(amount) || amount < 1 || amount > 200) {
          error.textContent = "请输入 1 至 200 元的整数金额。";
          input.focus();
          return;
        }
        close(amount);
      };
      modal.querySelectorAll("[data-tip-cancel]").forEach(item => { item.onclick = () => close(null); });
      document.addEventListener("keydown", onKeydown);
    });
  }

  function showSFCityTipNotice(title, message, error = false) {
    let modal = document.getElementById("sf-city-tip-notice");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "sf-city-tip-notice";
      modal.className = "od-tip-modal";
      modal.hidden = true;
      modal.innerHTML = `<div class="od-tip-backdrop" data-notice-close></div>
        <section class="od-tip-dialog od-tip-notice" role="alertdialog" aria-modal="true">
          <h3 data-notice-title></h3><p data-notice-message></p>
          <div class="od-tip-actions"><button type="button" class="btn btn-primary" data-notice-close>确定</button></div>
        </section>`;
      document.body.appendChild(modal);
    }
    modal.querySelector("[data-notice-title]").textContent = title;
    modal.querySelector("[data-notice-message]").textContent = message;
    modal.querySelector(".od-tip-notice").classList.toggle("is-error", error);
    modal.hidden = false;
    return new Promise(resolve => {
      const close = () => {
        modal.hidden = true;
        modal.querySelectorAll("[data-notice-close]").forEach(item => { item.onclick = null; });
        document.removeEventListener("keydown", onKeydown);
        resolve();
      };
      const onKeydown = event => { if (event.key === "Escape" || event.key === "Enter") close(); };
      modal.querySelectorAll("[data-notice-close]").forEach(item => { item.onclick = close; });
      document.addEventListener("keydown", onKeydown);
      setTimeout(() => modal.querySelector("button")?.focus(), 0);
    });
  }

  async function addSFCityTip(orderId, button) {
    const amount = await requestSFCityTipAmount();
    if (amount === null) return false;
    const original = button?.textContent || "加小费";
    if (button) { button.disabled = true; button.textContent = "提交中…"; }
    try {
      const response = await fetch(`/api/orders/${Number(orderId)}/sf-city-tip`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({amount})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.detail || data.message || "加小费失败");
      await showSFCityTipNotice("小费增加成功", `${data.message || "小费增加成功"}${Number(data.total_gratuity_fee || 0) > 0 ? `，当前累计小费 ¥${Number(data.total_gratuity_fee).toFixed(2)}` : ""}`);
      return true;
    } catch (error) {
      await showSFCityTipNotice("暂时无法增加小费", error.message || "加小费失败", true);
      return false;
    } finally {
      if (button) { button.disabled = false; button.textContent = original; }
    }
  }

  function approvalFlow(order) {
    const rejected = order.status === "rejected";
    const total = Number(order.approval_total_levels || 1);
    const current = Number(order.approval_current_level || 1);
    const level1Required = order.approval_level1_required !== false;
    const node = level => {
      const doneTime = order[`approval_level${level}_time`];
      const userName = order[`approval_level${level}_user_name`] || `第${level}级审批人`;
      const remark = order[`approval_level${level}_remark`];
      const adminSkipped = String(remark || "").startsWith("[管理员跳过]");
      const rejectedHere = rejected && current === level;
      const skipped = level === 1 && !level1Required;
      const stateClass = skipped ? "done" : (rejectedHere ? "rejected" : (doneTime && !rejectedHere ? "done" : "waiting"));
      const title = skipped ? "一级审批（暂未启用）" : (rejectedHere ? `第${level}级审批驳回` : (adminSkipped ? `第${level}级审批已由管理员跳过` : (doneTime ? `第${level}级审批通过` : `等待第${level}级审批`)));
      const detail = skipped ? `直属主管：${value(userName)} · 已跳过` : (doneTime ? `${value(userName)} · ${time(doneTime)}` : `指定审批人：${value(userName)}`);
      return `<div class="od-flow-node ${stateClass}"><span></span><div><strong>${title}</strong><p>${detail}</p>${remark ? `<p class="od-flow-remark">${esc(remark)}</p>` : ""}</div></div>`;
    };
    const approved = !rejected && order.approve_time;
    return `<div class="od-flow">
      <div class="od-flow-node done"><span></span><div><strong>发起寄件申请</strong><p>${value(order.created_by_name, "下单员工")} · ${time(order.created_at)}</p></div></div>
      ${node(1)}
      ${total === 2 ? node(2) : ""}
      ${approved && order.order_time ? `<div class="od-flow-node done"><span></span><div><strong>已向快递公司下单</strong><p>${time(order.order_time)}</p></div></div>` : ""}
    </div>`;
  }

  function actualFeeItems(order) {
    const detail = order.actual_fee_detail;
    let items = [];
    if (Array.isArray(detail)) {
      items = detail.map((item, index) => ({
        name: item.name || item.feeName || item.feeTypeName || item.typeDesc || item.businessTypeDesc || `费用项 ${item.feeType || item.type || index + 1}`,
        amount: item.afterBeforehandDiscount ?? item.money ?? item.value ?? item.amount ?? item.fee
      })).filter(item => present(item.amount) && Number.isFinite(Number(item.amount)) && Number(item.amount) !== 0);
    } else if (detail && typeof detail === "object") {
      const labels = {
        transportPrice: "基础运费", freight: "运费", freightPrice: "运费",
        resourceRegulationFee: "资源调节费", arriveCharge: "到达服务费",
        chargeForDelivery: "送货费", interceptFee: "拦截服务费",
        loadUnloadFee: "装卸费", otherWarehouseCharge: "其他仓储费",
        overWeightOperationFee: "超重操作费", overweightHandlingServiceFee: "超重处理费",
        pickCharge: "提货费", receiptReturnFee: "签单返还费", returnFee: "退回费",
        storageFee: "仓储费", transferFee: "中转费", unpackingFee: "拆包费",
        valuablePackagFee: "贵重物品包装费", vistReceivePrice: "预约收货费",
        woodenFrameFee: "木架费", easyInstallAtionFee: "简易安装费",
        insurance: "保价费", insurancePrice: "保价费", fuelSurcharge: "燃油附加费",
        deliveryFee: "派送费", pickupFee: "取件费", otherPrice: "其他费用",
        packingPrice: "包装费", codPrice: "代收货款服务费", taxPrice: "税费",
        bigUpstairsFee: "大件上楼费", cashOnDeliveryFee: "代收货款服务费",
        deliveryInboundFee: "送货进仓费", easyInstallationFee: "简易安装费",
        extraDeliveryFee: "超区派送费", extraFareDeliveryServiceFee: "附加派送服务费",
        favorFee: "优惠金额", installationFee: "安装费"
      };
      items = Object.entries(detail)
        .filter(([key, raw]) => key !== "totalPrice" && /(?:fee|price|amount|freight|insurance|surcharge)$/i.test(key) && present(raw) && Number.isFinite(Number(raw)) && Number(raw) !== 0)
        .map(([key, raw]) => ({name: labels[key] || key, amount: raw}));
    }
    return items;
  }

  function renderFeeItems(order) {
    const items = actualFeeItems(order);
    if (!items.length) return "";
    return `<div class="od-fee-list">${items.map(item => `<div><span>${esc(item.name)}</span><strong>${money(item.amount)}</strong></div>`).join("")}</div>`;
  }

  function renderTrackPane(target, order) {
    if (!order.courier_order_no) {
      target.innerHTML = '<div class="od-track-empty">生成运单号后可查询物流轨迹</div>';
      return;
    }
    target.innerHTML = '<div class="od-track-loading">正在查询物流轨迹…</div>';
    fetch(`/api/orders/${order.id}/track`).then(response => response.json().then(data => ({response, data})))
      .then(({response, data}) => {
        if (!response.ok || !data.ok) throw new Error(data.detail || "物流查询失败");
        const tracks = data.tracks || [];
        target.innerHTML = `<div class="od-track-toolbar"><button type="button" class="btn btn-secondary btn-sm" data-refresh-track>刷新轨迹和费用</button></div>${tracks.length ? `<div class="od-track-list">${tracks.map(item => { const when = trackTime(item.time); return `<article class="od-track-node"><time><b>${when.clock}</b><span>${when.date}</span></time><i aria-hidden="true"></i><p>${trackCopy(item)}</p></article>`; }).join("")}</div>` : '<div class="od-track-empty">快递公司暂未返回物流轨迹</div>'}`;
        target.querySelector("[data-refresh-track]")?.addEventListener("click", async event => {
          const button = event.currentTarget;
          button.disabled = true; button.textContent = "刷新中…";
          try {
            const response = await fetch(`/api/orders/${order.id}/refresh-track`, {method: "POST"});
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.detail || "刷新失败");
            const detailResponse = await fetch(`/api/orders/${order.id}`);
            const detailData = await detailResponse.json();
            if (!detailResponse.ok || !detailData.ok) throw new Error(detailData.detail || "详情刷新失败");
            window.applyOrderRowStatus?.(detailData.order);
            if (data.freight_message && (data.actual_freight === null || data.actual_freight === undefined)) {
              alert(data.freight_message);
            }
            render(target.closest(".drawer-body") || target.parentElement.parentElement, detailData.order);
            const newTrackTab = document.querySelector(`[data-order-detail="${order.id}"] [data-od-tab="track"]`);
            newTrackTab?.click();
          } catch (error) { alert(error.message || "刷新失败"); button.disabled = false; button.textContent = "刷新轨迹和费用"; }
        });
      }).catch(error => { target.innerHTML = `<div class="od-track-empty">${esc(error.message || "物流轨迹加载失败")}</div>`; });
  }

  function render(target, order) {
    target.classList.add("od-drawer-host");
    const courierType = String(order.courier_type || "").toUpperCase();
    const courier = courierType === "SF_CITY" ? "顺丰同城急送" : (courierType === "SF" ? "顺丰速运" : (["JDL", "JD"].includes(courierType) ? "京东物流" : "德邦快递"));
    const service = order.express_type_name ? `（${esc(order.express_type_name)}）` : "";
    const logo = ["SF", "SF_CITY"].includes(courierType) ? "/static/sf-logo.svg" : (["JDL", "JD"].includes(courierType) ? "/static/jdl-logo.svg" : "/static/deppon-logo.svg");
    const pickup = order.pickup_time_mode === "appointment"
      ? `${time(order.pickup_start_time)} 至 ${time(order.pickup_end_time)}` : "立即上门";
    const reviewing = order.status === "pending" || order.status === "rejected";
    const confirmedFreightRaw = present(order.actual_freight)
      ? order.actual_freight : (courierType === "SF_CITY" ? order.freight : null);
    const actualFreight = Number(confirmedFreightRaw);
    const hasActualTotal = present(confirmedFreightRaw) && Number.isFinite(actualFreight) && actualFreight !== 0;
    const hasActualFee = hasActualTotal || actualFeeItems(order).length > 0;
    const trackStatus = String(order.track_status || "");
    const pickupConfirmed = ["success", "delivered"].includes(String(order.status || ""))
      || Boolean(order.courier_picked_by || order.courier_picked_by_phone)
      || /已揽收|揽收成功|收取快件|已取件|取件成功|已收件|收件成功|揽件成功|揽件完成|已收货|在途|运输|转运|中转|发往|已发出|到达网点|到达分拨|快件已到达|离开网点|离开分拨|派送|派件|投递|签收|妥投|已送达|配送完成/.test(trackStatus);
    const estimateFields = [
      present(order.estimated_delivery_time) ? field("预计送达", time(order.estimated_delivery_time)) : "",
      present(order.freight) ? field("预计运费", money(order.freight)) : ""
    ].join("");
    const feeSection = reviewing && estimateFields ? `<section class="od-section"><h4>预计时效与费用</h4><dl class="od-fields od-fees">${estimateFields}</dl></section>`
      : (!reviewing && (pickupConfirmed || courierType === "SF_CITY") && hasActualFee ? `<section class="od-section"><h4>快递费用明细</h4>${hasActualTotal ? `<div class="od-actual-summary"><span>${courierType === "SF_CITY" ? "同城订单金额" : "订单金额"}</span><strong>${money(confirmedFreightRaw)}</strong></div>` : ""}<div class="od-fee-detail">${renderFeeItems(order)}<p>最终金额以快递公司月结账单为准。</p></div></section>` : "");
    const waybill = order.courier_order_no || (order.courier_order_nos || []).join("、");
    const requestNumber = order.source_id || order.order_no;
    const costType = order.cost_type === "project" ? "项目归属" : (order.cost_type === "department" ? "部门归属" : order.cost_type);
    const projectName = present(order.project_name || order.project_code)
      ? String(order.project_name || order.project_code).replace(/[\\/]+/g, "-") : "";
    const attribution = [costType, order.dept_name || order.dept_code, projectName].filter(Boolean).join(" · ");
    const approvalSection = present(requestNumber) ? `<section class="od-section od-compact-section"><h4>审批信息</h4><dl class="od-fields">${field("申请编号", value(requestNumber), true, true)}</dl></section>` : "";
    const dispatchFailureSection = order.status === "failed" && !present(waybill) && present(order.dispatch_error_message)
      ? `<section class="od-section od-failure-section"><h4>正式下单失败</h4><dl class="od-fields">${present(order.dispatch_error_code) ? field("错误码", value(order.dispatch_error_code)) : ""}${field("失败原因", value(order.dispatch_error_message), true)}</dl><p class="od-failure-help">审批已经完成，但快递公司没有成功创建运单。请核对失败原因后联系管理员处理，不要重复提交审批。</p></section>`
      : "";
    const cityWaitingSection = order.sf_city_waiting_for_rider ? `<section class="od-section od-city-waiting"><h4>正在查找骑士</h4><p>顺丰同城正在匹配骑士；若 ${Number(order.sf_city_wait_timeout_minutes || 30)} 分钟内仍无人接单，系统将自动取消订单。</p><div><button type="button" class="btn btn-primary btn-sm" data-sf-city-tip>加小费，提高接单概率</button>${order.sf_city_wait_deadline ? `<span>最晚等待至 ${time(order.sf_city_wait_deadline)}</span>` : ""}</div></section>` : "";
    const receiverCompany = present(order.receiver_company) ? field("收件公司", value(order.receiver_company)) : "";
    const orderInfo = [
      present(order.created_at) ? field("申请时间", time(order.created_at)) : "",
      present(order.order_time) ? field("正式下单时间", time(order.order_time)) : "",
      field("快递服务", `${courier}${service}`), field("期望上门时间", pickup),
      present(order.item_name) ? field("物品类型", value(order.item_name)) : "",
      field("包裹数量", `${value(order.parcel_count || 1)} 个`),
      present(order.item_count) ? field("物品数量", `${value(order.item_count)} 件`) : "",
      Number(order.insurance_amount || 0) > 0 ? field("保价金额", money(order.insurance_amount)) : "",
      attribution ? field("费用归属", value(attribution), true) : "",
      present(order.shipping_reason) ? field("寄件原因", value(order.shipping_reason), true) : ""
    ].join("");
    const assignmentSection = present(order.courier_picked_by) || present(order.courier_picked_by_phone) ? `<section class="od-section"><h4>分单信息</h4><dl class="od-fields">${present(order.courier_picked_by) ? field("快递员", value(order.courier_picked_by)) : ""}${present(order.courier_picked_by_phone) ? field("联系电话", value(order.courier_picked_by_phone)) : ""}</dl></section>` : "";
    target.innerHTML = `<div class="od-root" data-order-detail="${order.id}">
      <div class="od-hero">
        <img class="od-brand" src="${logo}" alt="${courier}">
        <div class="od-hero-copy"><h3>${courier}${service}</h3><div class="od-waybill">${value(waybill, "")}</div></div>
        <span class="od-status">${value(order.status_label || statusLabels[order.status] || order.status)}</span>
      </div>
      <nav class="od-tabs"><button type="button" class="active" data-od-tab="basic"><span>▤</span>订单基本信息</button><button type="button" data-od-tab="track"><span>⌖</span>物流轨迹</button></nav>
      <div class="od-pane active" data-od-pane="basic">
      ${approvalSection}
      ${dispatchFailureSection}
      ${cityWaitingSection}
      <section class="od-section"><h4>收件信息</h4><dl class="od-fields">${field("收件人", value(order.receiver_name))}${field("联系方式", value(order.receiver_phone))}${receiverCompany}${field("收件地址", value(address(order, "receiver")), true)}</dl></section>
      <section class="od-section"><h4>订单信息</h4><dl class="od-fields">${orderInfo}</dl></section>
      ${feeSection}
      <section class="od-section"><h4>寄件信息</h4><dl class="od-fields">${field("寄件人", value(order.sender_name))}${field("联系电话", value(order.sender_phone))}${present(order.sender_company) ? field("寄件公司", value(order.sender_company)) : ""}${field("寄件地址", value(address(order, "sender")), true)}${present(order.created_by_name) ? field("下单员工", `${value(order.created_by_name)}${order.created_by_department ? ` · ${esc(order.created_by_department)}` : ""}`, true) : ""}</dl></section>
      ${assignmentSection}
      <section class="od-section"><div class="od-section-title-row"><h4>审批流程</h4><div data-approval-admin-actions></div></div>${approvalFlow(order)}</section></div>
      <div class="od-pane" data-od-pane="track"></div></div>`;
    const root = target.querySelector(".od-root");
    root.querySelector("[data-sf-city-tip]")?.addEventListener("click", async event => {
      if (await addSFCityTip(order.id, event.currentTarget)) {
        const response = await fetch(`/api/orders/${order.id}`, {cache: "no-store"});
        const data = await response.json();
        if (response.ok && data.ok) render(target, data.order);
      }
    });
    root.querySelectorAll("[data-od-tab]").forEach(button => button.addEventListener("click", () => {
      root.querySelectorAll("[data-od-tab]").forEach(item => item.classList.toggle("active", item === button));
      root.querySelectorAll("[data-od-pane]").forEach(pane => pane.classList.toggle("active", pane.dataset.odPane === button.dataset.odTab));
      if (button.dataset.odTab === "track") renderTrackPane(root.querySelector('[data-od-pane="track"]'), order);
    }));
  }

  window.CourierOrderDetail = { render, money, time, value, addSFCityTip };
}());
