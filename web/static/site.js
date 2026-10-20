(function () {
  "use strict";

  var status = document.createElement("div");
  status.className = "visually-hidden";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  document.body.appendChild(status);

  function select(element) {
    var range = document.createRange();
    range.selectNodeContents(element);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function legacyCopy(element) {
    return new Promise(function (resolve, reject) {
      select(element);
      if (document.execCommand && document.execCommand("copy")) {
        resolve();
      } else {
        reject(new Error("copy failed"));
      }
    });
  }

  function copyText(element) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(element.textContent).catch(function () {
        return legacyCopy(element);
      });
    }
    return legacyCopy(element);
  }

  function announce(button, message) {
    var label = button.querySelector("span");
    if (!button.dataset.label) {
      button.dataset.label = label.textContent;
    }
    window.clearTimeout(button.timer);
    label.textContent = message;
    status.textContent = message;
    button.timer = window.setTimeout(function () {
      label.textContent = button.dataset.label;
      status.textContent = "";
    }, 2000);
  }

  document.querySelectorAll("button[data-copy]").forEach(function (button) {
    var target = document.getElementById(button.getAttribute("data-copy"));
    if (!target) {
      return;
    }
    button.hidden = false;
    button.addEventListener("click", function () {
      copyText(target).then(
        function () {
          announce(button, "Copied");
        },
        function () {
          select(target);
          announce(button, "Press Ctrl+C");
        }
      );
    });
  });

  document.querySelectorAll("details[data-collapse-narrow]").forEach(function (details) {
    if (window.matchMedia("(max-width: 55.99rem)").matches) {
      details.open = false;
    }
  });

  document.querySelectorAll("select[data-autosubmit]").forEach(function (field) {
    field.addEventListener("change", function () {
      if (field.form) {
        field.form.requestSubmit();
      }
    });
  });
})();
