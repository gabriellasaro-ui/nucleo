// Nucleo - glue between HTMX server events and the Alpine-driven shell.
// The server responds to writes with HX-Trigger events such as
//   {"nucleo:closeModal": true, "nucleo:toast": {"text": "..."}}
(function () {
  document.body.addEventListener("nucleo:closeModal", function () {
    window.dispatchEvent(new CustomEvent("nucleo-close-modal"));
  });

  document.body.addEventListener("nucleo:toast", function (event) {
    var detail = event.detail || {};
    window.nucleoToast(detail.text || "Ação concluída", detail.kind || "success");
  });

  document.body.addEventListener("nucleo:pipelineRenamed", function (event) {
    var detail = event.detail || {};
    if (!detail.name) return;
    document.querySelectorAll("[data-current-pipeline-name]").forEach(function (el) {
      el.textContent = detail.name;
    });
  });

  var TOAST_ICONS = {
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 6.5"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
    warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 2.6 17.5a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4"/><path d="M12 17h.01"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5h.01"/></svg>'
  };
  var TOAST_CLOSE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>';
  function iconFor(kind) { return TOAST_ICONS[kind] || TOAST_ICONS.success; }

  function dismissToast(el) {
    if (!el || el.dataset.dismissing === "true") return;
    el.dataset.dismissing = "true";
    el.style.transition = "opacity .22s ease, transform .22s ease";
    el.style.opacity = "0";
    el.style.transform = "translateY(-6px) scale(.98)";
    setTimeout(function () { el.remove(); }, 240);
  }

  function installToast(el) {
    if (!el || el.dataset.toastReady === "true") return;
    el.dataset.toastReady = "true";
    var text = el.textContent.trim();
    var kind = el.classList.contains("error") ? "error"
      : el.classList.contains("warning") ? "warning"
      : el.classList.contains("info") ? "info"
      : "success";
    el.className = "toast " + kind;
    el.innerHTML = '<span class="toast__icon" aria-hidden="true">' + iconFor(kind) + '</span>'
      + '<span class="toast__text"></span>'
      + '<button type="button" class="toast__close" aria-label="Fechar notificação">' + TOAST_CLOSE + '</button>';
    var textEl = el.querySelector(".toast__text");
    if (textEl) textEl.textContent = text;
    var close = el.querySelector(".toast__close");
    if (close) close.addEventListener("click", function () { dismissToast(el); });
    setTimeout(function () { dismissToast(el); }, kind === "error" ? 5600 : 3600);
  }

  window.nucleoToast = function (text, kind) {
    var host = document.getElementById("toasts");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = text;
    host.appendChild(el);
    installToast(el);
  };

  function initToasts() {
    Array.prototype.forEach.call(document.querySelectorAll(".toast"), installToast);
  }

  function syncDealStageFields(form) {
    if (!form) return;
    var select = form.querySelector("[data-deal-stage-select]");
    if (!select) return;
    var activeStage = select.value;
    Array.prototype.forEach.call(form.querySelectorAll("[data-deal-stage-fields]"), function (group) {
      var active = group.dataset.dealStageFields === activeStage;
      group.hidden = !active;
      Array.prototype.forEach.call(group.querySelectorAll("input, select, textarea"), function (field) {
        field.disabled = !active;
      });
    });
  }

  function initDealStageFields() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-deal-form]"), function (form) {
      if (form.dataset.stageFieldsReady === "true") {
        syncDealStageFields(form);
        return;
      }
      form.dataset.stageFieldsReady = "true";
      var select = form.querySelector("[data-deal-stage-select]");
      if (select) {
        select.addEventListener("change", function () {
          syncDealStageFields(form);
        });
      }
      syncDealStageFields(form);
    });
  }

  function syncPipelineStageForm(form) {
    var pipeline = form.querySelector("[data-pipeline-select]");
    var stage = form.querySelector("[data-pipeline-stage-select]");
    if (!pipeline || !stage) return;
    var pipelineId = String(pipeline.value || "");
    var visible = [];
    Array.prototype.forEach.call(stage.options, function (option) {
      var active = String(option.dataset.pipeline || "") === pipelineId;
      option.hidden = !active;
      option.disabled = !active;
      if (active) visible.push(option);
    });
    if (!visible.some(function (option) { return option.selected; }) && visible.length) {
      visible[0].selected = true;
    }
    var stageKey = String(stage.value || "");
    Array.prototype.forEach.call(form.querySelectorAll("[data-pipeline-stage-fields]"), function (group) {
      var active = group.dataset.pipelineStageFields === pipelineId + ":" + stageKey;
      group.hidden = !active;
      Array.prototype.forEach.call(group.querySelectorAll("input, select, textarea"), function (field) {
        field.disabled = !active;
      });
    });
  }

  function initPipelineStageForms() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-pipeline-stage-form]"), function (form) {
      if (form.dataset.pipelineStageReady !== "true") {
        form.dataset.pipelineStageReady = "true";
        var pipeline = form.querySelector("[data-pipeline-select]");
        var stage = form.querySelector("[data-pipeline-stage-select]");
        if (pipeline) pipeline.addEventListener("change", function () { syncPipelineStageForm(form); });
        if (stage) stage.addEventListener("change", function () { syncPipelineStageForm(form); });
      }
      syncPipelineStageForm(form);
    });
  }

  function initDealChats() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-deal-chat-thread], [data-wa-thread]"), function (thread) {
      thread.scrollTop = thread.scrollHeight;
    });
  }

  function initWhatsAppUnread() {
    var badge = document.querySelector("[data-whatsapp-unread]");
    if (!badge || badge.dataset.unreadReady === "true") return;
    badge.dataset.unreadReady = "true";

    function render(count) {
      count = Math.max(0, Number(count) || 0);
      badge.hidden = count === 0;
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.setAttribute("aria-label", count + (count === 1 ? " mensagem não lida" : " mensagens não lidas"));
    }

    function refresh() {
      if (document.hidden) return;
      fetch(badge.dataset.url, { credentials: "same-origin", cache: "no-store" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (payload) { if (payload) render(payload.count); })
        .catch(function () {});
    }

    window.setInterval(refresh, 12000);
    document.addEventListener("visibilitychange", refresh);
    document.body.addEventListener("nucleo:whatsappChanged", refresh);
  }

  var confirmDialog = document.getElementById("confirm-dialog");
  var confirmMessage = document.getElementById("confirm-message");
  var confirmOk = confirmDialog && confirmDialog.querySelector("[data-confirm-ok]");
  var confirmCancel = confirmDialog && confirmDialog.querySelector("[data-confirm-cancel]");
  var pendingConfirm = null;

  function closeConfirm() {
    if (!confirmDialog) return;
    confirmDialog.hidden = true;
    pendingConfirm = null;
  }

  function openConfirm(message, onConfirm) {
    if (!confirmDialog || !confirmMessage || !confirmOk) {
      if (window.confirm(message)) onConfirm();
      return;
    }
    pendingConfirm = onConfirm;
    confirmMessage.textContent = message;
    confirmDialog.hidden = false;
    confirmOk.focus();
  }

  if (confirmOk) {
    confirmOk.addEventListener("click", function () {
      var action = pendingConfirm;
      closeConfirm();
      if (action) action();
    });
  }

  if (confirmCancel) {
    confirmCancel.addEventListener("click", closeConfirm);
  }

  if (confirmDialog) {
    confirmDialog.addEventListener("click", function (event) {
      if (event.target === confirmDialog) closeConfirm();
    });
    document.addEventListener("keydown", function (event) {
      if (!confirmDialog.hidden && event.key === "Escape") closeConfirm();
    });
  }

  document.body.addEventListener("htmx:confirm", function (event) {
    var question = event.detail && event.detail.question;
    if (!question) return;
    event.preventDefault();
    openConfirm(question, function () {
      event.detail.issueRequest(true);
    });
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var question = form && form.dataset ? form.dataset.confirm : "";
    if (!question || form.dataset.confirmed === "true") return;
    event.preventDefault();
    openConfirm(question, function () {
      form.dataset.confirmed = "true";
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
      setTimeout(function () { delete form.dataset.confirmed; }, 0);
    });
  });

  function initAutomationCanvas(root) {
    if (!root || root.dataset.canvasReady === "true") return;
    root.dataset.canvasReady = "true";

    var layout = root.closest(".automation-layout") || root;
    var canvas = root.querySelector("[data-node-canvas]");
    var stage = root.querySelector("[data-canvas-stage]");
    var world = root.querySelector("[data-canvas-world]");
    var links = root.querySelector("[data-node-links]");
    var entryTemplate = root.querySelector("#automation-entry-template");
    var actionTemplate = root.querySelector("#automation-action-template");
    var filterTemplate = root.querySelector("#automation-filter-template");
    var conditionTemplate = root.querySelector("#automation-condition-template");
    var switchTemplate = root.querySelector("#automation-switch-template");
    var mergeTemplate = root.querySelector("#automation-merge-template");
    var loopTemplate = root.querySelector("#automation-loop-template");
    var ruleTemplate = root.querySelector("#automation-rule-template");
    var canvasInput = root.querySelector("[data-canvas-input]");
    var edgeDelete = root.querySelector("[data-edge-delete]");
    var zoomLabel = root.querySelector("[data-zoom-label]");
    var palette = layout.querySelector("[data-node-palette]");
    if (!canvas || !stage || !world || !links || !entryTemplate || !actionTemplate || !filterTemplate || !conditionTemplate || !switchTemplate || !mergeTemplate || !loopTemplate || !ruleTemplate) return;
    var edges = [];
    var draftEdge = null;
    var selectedEdgeIndex = null;
    var zoom = 1;
    var minZoom = 0.55;
    var maxZoom = 1.75;
    var panX = 0;
    var panY = 0;
    var baseStageWidth = parseInt(getComputedStyle(stage).getPropertyValue("--stage-width"), 10) || 4200;
    var baseStageHeight = parseInt(getComputedStyle(stage).getPropertyValue("--stage-height"), 10) || 3600;
    var canvasOriginOffset = 1200;
    var actionMeta = {};
    var paletteDrag = null;
    var suppressPaletteClick = false;

    Array.prototype.forEach.call(layout.querySelectorAll("[data-add-action][data-node-tone]"), function (button) {
      var type = button.dataset.addAction;
      var icon = button.querySelector("span");
      if (!type || actionMeta[type]) return;
      actionMeta[type] = {
        icon: icon ? icon.innerHTML : "",
        tone: button.dataset.nodeTone || "",
        label: button.textContent.trim()
      };
    });

    var automationIconSelect = root.querySelector("[data-automation-icon-select]");
    var automationIconPreview = root.querySelector("[data-automation-icon-preview]");
    function syncAutomationIconPreview() {
      if (!automationIconSelect || !automationIconPreview) return;
      var template = document.querySelector('[data-icon-template="' + automationIconSelect.value + '"]');
      if (template) automationIconPreview.innerHTML = template.innerHTML;
    }
    if (automationIconSelect) {
      automationIconSelect.addEventListener("change", syncAutomationIconPreview);
      syncAutomationIconPreview();
    }

    function actionNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-action-node]")).sort(function (a, b) {
        return Number(a.dataset.actionIndex || 0) - Number(b.dataset.actionIndex || 0);
      });
    }

    function filterNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-filter-node]")).sort(function (a, b) {
        return Number(a.dataset.filterIndex || 0) - Number(b.dataset.filterIndex || 0);
      });
    }

    function conditionNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-condition-node]")).sort(function (a, b) {
        return Number(a.dataset.conditionIndex || 0) - Number(b.dataset.conditionIndex || 0);
      });
    }

    function switchNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-switch-node]")).sort(function (a, b) {
        return Number(a.dataset.switchIndex || 0) - Number(b.dataset.switchIndex || 0);
      });
    }

    function mergeNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-merge-node]")).sort(function (a, b) {
        return Number(a.dataset.mergeIndex || 0) - Number(b.dataset.mergeIndex || 0);
      });
    }

    function loopNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-loop-node]")).sort(function (a, b) {
        return Number(a.dataset.loopIndex || 0) - Number(b.dataset.loopIndex || 0);
      });
    }

    function ruleNodes() {
      return Array.prototype.slice.call(world.querySelectorAll("[data-rule-node]")).sort(function (a, b) {
        return Number(a.dataset.ruleIndex || 0) - Number(b.dataset.ruleIndex || 0);
      });
    }

    function nodeById(id) {
      return world.querySelector('[data-node-id="' + id + '"]');
    }

    function entryNode() {
      return nodeById("entry");
    }

    function triggerSelect() {
      var node = entryNode();
      return node ? node.querySelector("[data-trigger-select]") : null;
    }

    function currentTrigger() {
      var select = triggerSelect();
      return select ? select.value : "";
    }

    function kindFromTrigger() {
      return currentTrigger().indexOf("deal_") === 0 ? "deal" : "contact";
    }

    function webhookKey() {
      return "wh_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    }

    function webhookUrlFor(key) {
      return window.location.origin + "/webhooks/automation/" + key + "/";
    }

    function syncTriggerExtras() {
      var node = entryNode();
      if (!node) return;
      var trigger = currentTrigger();
      var icon = node.querySelector(".node-card__icon");
      var kicker = node.querySelector(".node-card__kicker");
      var title = node.querySelector(".node-card__drag b");
      if (trigger === "schedule_interval") {
        node.dataset.nodeTone = "time";
        if (kicker) kicker.textContent = "Tempo";
        if (title) title.textContent = "Schedule trigger";
      } else if (trigger === "webhook_received") {
        node.dataset.nodeTone = "webhook";
        if (kicker) kicker.textContent = "Entrada";
        if (title) title.textContent = "Webhook recebido";
      } else {
        node.dataset.nodeTone = "";
        if (kicker) kicker.textContent = "Entrada";
        if (title) title.textContent = "Quando acontecer";
      }
      if (icon) {
        var iconTemplateName = trigger === "schedule_interval" ? "clock" : trigger === "webhook_received" ? "command" : "bolt";
        var iconTemplate = document.querySelector('[data-icon-template="' + iconTemplateName + '"]');
        if (iconTemplate) icon.innerHTML = iconTemplate.innerHTML;
      }
      Array.prototype.forEach.call(node.querySelectorAll("[data-trigger-extra]"), function (block) {
        var active = block.dataset.triggerExtra === trigger;
        block.hidden = !active;
        Array.prototype.forEach.call(block.querySelectorAll("input, select, textarea"), function (field) {
          if (field.type !== "hidden") field.disabled = !active;
        });
      });
      if (trigger === "schedule_interval") {
        var amount = node.querySelector("[data-trigger-amount]");
        var unit = node.querySelector("[data-trigger-unit]");
        var interval = node.querySelector("[data-trigger-interval]");
        var multiplier = { minutes: 1, hours: 60, days: 1440 };
        var amountValue = Math.max(1, Number(amount && amount.value ? amount.value : 1));
        var unitValue = unit && unit.value ? unit.value : "hours";
        if (interval) interval.value = String(amountValue * (multiplier[unitValue] || 60));
      }
      if (trigger === "webhook_received") {
        var keyField = node.querySelector("[data-trigger-webhook-key]");
        var urlField = node.querySelector("[data-trigger-webhook-url]");
        if (keyField && !keyField.value) keyField.value = webhookKey();
        if (urlField && keyField) urlField.value = webhookUrlFor(keyField.value);
      }
    }

    function nextIndex(selector, dataKey) {
      var max = 0;
      Array.prototype.forEach.call(world.querySelectorAll(selector), function (node) {
        max = Math.max(max, Number(node.dataset[dataKey] || 0));
      });
      return max + 1;
    }

    function renderTemplate(template, index) {
      var holder = document.createElement("div");
      holder.innerHTML = template.innerHTML.split("__INDEX__").join(String(index || 1)).trim();
      return holder.firstElementChild;
    }

    function applyStageSize() {
      stage.style.setProperty("--stage-width", baseStageWidth + "px");
      stage.style.setProperty("--stage-height", baseStageHeight + "px");
      stage.style.setProperty("--canvas-zoom", String(zoom));
      stage.style.setProperty("--canvas-pan-x", panX + "px");
      stage.style.setProperty("--canvas-pan-y", panY + "px");
      stage.style.width = "100%";
      stage.style.height = "100%";
      stage.style.minWidth = "100%";
      stage.style.minHeight = "100%";
      if (zoomLabel) zoomLabel.textContent = Math.round(zoom * 100) + "%";
    }

    function setCanvasViewport(nextPanX, nextPanY) {
      panX = Number.isFinite(nextPanX) ? nextPanX : panX;
      panY = Number.isFinite(nextPanY) ? nextPanY : panY;
      applyStageSize();
      drawLinks();
    }

    function openPalette() {
      if (!palette) return;
      palette.hidden = false;
      palette.classList.add("is-focused");
      setTimeout(function () { palette.classList.remove("is-focused"); }, 700);
    }

    function closePalette() {
      if (palette && !palette.hasAttribute("data-side-palette")) palette.hidden = true;
    }

    function defaultPosition(x, y) {
      return {
        x: Math.max(24, (-panX / zoom) + x),
        y: Math.max(24, (-panY / zoom) + y)
      };
    }

    function focusNode(node) {
      if (!node) return;
      setCanvasViewport(
        120 - node.offsetLeft * zoom,
        140 - node.offsetTop * zoom
      );
      node.classList.add("is-focused");
      setTimeout(function () {
        node.classList.remove("is-focused");
      }, 650);
    }

    function hideEdgeDelete() {
      selectedEdgeIndex = null;
      if (edgeDelete) edgeDelete.hidden = true;
    }

    function showEdgeDelete() {
      if (!edgeDelete || selectedEdgeIndex === null || selectedEdgeIndex < 0 || !edges[selectedEdgeIndex]) {
        if (edgeDelete) edgeDelete.hidden = true;
        return;
      }
      var edge = edges[selectedEdgeIndex];
      var fromNode = nodeById(edge.from);
      var toNode = nodeById(edge.to);
      if (!fromNode || !toNode) {
        edgeDelete.hidden = true;
        return;
      }
      var from = portPoint(fromNode, "out");
      var to = portPoint(toNode, "in", edgeInputIndex(edge, selectedEdgeIndex));
      edgeDelete.style.left = Math.max(24, (from.x + to.x) / 2 - 56) + "px";
      edgeDelete.style.top = Math.max(24, (from.y + to.y) / 2 - 42) + "px";
      edgeDelete.hidden = false;
    }

    function removeSelectedEdge() {
      if (selectedEdgeIndex === null || selectedEdgeIndex < 0 || !edges[selectedEdgeIndex]) return;
      edges.splice(selectedEdgeIndex, 1);
      hideEdgeDelete();
      drawLinks();
      syncCanvasInput();
    }

    function syncCondition() {
      Array.prototype.forEach.call(world.querySelectorAll("[data-filter-node], [data-condition-node], [data-switch-node]"), function (node) {
        var kind = node.dataset.conditionKind || kindFromTrigger();
        var title = node.querySelector("[data-filter-title]");
        if (title) {
          if (node.dataset.nodeKind === "filter") title.textContent = kind === "deal" ? "Filtro de negocio" : "Filtro de contato";
          else if (node.dataset.nodeKind === "switch") title.textContent = kind === "deal" ? "Roteador negocio" : "Roteador contato";
          else title.textContent = kind === "deal" ? "Se negocio" : "Se contato";
        }
        Array.prototype.forEach.call(node.querySelectorAll("[data-condition-for]"), function (block) {
          var active = block.dataset.conditionFor === kind;
          block.hidden = !active;
          Array.prototype.forEach.call(block.querySelectorAll("select, input"), function (field) {
            field.disabled = !active;
          });
        });
      });
      drawLinks();
    }

    function ensureEntry(value, position) {
      var existing = entryNode();
      if (existing) {
        var existingSelect = existing.querySelector("[data-trigger-select]");
        if (value && existingSelect) existingSelect.value = value;
        if (position) {
          ensureStageWidth(position.x);
          ensureStageHeight(position.y);
          setNodePosition(existing, position.x, position.y);
          drawLinks();
        }
        syncTriggerExtras();
        syncCondition();
        return existing;
      }
      var node = renderTemplate(entryTemplate, 1);
      var fallback = defaultPosition(140, 220);
      var x = position ? position.x : fallback.x;
      var y = position ? position.y : fallback.y;
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      var select = node.querySelector("[data-trigger-select]");
      if (select && value) select.value = value;
      syncTriggerExtras();
      syncCondition();
      syncCanvasInput();
      focusNode(node);
      return node;
    }

    function setTrigger(value, options) {
      if (!value) return;
      options = options || {};
      var node = ensureEntry(value, options.position);
      var select = node.querySelector("[data-trigger-select]");
      if (select) select.value = value;
      syncTriggerExtras();
      syncCondition();
      focusNode(node);
      syncCanvasInput();
    }

    function tailNode() {
      var nodes = Array.prototype.slice.call(world.querySelectorAll("[data-flow-node]:not([data-entry-node])"));
      nodes.sort(function (a, b) {
        return a.offsetLeft - b.offsetLeft || a.offsetTop - b.offsetTop;
      });
      return nodes[nodes.length - 1] || entryNode();
    }

    function syncActionNode(node) {
      var typeField = node.querySelector("[data-action-type]");
      if (!typeField) return;
      var type = typeField.value;
      var meta = actionMeta[type] || {};
      node.dataset.nodeSubtype = type || "";
      node.dataset.nodeTone = meta.tone || "";
      var label = meta.label || type || "Executar";
      var title = node.querySelector("[data-action-title]");
      if (title) title.textContent = label;
      var labelField = node.querySelector("[data-action-type-label]");
      if (labelField) labelField.textContent = label;
      var icon = node.querySelector("[data-action-icon]");
      if (icon) icon.innerHTML = meta.icon || "";
      Array.prototype.forEach.call(node.querySelectorAll("[data-action-fieldset]"), function (fieldset) {
        var allowedSet = (fieldset.dataset.showFor || "").split(",").indexOf(type) !== -1;
        fieldset.hidden = !allowedSet;
      });
      Array.prototype.forEach.call(node.querySelectorAll("[data-action-field]"), function (field) {
        var allowed = (field.dataset.showFor || "").split(",").indexOf(type) !== -1;
        field.hidden = !allowed;
        field.disabled = !allowed;
      });
      syncHttpNode(node);
    }

    function syncHttpNode(node) {
      var typeField = node.querySelector("[data-action-type]");
      var isHttp = typeField && typeField.value === "http_request";
      var authType = node.querySelector('[name$="_http_auth_type"]');
      var auth = isHttp && authType ? authType.value : "none";
      Array.prototype.forEach.call(node.querySelectorAll("[data-http-auth-block]"), function (block) {
        block.hidden = block.dataset.httpAuthBlock !== auth;
      });
      Array.prototype.forEach.call(node.querySelectorAll("[data-http-auth-field]"), function (field) {
        var active = isHttp && field.dataset.httpAuthField === auth;
        field.hidden = !active;
        field.disabled = !active;
      });
      Array.prototype.forEach.call(node.querySelectorAll("[data-http-block]"), function (block) {
        var key = block.dataset.httpBlock;
        var toggle = node.querySelector('[data-http-toggle="' + key + '"]');
        var active = isHttp && toggle && toggle.checked;
        block.hidden = !active;
        Array.prototype.forEach.call(block.querySelectorAll("[data-http-optional-field]"), function (field) {
          field.hidden = !active;
          field.disabled = !active;
        });
      });
    }

    function queryJsonFromNode(node) {
      var data = {};
      Array.prototype.forEach.call(node.querySelectorAll("[data-http-query-name]"), function (nameField) {
        var index = nameField.dataset.httpQueryName;
        var valueField = node.querySelector('[data-http-query-value="' + index + '"]');
        if (nameField.disabled || !valueField || valueField.disabled) return;
        var key = nameField.value.trim();
        if (!key) return;
        data[key] = valueField.value;
      });
      var raw = Object.keys(data).length ? JSON.stringify(data) : "";
      var hidden = node.querySelector("[data-http-query-json]");
      if (hidden) hidden.value = raw;
      return raw;
    }

    function setQueryRows(node, raw) {
      var data = {};
      if (raw) {
        try {
          data = JSON.parse(raw);
        } catch (error) {
          data = {};
        }
      }
      var pairs = Object.keys(data).map(function (key) {
        return { name: key, value: data[key] };
      });
      Array.prototype.forEach.call(node.querySelectorAll("[data-http-query-name]"), function (nameField, index) {
        var pair = pairs[index] || { name: "", value: "" };
        var valueField = node.querySelector('[data-http-query-value="' + nameField.dataset.httpQueryName + '"]');
        nameField.value = pair.name;
        if (valueField) valueField.value = pair.value;
      });
      var hidden = node.querySelector("[data-http-query-json]");
      if (hidden) hidden.value = raw || "";
    }

    function initActionNode(node) {
      if (!node || node.dataset.actionReady === "true") return;
      node.dataset.actionReady = "true";
      var select = node.querySelector("[data-action-type]");
      if (select) {
        select.addEventListener("change", function () {
          syncActionNode(node);
        });
      }
      syncActionNode(node);
    }

    function syncRuleNode(node) {
      var select = node.querySelector("[data-rule-type]");
      if (!select) return;
      var type = select.value || "once_per_record";
      var selected = select.options[select.selectedIndex];
      var title = node.querySelector("[data-rule-title]");
      var days = node.querySelector("[data-rule-days]");
      node.dataset.ruleSubtype = type;
      if (title) title.textContent = selected ? selected.textContent : "Controlar entrada";
      if (days) {
        var needsDays = type === "cooldown_days";
        days.hidden = !needsDays;
        days.disabled = !needsDays;
      }
    }

    function initRuleNode(node) {
      if (!node || node.dataset.ruleReady === "true") return;
      node.dataset.ruleReady = "true";
      var select = node.querySelector("[data-rule-type]");
      if (select) {
        select.addEventListener("change", function () {
          syncRuleNode(node);
        });
      }
      syncRuleNode(node);
    }

    function updateRemoveButtons() {
      var nodes = actionNodes();
      Array.prototype.forEach.call(nodes, function (node) {
        var remove = node.querySelector("[data-remove-action]");
        if (remove) remove.hidden = false;
      });
    }

    function renumberActions() {
      var idMap = {};
      actionNodes().forEach(function (node, index) {
        var oldId = node.dataset.nodeId;
        var next = index + 1;
        node.dataset.actionIndex = String(next);
        node.dataset.nodeId = "action-" + next;
        idMap[oldId] = node.dataset.nodeId;
        var step = node.querySelector(".node-step");
        var order = node.querySelector("[data-action-order]");
        if (order) order.textContent = String(next);
        else if (step) step.textContent = String(next);
        Array.prototype.forEach.call(node.querySelectorAll("[name]"), function (field) {
          field.name = field.name.replace(/^action\d+_/, "action" + next + "_");
        });
      });
      edges = edges.map(function (edge) {
        return { from: idMap[edge.from] || edge.from, to: idMap[edge.to] || edge.to };
      });
      updateRemoveButtons();
      drawLinks();
    }

    function setNodePosition(node, x, y) {
      var maxX = Math.max(24, baseStageWidth - node.offsetWidth - 24);
      var maxY = Math.max(24, baseStageHeight - node.offsetHeight - 24);
      node.style.left = Math.max(24, Math.min(maxX, x)) + "px";
      node.style.top = Math.max(24, Math.min(maxY, y)) + "px";
    }

    function ensureStageWidth(x) {
      if (x + 760 > baseStageWidth) {
        baseStageWidth = x + 1100;
        applyStageSize();
      }
    }

    function ensureStageHeight(y) {
      if (y + 520 > baseStageHeight) {
        baseStageHeight = y + 900;
        applyStageSize();
      }
    }

    function insertNodeAfter(fromId, newId) {
      if (!fromId || !newId || fromId === newId) return;
      var outgoing = edges.filter(function (edge) {
        return edge.from === fromId && edge.to !== newId;
      });
      edges = edges.filter(function (edge) {
        return edge.from !== fromId && !(edge.from === newId && edge.to === fromId);
      });
      edges.push({ from: fromId, to: newId });
      outgoing.forEach(function (edge) {
        if (edge.to !== newId) edges.push({ from: newId, to: edge.to });
      });
      hideEdgeDelete();
      drawLinks();
      syncCanvasInput();
    }

    function addFilter(kind, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var previous = connect ? (tailNode() || entry) : null;
      var index = nextIndex("[data-filter-node]", "filterIndex");
      var node = renderTemplate(filterTemplate, index);
      node.dataset.conditionKind = kind || kindFromTrigger();
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      syncCondition();
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      else syncCanvasInput();
      focusNode(node);
    }

    function addCondition(kind, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var previous = connect ? (tailNode() || entry) : null;
      var index = nextIndex("[data-condition-node]", "conditionIndex");
      var node = renderTemplate(conditionTemplate, index);
      node.dataset.conditionKind = kind || kindFromTrigger();
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      syncCondition();
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      else syncCanvasInput();
      focusNode(node);
    }

    function addSwitch(kind, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var previous = connect ? (tailNode() || entry) : null;
      var index = nextIndex("[data-switch-node]", "switchIndex");
      var node = renderTemplate(switchTemplate, index);
      node.dataset.conditionKind = kind || kindFromTrigger();
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      syncCondition();
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      else syncCanvasInput();
      focusNode(node);
    }

    function addMerge(mode, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var previous = connect ? (tailNode() || entry) : null;
      var index = nextIndex("[data-merge-node]", "mergeIndex");
      var node = renderTemplate(mergeTemplate, index);
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      var select = node.querySelector("[data-merge-mode]");
      if (select) select.value = mode || "wait_all";
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      else syncCanvasInput();
      focusNode(node);
    }

    function addLoop(mode, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var previous = connect ? (tailNode() || entry) : null;
      var index = nextIndex("[data-loop-node]", "loopIndex");
      var node = renderTemplate(loopTemplate, index);
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      var select = node.querySelector("[data-loop-mode]");
      if (select) select.value = mode || "for_each";
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      else syncCanvasInput();
      focusNode(node);
    }

    function addRule(type, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var previous = connect ? (tailNode() || entry) : null;
      var index = nextIndex("[data-rule-node]", "ruleIndex");
      var node = renderTemplate(ruleTemplate, index);
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      var select = node.querySelector("[data-rule-type]");
      if (select) select.value = type || "once_per_record";
      initRuleNode(node);
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      else syncCanvasInput();
      focusNode(node);
    }

    function addAction(type, options) {
      options = options || {};
      var connect = options.connect !== false;
      var entry = connect ? ensureEntry() : entryNode();
      var current = actionNodes();
      var index = current.length + 1;
      var node = renderTemplate(actionTemplate, index);
      var previous = connect ? (tailNode() || entry) : null;
      var fallback = defaultPosition(520, 220);
      var x = options.position ? options.position.x : (previous ? previous.offsetLeft + 410 : fallback.x);
      var y = options.position ? options.position.y : (previous ? previous.offsetTop : fallback.y);
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      var select = node.querySelector("[data-action-type]");
      if (select) select.value = type || "create_task";
      initActionNode(node);
      if (connect && previous) insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      updateRemoveButtons();
      drawLinks();
      syncCanvasInput();
      setCanvasViewport(120 - x * zoom, 140 - y * zoom);
    }

    function removeAndReconnect(node, reconnect) {
      if (!node) return;
      var nodeId = node.dataset.nodeId;
      var incoming = edges.filter(function (edge) { return edge.to === nodeId; });
      var outgoing = edges.filter(function (edge) { return edge.from === nodeId; });
      hideEdgeDelete();
      edges = edges.filter(function (edge) {
        return edge.from !== nodeId && edge.to !== nodeId;
      });
      if (reconnect) {
        incoming.forEach(function (sourceEdge) {
          outgoing.forEach(function (targetEdge) {
            if (sourceEdge.from !== targetEdge.to) {
              edges.push({ from: sourceEdge.from, to: targetEdge.to });
            }
          });
        });
      }
      node.remove();
      drawLinks();
      syncCanvasInput();
    }

    function removeAction(node) {
      removeAndReconnect(node, true);
      renumberActions();
      syncCanvasInput();
    }

    function connectNodes(fromId, toId, branch) {
      if (!fromId || !toId || fromId === toId || !nodeById(fromId) || !nodeById(toId)) return;
      branch = branch === "false" ? "false" : "";
      edges = edges.filter(function (edge) {
        return !(edge.from === fromId && edge.to === toId && (edge.branch || "") === branch);
      });
      var newEdge = { from: fromId, to: toId };
      if (branch) newEdge.branch = branch;
      edges.push(newEdge);
      hideEdgeDelete();
      drawLinks();
      syncCanvasInput();
    }

    function edgeInputIndex(edge, edgeIndex) {
      if (!edge || !edge.to) return 0;
      var incoming = edges.filter(function (item) {
        return item.to === edge.to;
      });
      var localIndex = incoming.indexOf(edge);
      if (localIndex === -1) {
        localIndex = incoming.findIndex(function (item) {
          return item.from === edge.from && item.to === edge.to;
        });
      }
      if (localIndex === -1) localIndex = Math.max(0, edgeIndex || 0);
      return localIndex % 3;
    }

    function portPoint(node, side, inputIndex) {
      var port = null;
      if (side === "in" && node.dataset.nodeKind === "merge") {
        port = node.querySelector('[data-merge-input-port="' + ((Number(inputIndex || 0) % 3) + 1) + '"]');
      }
      if (!port) port = node.querySelector('[data-port="' + side + '"]');
      if (port) {
        return {
          x: node.offsetLeft + port.offsetLeft + port.offsetWidth / 2,
          y: node.offsetTop + port.offsetTop
        };
      }
      return {
        x: node.offsetLeft + (side === "out" ? node.offsetWidth : 0),
        y: node.offsetTop + node.offsetHeight / 2
      };
    }

    function addPath(fromNode, toNode, className, inputIndex, branch) {
      if (!fromNode || !toNode) return;
      var from = portPoint(fromNode, branch === "false" ? "out-false" : "out");
      var to = portPoint(toNode, "in", inputIndex);
      var dx = Math.max(70, Math.abs(to.x - from.x) * 0.42);
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", className || "node-link");
      path.setAttribute("d", "M " + from.x + " " + from.y + " C " + (from.x + dx) + " " + from.y + ", " + (to.x - dx) + " " + to.y + ", " + to.x + " " + to.y);
      links.appendChild(path);
      return path;
    }

    function addDraftPath() {
      if (!draftEdge) return;
      var fromNode = nodeById(draftEdge.from);
      if (!fromNode) return;
      var from = portPoint(fromNode, draftEdge.branch === "false" ? "out-false" : "out");
      var to = draftEdge.to;
      var dx = Math.max(70, Math.abs(to.x - from.x) * 0.42);
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "node-link node-link--draft");
      path.setAttribute("d", "M " + from.x + " " + from.y + " C " + (from.x + dx) + " " + from.y + ", " + (to.x - dx) + " " + to.y + ", " + to.x + " " + to.y);
      links.appendChild(path);
    }

    function drawLinks() {
      links.setAttribute("viewBox", "0 0 " + baseStageWidth + " " + baseStageHeight);
      links.innerHTML = "";
      if (selectedEdgeIndex !== null && (selectedEdgeIndex < 0 || selectedEdgeIndex >= edges.length)) {
        hideEdgeDelete();
      }
      edges.forEach(function (edge, index) {
        var from = nodeById(edge.from);
        var to = nodeById(edge.to);
        var path = addPath(from, to, index === selectedEdgeIndex ? "node-link is-selected" : "node-link", edgeInputIndex(edge, index), edge.branch);
        if (path) path.dataset.edgeIndex = String(index);
      });
      addDraftPath();
      showEdgeDelete();
    }

    function stagePoint(event) {
      var rect = canvas.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left - panX) / zoom,
        y: (event.clientY - rect.top - panY) / zoom
      };
    }

    function dropPosition(event) {
      var point = stagePoint(event);
      return {
        x: Math.max(24, point.x - 150),
        y: Math.max(24, point.y - 46)
      };
    }

    function isOverCanvas(event) {
      var element = document.elementFromPoint(event.clientX, event.clientY);
      return !!(element && canvas.contains(element));
    }

    function movePaletteGhost(event) {
      if (!paletteDrag || !paletteDrag.ghost) return;
      paletteDrag.ghost.style.transform = "translate(" + (event.clientX + 12) + "px, " + (event.clientY + 12) + "px)";
      canvas.classList.toggle("is-drop-target", isOverCanvas(event));
    }

    function startPaletteDrag(button, event) {
      var payload = palettePayload(button);
      if (!payload) return;
      paletteDrag = {
        pointerId: event.pointerId,
        source: button,
        payload: payload,
        startX: event.clientX,
        startY: event.clientY,
        moved: false,
        ghost: null
      };
      button.setPointerCapture(event.pointerId);
    }

    function ensurePaletteGhost(event) {
      if (!paletteDrag || paletteDrag.ghost) return;
      var ghost = paletteDrag.source.cloneNode(true);
      ghost.className = "palette-drag-ghost";
      ghost.removeAttribute("id");
      ghost.removeAttribute("type");
      document.body.appendChild(ghost);
      paletteDrag.ghost = ghost;
      paletteDrag.source.classList.add("is-palette-dragging");
      movePaletteGhost(event);
    }

    function clearPaletteDrag() {
      if (!paletteDrag) return;
      if (paletteDrag.ghost) paletteDrag.ghost.remove();
      if (paletteDrag.source) paletteDrag.source.classList.remove("is-palette-dragging");
      canvas.classList.remove("is-drop-target");
      paletteDrag = null;
    }

    function palettePayload(button) {
      if (!button) return null;
      if (button.hasAttribute("data-add-trigger")) return { kind: "trigger", value: button.dataset.addTrigger };
      if (button.hasAttribute("data-add-filter")) return { kind: "filter", value: button.dataset.addFilter };
      if (button.hasAttribute("data-add-condition")) return { kind: "condition", value: button.dataset.addCondition };
      if (button.hasAttribute("data-add-switch")) return { kind: "switch", value: button.dataset.addSwitch };
      if (button.hasAttribute("data-add-merge")) return { kind: "merge", value: button.dataset.addMerge };
      if (button.hasAttribute("data-add-loop")) return { kind: "loop", value: button.dataset.addLoop };
      if (button.hasAttribute("data-add-rule")) return { kind: "rule", value: button.dataset.addRule };
      if (button.hasAttribute("data-add-action")) return { kind: "action", value: button.dataset.addAction };
      return null;
    }

    function addPaletteNode(payload, options) {
      if (!payload) return;
      options = options || {};
      if (payload.kind === "trigger") setTrigger(payload.value, options);
      else if (payload.kind === "filter") addFilter(payload.value, options);
      else if (payload.kind === "condition") addCondition(payload.value, options);
      else if (payload.kind === "switch") addSwitch(payload.value, options);
      else if (payload.kind === "merge") addMerge(payload.value || "wait_all", options);
      else if (payload.kind === "loop") addLoop(payload.value || "for_each", options);
      else if (payload.kind === "rule") addRule(payload.value || "once_per_record", options);
      else if (payload.kind === "action") addAction(payload.value || "create_task", options);
    }

    function activeField(node, selector) {
      var fallback = null;
      var fields = node.querySelectorAll(selector);
      for (var i = 0; i < fields.length; i += 1) {
        if (!fallback) fallback = fields[i];
        if (!fields[i].disabled) return fields[i];
      }
      return fallback;
    }

    function conditionRules(node) {
      return Array.prototype.slice.call(node.querySelectorAll("[data-rules] [data-rule]")).map(function (row) {
        var f = row.querySelector("[data-rule-field]");
        var o = row.querySelector("[data-rule-op]");
        var v = row.querySelector("[data-rule-value]");
        return { field: f ? f.value : "", op: o ? o.value : "eq", value: v ? v.value : "" };
      }).filter(function (r) { return r.field; });
    }

    function conditionFromNode(node) {
      var match = node.querySelector("[data-rule-match]");
      return { match: match ? match.value : "all", rules: conditionRules(node) };
    }

    function hydrateCondition(node, data) {
      var container = node.querySelector("[data-rules]");
      if (!container) return;
      var template = container.querySelector("[data-rule]");
      if (!template) return;
      var rules = (data && data.condition && data.condition.rules) ? data.condition.rules.slice() : [];
      if (!rules.length) {
        // Backward compatibility with the old single-field condition model.
        if (data && data.condition_stage) rules.push({ field: "stage", op: "eq", value: data.condition_stage });
        if (data && data.condition_custom_key) rules.push({ field: "custom:" + data.condition_custom_key, op: "eq", value: data.condition_custom_value || "" });
      }
      if (!rules.length) return; // keep the default empty row
      var blank = template.cloneNode(true);
      container.innerHTML = "";
      rules.forEach(function (r) {
        var row = blank.cloneNode(true);
        setValue(row, "[data-rule-field]", r.field);
        setValue(row, "[data-rule-op]", r.op || "eq");
        setValue(row, "[data-rule-value]", r.value || "");
        container.appendChild(row);
      });
      var match = node.querySelector("[data-rule-match]");
      if (match && data && data.condition && data.condition.match) match.value = data.condition.match;
    }

    world.addEventListener("click", function (event) {
      var addBtn = event.target.closest("[data-add-rule]");
      if (addBtn) {
        event.preventDefault();
        event.stopPropagation();
        var addContainer = addBtn.closest("[data-cond-builder]").querySelector("[data-rules]");
        var rows = addContainer.querySelectorAll("[data-rule]");
        var clone = rows[rows.length - 1].cloneNode(true);
        var valueInput = clone.querySelector("[data-rule-value]");
        if (valueInput) valueInput.value = "";
        addContainer.appendChild(clone);
        return;
      }
      var rmBtn = event.target.closest("[data-remove-rule]");
      if (rmBtn) {
        event.preventDefault();
        event.stopPropagation();
        var rmContainer = rmBtn.closest("[data-rules]");
        if (rmContainer && rmContainer.querySelectorAll("[data-rule]").length > 1) {
          rmBtn.closest("[data-rule]").remove();
        }
        return;
      }
    });

    function nodeData(node) {
      if (node.dataset.nodeId === "entry") {
        var select = node.querySelector("[data-trigger-select]");
        var amount = node.querySelector("[data-trigger-amount]");
        var unit = node.querySelector("[data-trigger-unit]");
        var interval = node.querySelector("[data-trigger-interval]");
        var webhook = node.querySelector("[data-trigger-webhook-key]");
        var fbForm = node.querySelector("[data-trigger-form]");
        return {
          trigger: select ? select.value : "",
          trigger_interval_amount: amount && !amount.disabled ? amount.value : "1",
          trigger_interval_unit: unit && !unit.disabled ? unit.value : "hours",
          trigger_interval_minutes: interval ? interval.value : "60",
          webhook_key: webhook ? webhook.value : "",
          trigger_form_id: fbForm && !fbForm.disabled ? fbForm.value : ""
        };
      }
      if (node.dataset.nodeKind === "filter" || node.dataset.nodeKind === "condition" || node.dataset.nodeKind === "switch") {
        return { condition: conditionFromNode(node) };
      }
      if (node.dataset.nodeKind === "merge") {
        var mergeMode = node.querySelector("[data-merge-mode]");
        return {
          merge_mode: mergeMode ? mergeMode.value : "wait_all"
        };
      }
      if (node.dataset.nodeKind === "loop") {
        var loopMode = node.querySelector("[data-loop-mode]");
        var loopCount = node.querySelector("[data-loop-count]");
        return {
          loop_mode: loopMode ? loopMode.value : "for_each",
          loop_count: loopCount ? loopCount.value : "1"
        };
      }
      if (node.dataset.nodeKind === "rule") {
        var ruleType = node.querySelector("[data-rule-type]");
        var ruleDays = node.querySelector("[data-rule-days]");
        return {
          rule_type: ruleType ? ruleType.value : "once_per_record",
          rule_days: ruleDays ? ruleDays.value : "0"
        };
      }
      var actionType = node.querySelector("[data-action-type]");
      var text = activeField(node, '[name$="_text"]');
      var pipeline = activeField(node, '[name$="_pipeline"]');
      var dealStage = activeField(node, '[name$="_deal_stage"]');
      var contactStage = activeField(node, '[name$="_contact_stage"]');
      var contactFirstName = activeField(node, '[name$="_contact_first_name"]');
      var contactLastName = activeField(node, '[name$="_contact_last_name"]');
      var contactEmail = activeField(node, '[name$="_contact_email"]');
      var contactPhone = activeField(node, '[name$="_contact_phone"]');
      var contactJobTitle = activeField(node, '[name$="_contact_job_title"]');
      var companyName = activeField(node, '[name$="_company_name"]');
      var companyDomain = activeField(node, '[name$="_company_domain"]');
      var companyIndustry = activeField(node, '[name$="_company_industry"]');
      var companyCity = activeField(node, '[name$="_company_city"]');
      var dueDays = activeField(node, '[name$="_due_days"]');
      var customActionKey = activeField(node, '[name$="_custom_key"]');
      var customActionValue = activeField(node, '[name$="_custom_value"]');
      var delayAmount = activeField(node, '[name$="_delay_amount"]');
      var delayUnit = activeField(node, '[name$="_delay_unit"]');
      var delayUntil = activeField(node, '[name$="_delay_until"]');
      var delayMinutes = activeField(node, '[name$="_delay_minutes"]');
      var webhookUrl = activeField(node, '[name$="_webhook_url"]');
      var httpMethod = activeField(node, '[name$="_http_method"]');
      var httpUrl = activeField(node, '[name$="_http_url"]');
      var httpAuthType = activeField(node, '[name$="_http_auth_type"]');
      var httpAuthToken = activeField(node, '[name$="_http_auth_token"]');
      var httpUsername = activeField(node, '[name$="_http_username"]');
      var httpPassword = activeField(node, '[name$="_http_password"]');
      var httpSendQuery = activeField(node, '[name$="_http_send_query"]');
      var httpQuery = activeField(node, '[name$="_http_query"]');
      var httpSendHeaders = activeField(node, '[name$="_http_send_headers"]');
      var httpHeaders = activeField(node, '[name$="_http_headers"]');
      var httpContentType = activeField(node, '[name$="_http_content_type"]');
      var httpSendBody = activeField(node, '[name$="_http_send_body"]');
      var httpBody = activeField(node, '[name$="_http_body"]');
      var httpTimeout = activeField(node, '[name$="_http_timeout"]');
      var amountValue = delayAmount && !delayAmount.disabled ? delayAmount.value : "0";
      var unitValue = delayUnit && !delayUnit.disabled ? delayUnit.value : "minutes";
      var delayMultipliers = { minutes: 1, hours: 60, days: 1440 };
      var computedDelayMinutes = String((Number(amountValue) || 0) * (delayMultipliers[unitValue] || 1));
      return {
        action_index: Number(node.dataset.actionIndex || 0),
        action_type: actionType ? actionType.value : "",
        text: text && !text.disabled ? text.value : "",
        pipeline: pipeline && !pipeline.disabled ? pipeline.value : "",
        deal_stage: dealStage && !dealStage.disabled ? dealStage.value : "",
        contact_stage: contactStage && !contactStage.disabled ? contactStage.value : "",
        contact_first_name: contactFirstName && !contactFirstName.disabled ? contactFirstName.value : "",
        contact_last_name: contactLastName && !contactLastName.disabled ? contactLastName.value : "",
        contact_email: contactEmail && !contactEmail.disabled ? contactEmail.value : "",
        contact_phone: contactPhone && !contactPhone.disabled ? contactPhone.value : "",
        contact_job_title: contactJobTitle && !contactJobTitle.disabled ? contactJobTitle.value : "",
        company_name: companyName && !companyName.disabled ? companyName.value : "",
        company_domain: companyDomain && !companyDomain.disabled ? companyDomain.value : "",
        company_industry: companyIndustry && !companyIndustry.disabled ? companyIndustry.value : "",
        company_city: companyCity && !companyCity.disabled ? companyCity.value : "",
        due_days: dueDays && !dueDays.disabled ? dueDays.value : "0",
        custom_key: customActionKey && !customActionKey.disabled ? customActionKey.value : "",
        custom_value: customActionValue && !customActionValue.disabled ? customActionValue.value : "",
        delay_amount: amountValue,
        delay_unit: unitValue,
        delay_until: delayUntil && !delayUntil.disabled ? delayUntil.value : "",
        delay_minutes: delayMinutes && !delayMinutes.disabled ? computedDelayMinutes : amountValue,
        webhook_url: webhookUrl && !webhookUrl.disabled ? webhookUrl.value : "",
        http_method: httpMethod && !httpMethod.disabled ? httpMethod.value : "POST",
        http_url: httpUrl && !httpUrl.disabled ? httpUrl.value : "",
        http_auth_type: httpAuthType && !httpAuthType.disabled ? httpAuthType.value : "none",
        http_auth_token: httpAuthToken && !httpAuthToken.disabled ? httpAuthToken.value : "",
        http_username: httpUsername && !httpUsername.disabled ? httpUsername.value : "",
        http_password: httpPassword && !httpPassword.disabled ? httpPassword.value : "",
        http_send_query: httpSendQuery && !httpSendQuery.disabled && httpSendQuery.checked ? "1" : "",
        http_query: httpSendQuery && !httpSendQuery.disabled && httpSendQuery.checked ? queryJsonFromNode(node) : "",
        http_send_headers: httpSendHeaders && !httpSendHeaders.disabled && httpSendHeaders.checked ? "1" : "",
        http_headers: httpHeaders && !httpHeaders.disabled ? httpHeaders.value : "",
        http_content_type: httpContentType && !httpContentType.disabled ? httpContentType.value : "json",
        http_send_body: httpSendBody && !httpSendBody.disabled && httpSendBody.checked ? "1" : "",
        http_body: httpBody && !httpBody.disabled ? httpBody.value : "",
        http_timeout: httpTimeout && !httpTimeout.disabled ? httpTimeout.value : "10"
      };
    }

    function syncCanvasInput() {
      if (!canvasInput) return;
      var nodes = Array.prototype.map.call(world.querySelectorAll("[data-flow-node]"), function (node) {
        var type = node.dataset.nodeKind || (node.dataset.nodeId === "entry" ? "trigger" : node.dataset.nodeId === "condition" ? "condition" : "action");
        return {
          id: node.dataset.nodeId,
          type: type,
          x: node.offsetLeft,
          y: node.offsetTop,
          data: nodeData(node)
        };
      });
      canvasInput.value = JSON.stringify({
        version: 1,
        nodes: nodes,
        edges: edges,
        viewport: {
          scroll_left: Math.max(0, Math.round(-panX / zoom)),
          scroll_top: Math.max(0, Math.round(-panY / zoom)),
          zoom: zoom
        }
      });
    }

    function setZoom(nextZoom, anchorEvent) {
      var previousZoom = zoom;
      var canvasRect = canvas.getBoundingClientRect();
      var anchor = anchorEvent ? stagePoint(anchorEvent) : {
        x: ((canvas.clientWidth / 2) - panX) / zoom,
        y: ((canvas.clientHeight / 2) - panY) / zoom
      };
      zoom = Math.max(minZoom, Math.min(maxZoom, Math.round(nextZoom * 20) / 20));
      if (zoom === previousZoom) return;
      applyStageSize();
      panX = (anchorEvent ? anchorEvent.clientX - canvasRect.left : canvas.clientWidth / 2) - anchor.x * zoom;
      panY = (anchorEvent ? anchorEvent.clientY - canvasRect.top : canvas.clientHeight / 2) - anchor.y * zoom;
      applyStageSize();
      drawLinks();
      syncCanvasInput();
    }

    function indexFromId(id, fallback) {
      var match = String(id || "").match(/-(\d+)$/);
      return match ? Math.max(1, Number(match[1])) : fallback;
    }

    function setValue(node, selector, value) {
      var field = node.querySelector(selector);
      if (field && value !== undefined && value !== null) field.value = value;
    }

    function setChecked(node, selector, value) {
      var field = node.querySelector(selector);
      if (field) field.checked = String(value).toLowerCase() in { "1": true, "true": true, "on": true, "yes": true };
    }

    function setNodeFromCanvas(node, item) {
      node.dataset.nodeId = item.id;
      setNodePosition(node, item.x || 120, item.y || 180);
      if (item.type === "trigger") {
        setValue(node, "[data-trigger-select]", item.data ? item.data.trigger : "");
        setValue(node, "[data-trigger-amount]", item.data ? (item.data.trigger_interval_amount || item.data.trigger_interval_minutes || "1") : "1");
        setValue(node, "[data-trigger-unit]", item.data ? (item.data.trigger_interval_unit || "hours") : "hours");
        setValue(node, "[data-trigger-interval]", item.data ? (item.data.trigger_interval_minutes || "60") : "60");
        setValue(node, "[data-trigger-webhook-key]", item.data ? item.data.webhook_key : "");
        setValue(node, "[data-trigger-form]", item.data ? (item.data.trigger_form_id || "") : "");
        syncTriggerExtras();
      }
      if (item.type === "filter" || item.type === "condition" || item.type === "switch") {
        node.dataset.conditionKind = item.data && item.data.condition_kind ? item.data.condition_kind : kindFromTrigger();
        hydrateCondition(node, item.data || {});
      }
      if (item.type === "switch") {
        var switchIndex = indexFromId(item.id, nextIndex("[data-switch-node]", "switchIndex"));
        node.dataset.switchIndex = String(switchIndex);
        node.dataset.nodeId = item.id || ("switch-" + switchIndex);
        Array.prototype.forEach.call(node.querySelectorAll("[name]"), function (field) {
          field.name = field.name.replace(/^switch\d+_/, "switch" + switchIndex + "_");
        });
      }
      if (item.type === "merge") {
        var mergeIndex = indexFromId(item.id, nextIndex("[data-merge-node]", "mergeIndex"));
        node.dataset.mergeIndex = String(mergeIndex);
        node.dataset.nodeId = item.id || ("merge-" + mergeIndex);
        Array.prototype.forEach.call(node.querySelectorAll("[name]"), function (field) {
          field.name = field.name.replace(/^merge\d+_/, "merge" + mergeIndex + "_");
        });
        setValue(node, "[data-merge-mode]", item.data ? (item.data.merge_mode || "wait_all") : "wait_all");
      }
      if (item.type === "loop") {
        var loopIndex = indexFromId(item.id, nextIndex("[data-loop-node]", "loopIndex"));
        node.dataset.loopIndex = String(loopIndex);
        node.dataset.nodeId = item.id || ("loop-" + loopIndex);
        Array.prototype.forEach.call(node.querySelectorAll("[name]"), function (field) {
          field.name = field.name.replace(/^loop\d+_/, "loop" + loopIndex + "_");
        });
        setValue(node, "[data-loop-mode]", item.data ? item.data.loop_mode : "for_each");
        setValue(node, "[data-loop-count]", item.data ? item.data.loop_count : "1");
      }
      if (item.type === "rule") {
        var ruleIndex = indexFromId(item.id, nextIndex("[data-rule-node]", "ruleIndex"));
        node.dataset.ruleIndex = String(ruleIndex);
        node.dataset.nodeId = item.id || ("rule-" + ruleIndex);
        Array.prototype.forEach.call(node.querySelectorAll("[name]"), function (field) {
          field.name = field.name.replace(/^rule\d+_/, "rule" + ruleIndex + "_");
        });
        setValue(node, "[data-rule-type]", item.data ? item.data.rule_type : "once_per_record");
        setValue(node, "[data-rule-days]", item.data ? item.data.rule_days : "0");
        initRuleNode(node);
      }
      if (item.type === "action") {
        var actionIndex = item.data && item.data.action_index ? Number(item.data.action_index) : indexFromId(item.id, nextIndex("[data-action-node]", "actionIndex"));
        node.dataset.actionIndex = String(actionIndex);
        node.dataset.nodeId = item.id || ("action-" + actionIndex);
        Array.prototype.forEach.call(node.querySelectorAll("[name]"), function (field) {
          field.name = field.name.replace(/^action\d+_/, "action" + actionIndex + "_");
        });
        var order = node.querySelector("[data-action-order]");
        if (order) order.textContent = String(actionIndex);
        setValue(node, "[data-action-type]", item.data ? item.data.action_type : "");
        initActionNode(node);
        setValue(node, '[name$="_pipeline"]', item.data ? item.data.pipeline : "");
        setValue(node, '[name$="_deal_stage"]', item.data ? item.data.deal_stage : "");
        setValue(node, '[name$="_contact_stage"]', item.data ? item.data.contact_stage : "");
        setValue(node, '[name$="_contact_first_name"]', item.data ? item.data.contact_first_name : "");
        setValue(node, '[name$="_contact_last_name"]', item.data ? item.data.contact_last_name : "");
        setValue(node, '[name$="_contact_email"]', item.data ? item.data.contact_email : "");
        setValue(node, '[name$="_contact_phone"]', item.data ? item.data.contact_phone : "");
        setValue(node, '[name$="_contact_job_title"]', item.data ? item.data.contact_job_title : "");
        setValue(node, '[name$="_company_name"]', item.data ? item.data.company_name : "");
        setValue(node, '[name$="_company_domain"]', item.data ? item.data.company_domain : "");
        setValue(node, '[name$="_company_industry"]', item.data ? item.data.company_industry : "");
        setValue(node, '[name$="_company_city"]', item.data ? item.data.company_city : "");
        setValue(node, '[name$="_text"]', item.data ? item.data.text : "");
        setValue(node, '[name$="_due_days"]', item.data ? item.data.due_days : "0");
        setValue(node, '[name$="_custom_key"]', item.data ? item.data.custom_key : "");
        setValue(node, '[name$="_custom_value"]', item.data ? item.data.custom_value : "");
        setValue(node, '[name$="_delay_amount"]', item.data ? (item.data.delay_amount || item.data.delay_minutes) : "15");
        setValue(node, '[name$="_delay_unit"]', item.data ? (item.data.delay_unit || "minutes") : "minutes");
        setValue(node, '[name$="_delay_until"]', item.data ? item.data.delay_until : "");
        setValue(node, '[name$="_delay_minutes"]', item.data ? item.data.delay_minutes : "15");
        setValue(node, '[name$="_webhook_url"]', item.data ? item.data.webhook_url : "");
        setValue(node, '[name$="_http_method"]', item.data ? item.data.http_method : "POST");
        setValue(node, '[name$="_http_url"]', item.data ? item.data.http_url : "");
        setValue(node, '[name$="_http_auth_type"]', item.data ? item.data.http_auth_type : "none");
        setValue(node, '[name$="_http_auth_token"]', item.data ? item.data.http_auth_token : "");
        setValue(node, '[name$="_http_username"]', item.data ? item.data.http_username : "");
        setValue(node, '[name$="_http_password"]', item.data ? item.data.http_password : "");
        setChecked(node, '[name$="_http_send_query"]', item.data ? item.data.http_send_query : "");
        setValue(node, '[name$="_http_query"]', item.data ? item.data.http_query : "");
        setQueryRows(node, item.data ? item.data.http_query : "");
        setChecked(node, '[name$="_http_send_headers"]', item.data ? item.data.http_send_headers : "");
        setValue(node, '[name$="_http_headers"]', item.data ? item.data.http_headers : "");
        setValue(node, '[name$="_http_content_type"]', item.data ? item.data.http_content_type : "json");
        setChecked(node, '[name$="_http_send_body"]', item.data ? item.data.http_send_body : "");
        setValue(node, '[name$="_http_body"]', item.data ? item.data.http_body : "");
        setValue(node, '[name$="_http_timeout"]', item.data ? item.data.http_timeout : "10");
        syncHttpNode(node);
      }
    }

    function buildNodeFromCanvas(item) {
      if (!item || !item.id) return null;
      var data = item.data || {};
      var node = null;
      if (item.type === "trigger") {
        node = renderTemplate(entryTemplate, 1);
        node.dataset.nodeId = item.id;
        setValue(node, "[data-trigger-select]", data.trigger);
      } else if (item.type === "filter") {
        var filterIndex = indexFromId(item.id, nextIndex("[data-filter-node]", "filterIndex"));
        node = renderTemplate(filterTemplate, filterIndex);
        node.dataset.filterIndex = String(filterIndex);
      } else if (item.type === "condition") {
        var conditionIndex = indexFromId(item.id, nextIndex("[data-condition-node]", "conditionIndex"));
        node = renderTemplate(conditionTemplate, conditionIndex);
        node.dataset.conditionIndex = String(conditionIndex);
      } else if (item.type === "switch") {
        var switchIndex = indexFromId(item.id, nextIndex("[data-switch-node]", "switchIndex"));
        node = renderTemplate(switchTemplate, switchIndex);
        node.dataset.switchIndex = String(switchIndex);
      } else if (item.type === "merge") {
        var mergeIndex = indexFromId(item.id, nextIndex("[data-merge-node]", "mergeIndex"));
        node = renderTemplate(mergeTemplate, mergeIndex);
        node.dataset.mergeIndex = String(mergeIndex);
      } else if (item.type === "loop") {
        var loopIndex = indexFromId(item.id, nextIndex("[data-loop-node]", "loopIndex"));
        node = renderTemplate(loopTemplate, loopIndex);
        node.dataset.loopIndex = String(loopIndex);
      } else if (item.type === "rule") {
        var ruleIndex = indexFromId(item.id, nextIndex("[data-rule-node]", "ruleIndex"));
        node = renderTemplate(ruleTemplate, ruleIndex);
        node.dataset.ruleIndex = String(ruleIndex);
      } else if (item.type === "action") {
        var actionIndex = data.action_index || indexFromId(item.id, nextIndex("[data-action-node]", "actionIndex"));
        node = renderTemplate(actionTemplate, actionIndex);
      }
      if (!node) return null;
      world.appendChild(node);
      setNodeFromCanvas(node, item);
      return node;
    }

    function loadInitialCanvas() {
      var script = document.getElementById("automation-initial-canvas");
      if (!script || !script.textContent) return false;
      var data = {};
      try {
        data = JSON.parse(script.textContent);
      } catch (error) {
        return false;
      }
      if (!data || !Array.isArray(data.nodes) || !data.nodes.length) return false;
      Array.prototype.forEach.call(world.querySelectorAll("[data-flow-node]"), function (node) {
        node.remove();
      });
      edges = [];
      var maxX = baseStageWidth;
      var maxY = baseStageHeight;
      var savedViewport = data.viewport || {};
      var nodePositions = data.nodes.map(function (item) {
        return {
          x: Number(item.x || 0),
          y: Number(item.y || 0)
        };
      });
      var maxSavedX = Math.max.apply(null, nodePositions.map(function (item) { return item.x; }));
      var maxSavedY = Math.max.apply(null, nodePositions.map(function (item) { return item.y; }));
      var savedLeft = Number(savedViewport.scroll_left || 0);
      var savedTop = Number(savedViewport.scroll_top || 0);
      var shiftSavedNodes = maxSavedX < 1400 && maxSavedY < 1100 && savedLeft < 900 && savedTop < 900;
      var savedShift = shiftSavedNodes ? canvasOriginOffset : 0;
      data.nodes.forEach(function (item) {
        var shiftedItem = Object.assign({}, item, {
          x: Number(item.x || 0) + savedShift,
          y: Number(item.y || 0) + savedShift
        });
        buildNodeFromCanvas(shiftedItem);
        maxX = Math.max(maxX, Number(shiftedItem.x || 0) + 820);
        maxY = Math.max(maxY, Number(shiftedItem.y || 0) + 620);
      });
      baseStageWidth = maxX;
      baseStageHeight = maxY;
      var viewport = savedViewport;
      zoom = Math.max(minZoom, Math.min(maxZoom, Number(viewport.zoom || 1)));
      applyStageSize();
      var ids = {};
      Array.prototype.forEach.call(world.querySelectorAll("[data-flow-node]"), function (node) {
        ids[node.dataset.nodeId] = true;
      });
      if (Array.isArray(data.edges)) {
        edges = data.edges.filter(function (edge) {
          return edge && ids[edge.from] && ids[edge.to] && edge.from !== edge.to;
        }).map(function (edge) {
          return { from: edge.from, to: edge.to };
        });
      }
      syncCondition();
      actionNodes().forEach(initActionNode);
      ruleNodes().forEach(initRuleNode);
      updateRemoveButtons();
      drawLinks();
      syncCanvasInput();
      setTimeout(function () {
        var nextLeft = Math.max(0, savedLeft + savedShift - (savedShift ? 180 : 0));
        var nextTop = Math.max(0, savedTop + savedShift - (savedShift ? 220 : 0));
        setCanvasViewport(-nextLeft * zoom, -(nextTop || canvasOriginOffset) * zoom);
        syncCanvasInput();
      }, 0);
      return true;
    }

    var drag = null;
    var pan = null;

    links.addEventListener("click", function (event) {
      var path = event.target.closest ? event.target.closest(".node-link") : null;
      if (!path || path.classList.contains("node-link--draft")) return;
      event.preventDefault();
      event.stopPropagation();
      selectedEdgeIndex = Number(path.dataset.edgeIndex);
      drawLinks();
    });

    links.addEventListener("dblclick", function (event) {
      var path = event.target.closest ? event.target.closest(".node-link") : null;
      if (!path || path.classList.contains("node-link--draft")) return;
      event.preventDefault();
      event.stopPropagation();
      selectedEdgeIndex = Number(path.dataset.edgeIndex);
      removeSelectedEdge();
    });

    canvas.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      var target = event.target;
      if (target.closest && target.closest("[data-flow-node], [data-port], button, input, select, textarea, a, .node-link")) return;
      pan = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        x: panX,
        y: panY
      };
      hideEdgeDelete();
      drawLinks();
      canvas.classList.add("is-panning");
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    canvas.addEventListener("pointermove", function (event) {
      if (!pan || pan.pointerId !== event.pointerId) return;
      setCanvasViewport(
        pan.x + (event.clientX - pan.startX),
        pan.y + (event.clientY - pan.startY)
      );
    });

    function endPan(event) {
      if (!pan || pan.pointerId !== event.pointerId) return;
      pan = null;
      canvas.classList.remove("is-panning");
      syncCanvasInput();
    }

    canvas.addEventListener("pointerup", endPan);
    canvas.addEventListener("pointercancel", endPan);

    canvas.addEventListener("wheel", function (event) {
      if (!event.shiftKey) {
        event.preventDefault();
        setCanvasViewport(panX - event.deltaX, panY - event.deltaY);
        syncCanvasInput();
        return;
      }
      event.preventDefault();
      setZoom(zoom + (event.deltaY < 0 ? 0.1 : -0.1), event);
    }, { passive: false });

    layout.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      var button = event.target.closest("[data-add-trigger], [data-add-filter], [data-add-condition], [data-add-switch], [data-add-merge], [data-add-loop], [data-add-rule], [data-add-action]");
      if (!button || !layout.contains(button)) return;
      startPaletteDrag(button, event);
    });

    layout.addEventListener("pointermove", function (event) {
      if (!paletteDrag || paletteDrag.pointerId !== event.pointerId) return;
      var dx = Math.abs(event.clientX - paletteDrag.startX);
      var dy = Math.abs(event.clientY - paletteDrag.startY);
      if (!paletteDrag.moved && dx + dy > 6) {
        paletteDrag.moved = true;
        ensurePaletteGhost(event);
      }
      if (!paletteDrag.moved) return;
      event.preventDefault();
      movePaletteGhost(event);
    });

    function endPaletteDrag(event) {
      if (!paletteDrag || paletteDrag.pointerId !== event.pointerId) return;
      if (paletteDrag.moved) {
        event.preventDefault();
        if (isOverCanvas(event)) {
          addPaletteNode(paletteDrag.payload, {
            connect: false,
            position: dropPosition(event)
          });
        }
        suppressPaletteClick = true;
        setTimeout(function () { suppressPaletteClick = false; }, 0);
      }
      clearPaletteDrag();
    }

    layout.addEventListener("pointerup", endPaletteDrag);
    layout.addEventListener("pointercancel", function (event) {
      if (!paletteDrag || paletteDrag.pointerId !== event.pointerId) return;
      clearPaletteDrag();
    });

    stage.addEventListener("pointerdown", function (event) {
      var outPort = event.target.closest('[data-port="out"], [data-port="out-false"]');
      if (outPort && stage.contains(outPort)) {
        var source = outPort.closest("[data-flow-node]");
        if (!source) return;
        draftEdge = { from: source.dataset.nodeId, to: stagePoint(event), pointerId: event.pointerId, branch: outPort.dataset.port === "out-false" ? "false" : "" };
        outPort.setPointerCapture(event.pointerId);
        drawLinks();
        event.preventDefault();
        return;
      }
      if (event.target.closest("button, input, select, textarea, a")) return;
      var node = event.target.closest("[data-flow-node]");
      if (!node || !stage.contains(node)) return;
      drag = {
        node: node,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: node.offsetLeft,
        top: node.offsetTop
      };
      node.classList.add("is-dragging");
      node.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    stage.addEventListener("pointermove", function (event) {
      if (draftEdge && draftEdge.pointerId === event.pointerId) {
        draftEdge.to = stagePoint(event);
        drawLinks();
        return;
      }
      if (!drag || drag.pointerId !== event.pointerId) return;
      setNodePosition(drag.node, drag.left + (event.clientX - drag.startX) / zoom, drag.top + (event.clientY - drag.startY) / zoom);
      drawLinks();
    });

    function endDrag(event) {
      if (draftEdge && draftEdge.pointerId === event.pointerId) {
        var target = document.elementFromPoint(event.clientX, event.clientY);
        var inPort = target && target.closest ? target.closest('[data-port="in"]') : null;
        var targetNode = inPort && inPort.closest("[data-flow-node]");
        if (targetNode) connectNodes(draftEdge.from, targetNode.dataset.nodeId, draftEdge.branch);
        draftEdge = null;
        drawLinks();
        return;
      }
      if (!drag || drag.pointerId !== event.pointerId) return;
      drag.node.classList.remove("is-dragging");
      drag = null;
      drawLinks();
      syncCanvasInput();
    }

    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    layout.addEventListener("click", function (event) {
      if (suppressPaletteClick) {
        event.preventDefault();
        return;
      }
      var helpButton = event.target.closest("[data-node-help-toast]");
      if (helpButton && layout.contains(helpButton)) {
        event.preventDefault();
        if (window.nucleoToast) {
          window.nucleoToast(helpButton.dataset.help || "Configure este node pelos campos abaixo.", "info");
        }
        return;
      }
      if (event.target.closest("[data-toggle-palette]")) {
        event.preventDefault();
        if (palette && palette.hasAttribute("data-side-palette")) openPalette();
        else if (palette) palette.hidden = !palette.hidden;
        return;
      }
      if (event.target.closest("[data-close-palette]")) {
        event.preventDefault();
        closePalette();
        return;
      }
      if (event.target.closest("[data-zoom-out]")) {
        event.preventDefault();
        setZoom(zoom - 0.1);
        return;
      }
      if (event.target.closest("[data-zoom-in]")) {
        event.preventDefault();
        setZoom(zoom + 0.1);
        return;
      }
      if (event.target.closest("[data-zoom-reset]")) {
        event.preventDefault();
        setZoom(1);
        return;
      }
      var triggerButton = event.target.closest("[data-add-trigger]");
      if (triggerButton && layout.contains(triggerButton)) {
        event.preventDefault();
        setTrigger(triggerButton.dataset.addTrigger);
        closePalette();
        return;
      }
      var filterButton = event.target.closest("[data-add-filter]");
      if (filterButton && layout.contains(filterButton)) {
        event.preventDefault();
        addFilter(filterButton.dataset.addFilter);
        closePalette();
        return;
      }
      var conditionButton = event.target.closest("[data-add-condition]");
      if (conditionButton && layout.contains(conditionButton)) {
        event.preventDefault();
        addCondition(conditionButton.dataset.addCondition);
        closePalette();
        return;
      }
      var switchButton = event.target.closest("[data-add-switch]");
      if (switchButton && layout.contains(switchButton)) {
        event.preventDefault();
        addSwitch(switchButton.dataset.addSwitch);
        closePalette();
        return;
      }
      var mergeButton = event.target.closest("[data-add-merge]");
      if (mergeButton && layout.contains(mergeButton)) {
        event.preventDefault();
        addMerge(mergeButton.dataset.addMerge || "wait_all");
        closePalette();
        return;
      }
      var loopButton = event.target.closest("[data-add-loop]");
      if (loopButton && layout.contains(loopButton)) {
        event.preventDefault();
        addLoop(loopButton.dataset.addLoop || "for_each");
        closePalette();
        return;
      }
      var ruleButton = event.target.closest("[data-add-rule]");
      if (ruleButton && layout.contains(ruleButton)) {
        event.preventDefault();
        addRule(ruleButton.dataset.addRule);
        closePalette();
        return;
      }
      var add = event.target.closest("[data-add-action]");
      if (add && layout.contains(add)) {
        event.preventDefault();
        addAction(add.dataset.addAction || "create_task");
        closePalette();
        return;
      }
      var deleteEdge = event.target.closest("[data-edge-delete]");
      if (deleteEdge && layout.contains(deleteEdge)) {
        event.preventDefault();
        removeSelectedEdge();
        return;
      }
      var remove = event.target.closest("[data-remove-action]");
      if (remove && layout.contains(remove)) {
        event.preventDefault();
        removeAction(remove.closest("[data-action-node]"));
        return;
      }
      var removeFilterButton = event.target.closest("[data-remove-filter]");
      if (removeFilterButton && layout.contains(removeFilterButton)) {
        event.preventDefault();
        removeAndReconnect(removeFilterButton.closest("[data-filter-node]"), true);
        return;
      }
      var removeConditionButton = event.target.closest("[data-remove-condition]");
      if (removeConditionButton && layout.contains(removeConditionButton)) {
        event.preventDefault();
        removeAndReconnect(removeConditionButton.closest("[data-condition-node]"), true);
        return;
      }
      var removeSwitchButton = event.target.closest("[data-remove-switch]");
      if (removeSwitchButton && layout.contains(removeSwitchButton)) {
        event.preventDefault();
        removeAndReconnect(removeSwitchButton.closest("[data-switch-node]"), true);
        return;
      }
      var removeMergeButton = event.target.closest("[data-remove-merge]");
      if (removeMergeButton && layout.contains(removeMergeButton)) {
        event.preventDefault();
        removeAndReconnect(removeMergeButton.closest("[data-merge-node]"), true);
        return;
      }
      var removeLoopButton = event.target.closest("[data-remove-loop]");
      if (removeLoopButton && layout.contains(removeLoopButton)) {
        event.preventDefault();
        removeAndReconnect(removeLoopButton.closest("[data-loop-node]"), true);
        return;
      }
      var removeRuleButton = event.target.closest("[data-remove-rule]");
      if (removeRuleButton && layout.contains(removeRuleButton)) {
        event.preventDefault();
        removeAndReconnect(removeRuleButton.closest("[data-rule-node]"), true);
        return;
      }
      var removeEntryButton = event.target.closest("[data-remove-entry]");
      if (removeEntryButton && layout.contains(removeEntryButton)) {
        event.preventDefault();
        removeAndReconnect(removeEntryButton.closest("[data-entry-node]"), false);
      }
    });

    document.addEventListener("keydown", function (event) {
      var active = document.activeElement;
      var isTyping = active && active.matches && active.matches("input, select, textarea");
      if (isTyping || (event.key !== "Delete" && event.key !== "Backspace")) return;
      if (selectedEdgeIndex === null) return;
      event.preventDefault();
      removeSelectedEdge();
    });

    root.addEventListener("change", function (event) {
      if (event.target.matches && event.target.matches("[data-trigger-select], [data-trigger-amount], [data-trigger-unit]")) {
        syncTriggerExtras();
        syncCondition();
      }
      if (event.target.matches && event.target.matches('[name$="_http_auth_type"], [data-http-toggle]')) {
        var httpNode = event.target.closest("[data-action-node]");
        if (httpNode) syncHttpNode(httpNode);
      }
      syncCanvasInput();
    });
    root.addEventListener("input", function (event) {
      if (event.target.matches && event.target.matches("[data-trigger-amount]")) syncTriggerExtras();
      syncCanvasInput();
    });
    root.addEventListener("submit", syncCanvasInput);
    applyStageSize();
    if (!loadInitialCanvas()) {
      setTimeout(function () {
        setCanvasViewport(-(canvasOriginOffset - 180) * zoom, -(canvasOriginOffset - 220) * zoom);
        syncCanvasInput();
      }, 0);
      actionNodes().forEach(initActionNode);
      ruleNodes().forEach(initRuleNode);
      updateRemoveButtons();
      syncTriggerExtras();
      syncCondition();
      syncCanvasInput();
    }
    window.addEventListener("resize", drawLinks);
    setTimeout(drawLinks, 60);
  }

  function initAutomationCanvases() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-automation-canvas]"), initAutomationCanvas);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initToasts();
      initDealStageFields();
      initPipelineStageForms();
      initDealChats();
      initWhatsAppUnread();
      initAutomationCanvases();
    });
  } else {
    initToasts();
    initDealStageFields();
    initPipelineStageForms();
    initDealChats();
    initWhatsAppUnread();
    initAutomationCanvases();
  }
  document.body.addEventListener("htmx:afterSwap", function () {
    initToasts();
    initDealStageFields();
    initPipelineStageForms();
    initDealChats();
    initWhatsAppUnread();
    initAutomationCanvases();
  });
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var form = event.detail && event.detail.elt;
    var xhr = event.detail && event.detail.xhr;
    if (form && form.matches && form.matches(".deal-chat__composer, .wa-composer") &&
        xhr && xhr.getResponseHeader("X-Nucleo-Message-Sent") === "1") {
      form.reset();
    }
  });
})();
