// Núcleo — glue between HTMX server events and the Alpine-driven shell.
// The server responds to writes with:
//   HX-Trigger: {"nucleo:closeModal": true, "nucleo:dataChanged": true}
// htmx re-emits those as DOM events. Colons are awkward for Alpine's @-syntax,
// so we bridge them to hyphenated window events the shell can bind to.
(function () {
  document.body.addEventListener("nucleo:closeModal", function () {
    window.dispatchEvent(new CustomEvent("nucleo-close-modal"));
  });

  document.body.addEventListener("nucleo:dataChanged", function () {
    window.nucleoToast("Alterações salvas", "success");
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
})();
