// Núcleo — Kanban drag-and-drop. Uses SortableJS in NATIVE HTML5 drag mode
// (hardware-accelerated → light to drag) and persists moves to the server.
// Re-runs after HTMX swaps rebuild the board.
(function () {
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
        document.body.dispatchEvent(new CustomEvent("nucleo:dealsStats", { bubbles: true }));
      } else {
        window.nucleoToast("Não foi possível mover o negócio", "error");
      }
    }).catch(function () {
      window.nucleoToast("Falha de conexão ao mover", "error");
    });
  }

  window.nucleoInitKanban = function () {
    if (typeof Sortable === "undefined") return;
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

  document.addEventListener("DOMContentLoaded", window.nucleoInitKanban);
  document.body.addEventListener("htmx:afterSwap", window.nucleoInitKanban);
})();
