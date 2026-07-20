// Núcleo — Kanban drag-and-drop. Uses SortableJS in NATIVE HTML5 drag mode
// (hardware-accelerated → light to drag) and persists moves to the server.
// Re-runs after HTMX swaps rebuild the board.
(function () {
  function replayHxTriggers(response, fallbackToast) {
    var triggers = {};
    try {
      triggers = JSON.parse(response.headers.get("HX-Trigger") || "{}");
    } catch (e) {
      triggers = {};
    }

    Object.keys(triggers).forEach(function (name) {
      var detail = triggers[name];
      if (name === "nucleo:toast" && window.nucleoToast) {
        window.nucleoToast(detail.text, detail.kind || "success");
      } else {
        document.body.dispatchEvent(new CustomEvent(name, {
          bubbles: true,
          detail: detail === true ? {} : detail,
        }));
      }
    });

    if (!triggers["nucleo:toast"] && fallbackToast && window.nucleoToast) {
      window.nucleoToast(fallbackToast, "success");
    }
  }

  function persistMove(column, movedCard) {
    var stage = column.dataset.stage;
    var ids = Array.prototype.map.call(
      column.querySelectorAll(".kanban-card"),
      function (c) { return c.dataset.id; }
    );
    var body = new FormData();
    body.append("stage", stage);
    body.append("order", ids.join(","));
    body.append("csrfmiddlewaretoken", window.NUCLEO_CSRF);

    fetch("/crm/deals/" + movedCard.dataset.id + "/move/", {
      method: "POST",
      body: body,
      headers: { "X-CSRFToken": window.NUCLEO_CSRF },
    }).then(function (r) {
      if (r.ok) {
        replayHxTriggers(r);
      } else {
        window.nucleoToast("Não foi possível mover o negócio", "error");
      }
    }).catch(function () {
      window.nucleoToast("Falha de conexão ao mover", "error");
    });
  }

  function persistColumnOrder(board) {
    var ids = Array.prototype.map.call(
      board.querySelectorAll(".kanban__col[data-stage-id]"),
      function (c) { return c.dataset.stageId; }
    );
    var body = new FormData();
    body.append("order", ids.join(","));
    body.append("csrfmiddlewaretoken", window.NUCLEO_CSRF);
    fetch("/crm/pipelines/stages/reorder/", {
      method: "POST",
      body: body,
      headers: { "X-CSRFToken": window.NUCLEO_CSRF },
    }).then(function (r) {
      if (r.ok) {
        replayHxTriggers(r, "Ordem das fases salva.");
      } else {
        window.nucleoToast("Não foi possível reordenar as colunas", "error");
      }
    }).catch(function () {
      window.nucleoToast("Falha de conexão ao reordenar", "error");
    });
  }

  function persistStageListOrder(list) {
    var ids = Array.prototype.map.call(
      list.querySelectorAll("[data-stage-id]"),
      function (row) { return row.dataset.stageId; }
    );
    var body = new FormData();
    body.append("order", ids.join(","));
    body.append("pipeline", list.dataset.pipelineId || "");
    body.append("return_modal", "1");
    body.append("csrfmiddlewaretoken", window.NUCLEO_CSRF);
    fetch(list.dataset.reorderUrl || "/crm/pipelines/stages/reorder/", {
      method: "POST",
      body: body,
      headers: { "X-CSRFToken": window.NUCLEO_CSRF },
    }).then(function (r) {
      if (r.ok) {
        replayHxTriggers(r, "Ordem das fases salva.");
      } else {
        window.nucleoToast("Não foi possível reordenar as fases", "error");
      }
    }).catch(function () {
      window.nucleoToast("Falha de conexão ao reordenar", "error");
    });
  }

  window.nucleoInitKanban = function () {
    if (typeof Sortable === "undefined") return;
    document.querySelectorAll("[data-stage-list]").forEach(function (list) {
      if (list.dataset.sortable) return;
      list.dataset.sortable = "1";
      new Sortable(list, {
        animation: 150,
        easing: "cubic-bezier(0.2, 0, 0, 1)",
        draggable: "[data-stage-id]",
        handle: ".stage-row__grip, .pipeline-stage-row__drag",
        filter: "input, select, textarea, button, a",
        preventOnFilter: false,
        ghostClass: "stage-row--ghost",
        chosenClass: "stage-row--chosen",
        dragClass: "stage-row--drag",
        forceFallback: true,
        fallbackOnBody: true,
        fallbackTolerance: 4,
        onEnd: function (evt) {
          if (evt.oldIndex !== evt.newIndex) {
            persistStageListOrder(list);
          }
        },
      });
    });

    document.querySelectorAll(".kanban-board").forEach(function (board) {
      if (board.dataset.canEdit !== "1") return;
      if (board.dataset.columnSortable) return;
      board.dataset.columnSortable = "1";
      new Sortable(board, {
        animation: 150,
        easing: "cubic-bezier(0.2, 0, 0, 1)",
        direction: "horizontal",
        draggable: ".kanban__col[data-stage-id]",
        handle: ".kanban__col-head",
        filter: "button, input, select, textarea, a, .kanban-card, .kanban__cards, .kanban__col-editform, .kanban__col--add",
        preventOnFilter: false,
        forceFallback: true,
        fallbackOnBody: true,
        fallbackTolerance: 5,
        scroll: true,
        scrollSensitivity: 60,
        scrollSpeed: 12,
        ghostClass: "kanban__col--ghost",
        chosenClass: "kanban__col--chosen",
        dragClass: "kanban__col--drag",
        onEnd: function (evt) {
          if (evt.oldIndex !== evt.newIndex) {
            persistColumnOrder(board);
          }
        },
      });
    });

    document.querySelectorAll(".kanban__cards").forEach(function (col) {
      if (col.dataset.sortable) return;
      col.dataset.sortable = "1";
      new Sortable(col, {
        group: "deals",
        animation: 130,
        easing: "cubic-bezier(0.2, 0, 0, 1)",
        draggable: ".kanban-card",
        ghostClass: "kanban-card--ghost",
        chosenClass: "sortable-chosen",
        dragClass: "sortable-drag",
        onEnd: function (evt) { persistMove(evt.to, evt.item); },
      });
    });
  };

  // Click a card to open its detail. A real drag does not emit a click, so
  // this stays out of the way of dragging. Ignore drags flagged by Sortable.
  document.addEventListener("click", function (e) {
    var card = e.target.closest(".kanban-card");
    if (card && card.dataset.href && !card.classList.contains("sortable-chosen")) {
      window.location.href = card.dataset.href;
    }
  });

  function initKanbanWheelScroll() {
    document.querySelectorAll(".kanban-board").forEach(function (board) {
      if (board.dataset.wheelScroll) return;
      board.dataset.wheelScroll = "1";
      board.addEventListener("wheel", function (e) {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
          e.preventDefault();
          board.scrollLeft += e.deltaY;
        }
      }, { passive: false });
    });
  }

  document.addEventListener("DOMContentLoaded", initKanbanWheelScroll);
  document.body.addEventListener("htmx:afterSwap", initKanbanWheelScroll);
  document.addEventListener("DOMContentLoaded", window.nucleoInitKanban);
  document.body.addEventListener("htmx:afterSwap", window.nucleoInitKanban);
})();
