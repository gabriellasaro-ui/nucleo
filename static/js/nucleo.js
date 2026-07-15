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

  window.nucleoToast = function (text, kind) {
    var host = document.getElementById("toasts");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = text;
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .3s, transform .3s";
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      setTimeout(function () { el.remove(); }, 320);
    }, 2600);
  };

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
    var canvasInput = root.querySelector("[data-canvas-input]");
    var edgeDelete = root.querySelector("[data-edge-delete]");
    var zoomLabel = root.querySelector("[data-zoom-label]");
    var palette = layout.querySelector("[data-node-palette]");
    var startButton = root.querySelector("[data-start-flow]");
    if (!canvas || !stage || !world || !links || !entryTemplate || !actionTemplate || !filterTemplate || !conditionTemplate) return;
    var edges = [];
    var draftEdge = null;
    var selectedEdgeIndex = null;
    var zoom = 1;
    var minZoom = 0.55;
    var maxZoom = 1.75;
    var baseStageWidth = parseInt(getComputedStyle(stage).getPropertyValue("--stage-width"), 10) || 1540;
    var baseStageHeight = parseInt(getComputedStyle(stage).getPropertyValue("--stage-height"), 10) || 940;
    var actionMeta = {};

    Array.prototype.forEach.call(layout.querySelectorAll("[data-add-action][data-node-tone]"), function (button) {
      var type = button.dataset.addAction;
      var icon = button.querySelector("span");
      if (!type || actionMeta[type]) return;
      actionMeta[type] = {
        icon: icon ? icon.innerHTML : "",
        tone: button.dataset.nodeTone || ""
      };
    });

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
      var scaledWidth = Math.ceil(baseStageWidth * zoom);
      var scaledHeight = Math.ceil(baseStageHeight * zoom);
      stage.style.setProperty("--stage-width", baseStageWidth + "px");
      stage.style.setProperty("--stage-height", baseStageHeight + "px");
      stage.style.setProperty("--canvas-zoom", String(zoom));
      stage.style.width = scaledWidth + "px";
      stage.style.height = scaledHeight + "px";
      stage.style.minWidth = scaledWidth + "px";
      stage.style.minHeight = scaledHeight + "px";
      if (zoomLabel) zoomLabel.textContent = Math.round(zoom * 100) + "%";
    }

    function updateStartState() {
      if (startButton) startButton.hidden = !!entryNode();
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

    function focusNode(node) {
      if (!node) return;
      canvas.scrollTo({
        left: Math.max(0, node.offsetLeft * zoom - 120),
        top: Math.max(0, node.offsetTop * zoom - 120),
        behavior: "smooth"
      });
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
      var to = portPoint(toNode, "in");
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
      Array.prototype.forEach.call(world.querySelectorAll("[data-filter-node], [data-condition-node]"), function (node) {
        var kind = node.dataset.conditionKind || kindFromTrigger();
        var title = node.querySelector("[data-filter-title]");
        if (title) {
          if (node.dataset.nodeKind === "filter") title.textContent = kind === "deal" ? "Filtro de negocio" : "Filtro de contato";
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

    function ensureEntry(value) {
      var existing = entryNode();
      if (existing) {
        var existingSelect = existing.querySelector("[data-trigger-select]");
        if (value && existingSelect) existingSelect.value = value;
        updateStartState();
        syncCondition();
        return existing;
      }
      var node = renderTemplate(entryTemplate, 1);
      setNodePosition(node, 120, 180);
      world.appendChild(node);
      var select = node.querySelector("[data-trigger-select]");
      if (select && value) select.value = value;
      updateStartState();
      syncCondition();
      syncCanvasInput();
      focusNode(node);
      return node;
    }

    function setTrigger(value) {
      if (!value) return;
      var node = ensureEntry(value);
      var select = node.querySelector("[data-trigger-select]");
      if (select) select.value = value;
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
      var select = node.querySelector("[data-action-type]");
      if (!select) return;
      var type = select.value;
      var selected = select.options[select.selectedIndex];
      var meta = actionMeta[type] || {};
      node.dataset.nodeSubtype = type || "";
      node.dataset.nodeTone = meta.tone || "";
      var title = node.querySelector("[data-action-title]");
      if (title) title.textContent = type ? selected.textContent : "Executar";
      var icon = node.querySelector("[data-action-icon]");
      if (icon) icon.innerHTML = meta.icon || "";
      Array.prototype.forEach.call(node.querySelectorAll("[data-action-field]"), function (field) {
        var allowed = (field.dataset.showFor || "").split(",").indexOf(type) !== -1;
        field.hidden = !allowed;
        field.disabled = !allowed;
      });
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
      if (x + 480 > baseStageWidth) {
        baseStageWidth = x + 540;
        applyStageSize();
      }
    }

    function ensureStageHeight(y) {
      if (y + 260 > baseStageHeight) {
        baseStageHeight = y + 320;
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

    function addFilter(kind) {
      var entry = ensureEntry();
      var previous = tailNode() || entry;
      var index = nextIndex("[data-filter-node]", "filterIndex");
      var node = renderTemplate(filterTemplate, index);
      node.dataset.conditionKind = kind || kindFromTrigger();
      var x = previous ? previous.offsetLeft + 410 : 520;
      var y = previous ? previous.offsetTop : 180;
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      syncCondition();
      insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      focusNode(node);
    }

    function addCondition(kind) {
      var entry = ensureEntry();
      var previous = tailNode() || entry;
      var index = nextIndex("[data-condition-node]", "conditionIndex");
      var node = renderTemplate(conditionTemplate, index);
      node.dataset.conditionKind = kind || kindFromTrigger();
      var x = previous ? previous.offsetLeft + 410 : 520;
      var y = previous ? previous.offsetTop : 180;
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      syncCondition();
      insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      focusNode(node);
    }

    function addAction(type) {
      var entry = ensureEntry();
      var current = actionNodes();
      var index = current.length + 1;
      var node = renderTemplate(actionTemplate, index);
      var previous = tailNode() || entry;
      var x = previous ? previous.offsetLeft + 410 : 520;
      var y = previous ? previous.offsetTop : 180;
      ensureStageWidth(x);
      ensureStageHeight(y);
      setNodePosition(node, x, y);
      world.appendChild(node);
      var select = node.querySelector("[data-action-type]");
      if (select) select.value = type || "create_task";
      initActionNode(node);
      insertNodeAfter(previous.dataset.nodeId, node.dataset.nodeId);
      updateRemoveButtons();
      drawLinks();
      syncCanvasInput();
      canvas.scrollTo({ left: Math.max(0, x * zoom - 120), top: Math.max(0, y * zoom - 140), behavior: "smooth" });
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
      updateStartState();
      drawLinks();
      syncCanvasInput();
    }

    function removeAction(node) {
      removeAndReconnect(node, true);
      renumberActions();
      syncCanvasInput();
    }

    function connectNodes(fromId, toId) {
      if (!fromId || !toId || fromId === toId || !nodeById(fromId) || !nodeById(toId)) return;
      edges = edges.filter(function (edge) {
        return !(edge.from === fromId && edge.to === toId);
      });
      edges.push({ from: fromId, to: toId });
      hideEdgeDelete();
      drawLinks();
      syncCanvasInput();
    }

    function portPoint(node, side) {
      return {
        x: node.offsetLeft + (side === "out" ? node.offsetWidth : 0),
        y: node.offsetTop + node.offsetHeight / 2
      };
    }

    function addPath(fromNode, toNode, className) {
      if (!fromNode || !toNode) return;
      var from = portPoint(fromNode, "out");
      var to = portPoint(toNode, "in");
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
      var from = portPoint(fromNode, "out");
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
        var path = addPath(from, to, index === selectedEdgeIndex ? "node-link is-selected" : "node-link");
        if (path) path.dataset.edgeIndex = String(index);
      });
      addDraftPath();
      showEdgeDelete();
    }

    function stagePoint(event) {
      var rect = world.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left) / zoom,
        y: (event.clientY - rect.top) / zoom
      };
    }

    function nodeData(node) {
      if (node.dataset.nodeId === "entry") {
        var select = node.querySelector("[data-trigger-select]");
        return { trigger: select ? select.value : "" };
      }
      if (node.dataset.nodeKind === "filter" || node.dataset.nodeKind === "condition") {
        var kind = node.dataset.conditionKind || kindFromTrigger();
        var selector = kind === "deal" ? '[data-condition-stage="deal"]' : '[data-condition-stage="contact"]';
        var conditionField = node.querySelector(selector);
        return {
          condition_kind: kind,
          condition_stage: conditionField ? conditionField.value : ""
        };
      }
      var actionType = node.querySelector("[data-action-type]");
      var text = node.querySelector('[name$="_text"]');
      var pipeline = node.querySelector('[name$="_pipeline"]');
      var dealStage = node.querySelector('[name$="_deal_stage"]');
      var contactStage = node.querySelector('[name$="_contact_stage"]');
      var dueDays = node.querySelector('[name$="_due_days"]');
      return {
        action_index: Number(node.dataset.actionIndex || 0),
        action_type: actionType ? actionType.value : "",
        text: text && !text.disabled ? text.value : "",
        pipeline: pipeline && !pipeline.disabled ? pipeline.value : "",
        deal_stage: dealStage && !dealStage.disabled ? dealStage.value : "",
        contact_stage: contactStage && !contactStage.disabled ? contactStage.value : "",
        due_days: dueDays && !dueDays.disabled ? dueDays.value : "0"
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
          scroll_left: canvas.scrollLeft,
          scroll_top: canvas.scrollTop,
          zoom: zoom
        }
      });
    }

    function setZoom(nextZoom, anchorEvent) {
      var previousZoom = zoom;
      var canvasRect = canvas.getBoundingClientRect();
      var anchor = anchorEvent ? stagePoint(anchorEvent) : {
        x: (canvas.scrollLeft + canvas.clientWidth / 2) / zoom,
        y: (canvas.scrollTop + canvas.clientHeight / 2) / zoom
      };
      zoom = Math.max(minZoom, Math.min(maxZoom, Math.round(nextZoom * 20) / 20));
      if (zoom === previousZoom) return;
      applyStageSize();
      canvas.scrollLeft = Math.max(0, anchor.x * zoom - (anchorEvent ? anchorEvent.clientX - canvasRect.left : canvas.clientWidth / 2));
      canvas.scrollTop = Math.max(0, anchor.y * zoom - (anchorEvent ? anchorEvent.clientY - canvasRect.top : canvas.clientHeight / 2));
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

    function setNodeFromCanvas(node, item) {
      node.dataset.nodeId = item.id;
      setNodePosition(node, item.x || 120, item.y || 180);
      if (item.type === "filter" || item.type === "condition") {
        node.dataset.conditionKind = item.data && item.data.condition_kind ? item.data.condition_kind : kindFromTrigger();
        setValue(node, '[data-condition-stage="' + node.dataset.conditionKind + '"]', item.data ? item.data.condition_stage : "");
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
        setValue(node, '[name$="_text"]', item.data ? item.data.text : "");
        setValue(node, '[name$="_due_days"]', item.data ? item.data.due_days : "0");
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
      data.nodes.forEach(function (item) {
        buildNodeFromCanvas(item);
        maxX = Math.max(maxX, Number(item.x || 0) + 520);
        maxY = Math.max(maxY, Number(item.y || 0) + 320);
      });
      baseStageWidth = maxX;
      baseStageHeight = maxY;
      var viewport = data.viewport || {};
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
      updateRemoveButtons();
      updateStartState();
      drawLinks();
      syncCanvasInput();
      setTimeout(function () {
        canvas.scrollLeft = Number(viewport.scroll_left || 0);
        canvas.scrollTop = Number(viewport.scroll_top || 0);
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
        left: canvas.scrollLeft,
        top: canvas.scrollTop
      };
      hideEdgeDelete();
      drawLinks();
      canvas.classList.add("is-panning");
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    canvas.addEventListener("pointermove", function (event) {
      if (!pan || pan.pointerId !== event.pointerId) return;
      canvas.scrollLeft = pan.left - (event.clientX - pan.startX);
      canvas.scrollTop = pan.top - (event.clientY - pan.startY);
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
      if (!event.shiftKey) return;
      event.preventDefault();
      setZoom(zoom + (event.deltaY < 0 ? 0.1 : -0.1), event);
    }, { passive: false });

    stage.addEventListener("pointerdown", function (event) {
      var outPort = event.target.closest('[data-port="out"]');
      if (outPort && stage.contains(outPort)) {
        var source = outPort.closest("[data-flow-node]");
        if (!source) return;
        draftEdge = { from: source.dataset.nodeId, to: stagePoint(event), pointerId: event.pointerId };
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
        if (targetNode) connectNodes(draftEdge.from, targetNode.dataset.nodeId);
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
      if (event.target.closest("[data-start-flow]")) {
        event.preventDefault();
        ensureEntry();
        openPalette();
        return;
      }
      if (event.target.closest("[data-focus-library]")) {
        event.preventDefault();
        openPalette();
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
      if (event.target.matches && event.target.matches("[data-trigger-select]")) syncCondition();
      syncCanvasInput();
    });
    root.addEventListener("input", syncCanvasInput);
    root.addEventListener("submit", syncCanvasInput);
    applyStageSize();
    if (!loadInitialCanvas()) {
      actionNodes().forEach(initActionNode);
      updateRemoveButtons();
      updateStartState();
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
    document.addEventListener("DOMContentLoaded", initAutomationCanvases);
  } else {
    initAutomationCanvases();
  }
  document.body.addEventListener("htmx:afterSwap", initAutomationCanvases);
})();
