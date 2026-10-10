// Tag picker: searchable multi-select over the search-term catalog
// (/api/terms/<kind>), with free entry. Markup comes from _tag_picker.html.
(function () {
  "use strict";

  var catalogs = {}; // kind -> Promise<[{label, count, type}]>, fetched once per page load

  function loadCatalog(kind) {
    if (!catalogs[kind]) {
      catalogs[kind] = fetch("/api/terms/" + encodeURIComponent(kind))
        .then(function (r) { return r.ok ? r.json() : []; })
        .catch(function () { return []; });
    }
    return catalogs[kind];
  }

  function norm(text) {
    return text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^0-9a-z+#]+/g, " ").trim();
  }

  var TYPE_LABELS = { country: "country", province: "province", region: "region", city: "city", remote: "remote" };
  var MAX_OPTIONS = 60;

  function init(picker) {
    if (picker.dataset.ready) return;
    picker.dataset.ready = "1";
    var kind = picker.dataset.catalog;
    var name = picker.dataset.name;
    var box = picker.querySelector(".tp-box");
    var input = picker.querySelector(".tp-input");
    var menu = picker.querySelector(".tp-menu");
    var active = -1;
    var shown = [];
    var placeholder = input.getAttribute("placeholder") || "";
    var more = document.createElement("button");
    more.type = "button"; more.className = "tp-more"; more.hidden = true;
    box.insertBefore(more, input);
    more.addEventListener("click", function (e) { e.stopPropagation(); input.focus(); render(); });

    // The box keeps one fixed line: show the tags that fit, count the rest as "+N"
    // (all of them are listed at the top of the open menu).
    function layout() {
      var tags = picker.querySelectorAll(".tp-tag");
      tags.forEach(function (t) { t.hidden = false; });
      more.hidden = true;
      input.placeholder = tags.length ? "Add\u2026" : placeholder;
      if (!tags.length || !box.clientWidth) return;
      var gap = 5, room = box.clientWidth - 64 - 40; // keep space for the input and the "+N" chip
      var used = 0, hidden = 0;
      tags.forEach(function (t, i) {
        used += t.offsetWidth + gap;
        if (used > room && i > 0) { t.hidden = true; hidden++; }
      });
      if (hidden) { more.hidden = false; more.textContent = "+" + hidden; more.title = hidden + " more selected"; }
      box.scrollLeft = 0; // never leave the first tag half scrolled out of view
    }

    function selected() {
      return Array.prototype.map.call(picker.querySelectorAll(".tp-tag input"), function (i) { return i.value; });
    }

    function addTag(value) {
      value = value.replace(/\s+/g, " ").trim();
      if (!value) return;
      var key = norm(value);
      if (selected().some(function (v) { return norm(v) === key; })) return;
      var tag = document.createElement("span");
      tag.className = "tp-tag";
      var hidden = document.createElement("input");
      hidden.type = "hidden"; hidden.name = name; hidden.value = value;
      var text = document.createElement("span");
      text.className = "tp-text"; text.textContent = value;
      var remove = document.createElement("button");
      remove.type = "button"; remove.className = "tp-remove"; remove.textContent = "×";
      remove.setAttribute("aria-label", "Remove " + value);
      tag.append(hidden, text, remove);
      box.insertBefore(tag, more);
      layout();
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function close() {
      menu.hidden = true; active = -1;
      input.setAttribute("aria-expanded", "false");
    }

    function render() {
      var query = norm(input.value);
      var taken = {};
      selected().forEach(function (v) { taken[norm(v)] = true; });
      loadCatalog(kind).then(function (options) {
        var starts = [], contains = [], exact = false;
        options.forEach(function (o) {
          var n = norm(o.label);
          if (n === query) exact = true;
          if (taken[n]) return;
          if (!query || n.indexOf(query) === 0) starts.push(o);
          else if (n.indexOf(query) > 0) contains.push(o);
        });
        shown = starts.concat(contains).slice(0, MAX_OPTIONS);
        // Real matches first (Enter picks the best one); "Add …" last, or alone when nothing matches.
        if (query && !exact && !taken[query]) shown.push({ label: input.value.trim(), custom: true });
        menu.innerHTML = "";
        var picked = selected();
        if (!query && picked.length) {
          var head = document.createElement("li");
          head.className = "tp-heading"; head.textContent = "Selected (" + picked.length + ")";
          menu.appendChild(head);
          picked.forEach(function (value) {
            var li = document.createElement("li");
            li.className = "tp-selected"; li.dataset.remove = value;
            li.setAttribute("role", "option"); li.setAttribute("aria-selected", "true");
            var label = document.createElement("span"); label.textContent = "\u2713 " + value;
            var hint = document.createElement("small"); hint.textContent = "remove";
            li.append(label, hint);
            menu.appendChild(li);
          });
          if (shown.length) {
            var sep = document.createElement("li");
            sep.className = "tp-heading"; sep.textContent = "Suggestions";
            menu.appendChild(sep);
          }
        }
        shown.forEach(function (o, i) {
          var li = document.createElement("li");
          li.setAttribute("role", "option");
          li.dataset.index = i;
          if (o.custom) {
            li.className = "tp-custom";
            li.textContent = "Add “" + o.label + "”";
          } else {
            var label = document.createElement("span");
            label.textContent = o.label;
            li.appendChild(label);
            var meta = [];
            if (o.type && TYPE_LABELS[o.type]) meta.push(TYPE_LABELS[o.type]);
            if (o.count) meta.push(o.count.toLocaleString() + " jobs");
            if (meta.length) {
              var m = document.createElement("small");
              m.textContent = meta.join(" · ");
              li.appendChild(m);
            }
          }
          menu.appendChild(li);
        });
        if (!shown.length) {
          var empty = document.createElement("li");
          empty.className = "tp-empty"; empty.textContent = "No suggestions";
          menu.appendChild(empty);
        }
        active = shown.length ? 0 : -1;
        highlight();
        menu.hidden = false;
        input.setAttribute("aria-expanded", "true");
      });
    }

    function highlight() {
      Array.prototype.forEach.call(menu.querySelectorAll("li[data-index]"), function (li) {
        var on = +li.dataset.index === active;
        li.classList.toggle("active", on);
        if (on && input.value) li.scrollIntoView({ block: "nearest" });
      });
    }

    function removeTag(value) {
      Array.prototype.forEach.call(picker.querySelectorAll(".tp-tag"), function (tag) {
        if (tag.querySelector("input").value === value) tag.remove();
      });
      layout();
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function choose(i) {
      var option = shown[i];
      if (!option) return;
      addTag(option.label);
      input.value = "";
      render();
    }

    // Splits pasted or typed lists. Locations keep their commas ("Victoria, BC").
    function splitList(text) {
      var separators = kind === "location" ? /[;\n]+/ : /[;,\n]+/;
      return text.split(separators).map(function (s) { return s.trim(); }).filter(Boolean);
    }

    input.addEventListener("focus", function () { box.scrollLeft = 0; if (menu.hidden) render(); });
    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); if (menu.hidden) render(); else { active = Math.min(active + 1, shown.length - 1); highlight(); } }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
      else if (e.key === "Enter") {
        if (!input.value.trim() && menu.hidden) return; // let Enter submit the form
        e.preventDefault();
        if (active >= 0 && !menu.hidden) choose(active);
        else { splitList(input.value).forEach(addTag); input.value = ""; render(); }
      }
      else if ((e.key === "," && kind !== "location") || e.key === ";") {
        if (input.value.trim()) { e.preventDefault(); addTag(input.value); input.value = ""; render(); }
      }
      else if (e.key === "Backspace" && !input.value) {
        var tags = picker.querySelectorAll(".tp-tag");
        if (tags.length) { removeTag(tags[tags.length - 1].querySelector("input").value); render(); }
      }
      else if (e.key === "Escape") { close(); }
      else if (e.key === "Tab") { if (input.value.trim()) { addTag(input.value); input.value = ""; } close(); }
    });
    input.addEventListener("paste", function (e) {
      var text = (e.clipboardData || window.clipboardData).getData("text");
      var parts = splitList(text);
      if (parts.length > 1) { e.preventDefault(); parts.forEach(addTag); render(); }
    });
    // Any press inside the menu -- an option, its scrollbar, or the gaps --
    // must not take focus from the input, or blur would close the menu.
    var pressingMenu = false;
    menu.addEventListener("mousedown", function (e) {
      e.preventDefault();
      pressingMenu = true;
      var selectedRow = e.target.closest("li[data-remove]");
      if (selectedRow) { removeTag(selectedRow.dataset.remove); render(); return; }
      var li = e.target.closest("li[data-index]");
      if (li) choose(+li.dataset.index);
    });
    document.addEventListener("mouseup", function () { pressingMenu = false; });
    box.addEventListener("click", function (e) {
      var remove = e.target.closest(".tp-remove");
      if (remove) {
        removeTag(remove.closest(".tp-tag").querySelector("input").value);
        if (!menu.hidden) render();
        return;
      }
      input.focus();
    });
    input.addEventListener("blur", function () {
      if (pressingMenu) { input.focus(); return; } // e.g. dragging the menu's scrollbar
      // Keep whatever was typed: blurring (or submitting) adds it as a tag.
      if (input.value.trim()) { splitList(input.value).forEach(addTag); input.value = ""; }
      setTimeout(close, 120);
    });
    layout();
    if (window.ResizeObserver) new ResizeObserver(layout).observe(box);
    // Submitting with text still in the box: add it first.
    var form = picker.closest("form");
    if (form) form.addEventListener("submit", function () {
      if (input.value.trim()) { splitList(input.value).forEach(addTag); input.value = ""; }
    });
  }

  function initAll(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll(".tag-picker"), init);
  }

  document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  document.addEventListener("htmx:load", function (e) { initAll(e.target); });
})();
