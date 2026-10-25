/* Hirlumo -- page behaviour without a framework:
 *  - the sign-in / sign-up flow (the "Welcome" dialog and the /login, /signup pages),
 *    talking JSON to /api/auth/*; the session itself is an HttpOnly cookie, so
 *    nothing about it lives in JavaScript or localStorage;
 *  - "sign in to continue" for searches and actions that need an account;
 *  - success messages that fade after a few seconds (with a close button);
 *  - Show/Hide on password fields, account / mobile menus, the job details panel;
 *  - the theme menu (Light / Dark / Match system), kept in a "theme" cookie so the
 *    server renders the page in that theme from the first paint;
 *  - window.hirlumo.access: hasPermission / hasAnyPermission / hasAllPermissions /
 *    canAccessRoute for the signed-in user (UX only -- the server checks every request);
 *  - admin pages: the permission matrix's unsaved-changes state, confirm prompts;
 *  - the review card: asks once after a few minutes of use (counted in localStorage);
 *  - the help assistant: a chat panel answering from the help articles (/help/ask).
 * Everything is attached by event delegation, so it keeps working after htmx swaps.
 */
(function () {
  "use strict";
  document.documentElement.classList.add("js");  // CSS can tell scripts are running

  var signedIn = function () { return document.body && document.body.dataset.auth === "1"; };

  // ---------- theme ----------
  function setTheme(choice) {
    var root = document.documentElement;
    if (choice !== "light" && choice !== "dark") choice = "system";
    if (choice === "system") root.removeAttribute("data-theme");  // follow the device's setting
    else root.setAttribute("data-theme", choice);
    // "system" is remembered too, so it wins over a site default of light or dark.
    document.cookie = "theme=" + choice + "; path=/; max-age=31536000; samesite=lax";
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.setAttribute("aria-checked", button.dataset.themeChoice === (choice || "system") ? "true" : "false");
    });
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-theme-choice]");
    if (!button) return;
    setTheme(button.dataset.themeChoice);
    var menu = button.closest("details");
    if (menu) menu.open = false;
  });

  // ---------- small helpers ----------
  function $(root, sel) { return root.querySelector(sel); }
  function $all(root, sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); }

  function api(path, body) {
    return fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = (data && data.error) || { code: "server", message: "Something went wrong. Please try again." };
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }, function () {
      throw { code: "network", message: "Can't reach the server. Check your connection and try again." };
    });
  }

  function busy(button, on) {
    if (!button) return;
    if (on) {
      button.dataset.label = button.dataset.label || button.innerHTML;
      button.disabled = true;
      button.classList.add("is-loading");
      button.innerHTML = '<span class="spinner" aria-hidden="true"></span>' + button.dataset.label;
    } else {
      button.disabled = false;
      button.classList.remove("is-loading");
      if (button.dataset.label) button.innerHTML = button.dataset.label;
    }
  }

  // ---------- the sign-in flow ----------
  function Flow(root) {
    this.root = root;
    this.email = "";
    this.timers = [];
    root._flow = this;
    var self = this;
    if (root.dataset.mode === "signup") {
      $(root, "[data-title-login]").hidden = true;
      $(root, "[data-title-signup]").hidden = false;
    }
    root.addEventListener("submit", function (e) {
      var form = e.target.closest("[data-form]");
      if (!form) return;
      e.preventDefault();
      self["submit_" + form.dataset.form](form);
    });
    root.addEventListener("click", function (e) {
      var go = e.target.closest("[data-goto]");
      if (go) { e.preventDefault(); self.show(go.dataset.goto); return; }
      var act = e.target.closest("[data-action]");
      if (act) { e.preventDefault(); self["action_" + act.dataset.action.replace("-", "_")](act); }
    });
    this.initOtp();
    this.initStrength();
  }

  Flow.prototype.next = function () { return this.root.dataset.next || "/jobs"; };

  Flow.prototype.show = function (step) {
    var self = this;
    $all(this.root, "[data-step]").forEach(function (s) { s.hidden = s.dataset.step !== step; });
    var progress = $(this.root, "[data-progress]");
    var order = { email: 1, verify: 2, details: 3 };
    progress.hidden = !(step === "verify" || step === "details");
    $all(progress, "[data-progress-step]").forEach(function (el) {
      var n = +el.dataset.progressStep;
      el.classList.toggle("done", n < order[step]);
      el.classList.toggle("current", n === order[step]);
    });
    $all(this.root, "[data-email-label]").forEach(function (el) { el.textContent = self.email; });
    if (step === "password" && this.email) $(this.root, "#af-login-email").value = this.email;
    if (step === "forgot" && this.email) $(this.root, "#af-forgot-email").value = this.email;
    this.clearErrors();
    var section = $(this.root, '[data-step="' + step + '"]');
    var first = section && section.querySelector("input:not([type=hidden]):not([readonly]), a.button, button.primary");
    if (step === "verify") first = $(this.root, "[data-otp]");
    if (first) setTimeout(function () { first.focus(); }, 30);
  };

  Flow.prototype.reset = function (next, reason) {
    if (next) this.root.dataset.next = next;
    var next = encodeURIComponent(this.next());
    $all(this.root, "[data-social]").forEach(function (link) {
      link.href = "/auth/" + link.dataset.social + "/start?next=" + next;
    });
    this.show("email");
  };

  Flow.prototype.clearErrors = function () {
    $all(this.root, ".flow-error").forEach(function (el) { el.hidden = true; el.textContent = ""; });
    $all(this.root, "[data-field-error]").forEach(function (el) { el.hidden = true; el.textContent = ""; });
    $all(this.root, ".has-error").forEach(function (el) { el.classList.remove("has-error"); });
  };

  Flow.prototype.error = function (section, err) {
    var box = $(section, ".flow-error");
    var fields = err && err.fields;
    if (fields) {
      Object.keys(fields).forEach(function (name) {
        var slot = section.querySelector('[data-field-error="' + name + '"]');
        if (slot) { slot.textContent = fields[name]; slot.hidden = false; }
        var input = section.querySelector('[name="' + name + '"]');
        if (input) input.classList.add("has-error");
      });
    }
    if (box) { box.textContent = (err && err.message) || "Something went wrong."; box.hidden = false; }
  };

  Flow.prototype.done = function (data) {
    window.location.assign(data.redirect || this.next());
  };

  Flow.prototype.submit_email = function (form) {
    var self = this, section = form.closest("[data-step]"), input = form.email;
    this.clearErrors();
    var value = input.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) {
      this.error(section, { message: "Enter a valid email address.", fields: { email: "Enter a valid email address." } });
      input.focus();
      return;
    }
    var button = form.querySelector("button[type=submit]");
    busy(button, true);
    api("/api/auth/register/start", { email: value, next: this.next() }).then(function (data) {
      self.email = data.email || value;
      self.startTimers(data.expires_in || 60, data.resend_in || 30);
      self.clearOtp();
      self.show("verify");
    }, function (err) {
      if (err.code === "cooldown" && self.email === value) { self.show("verify"); return; }
      self.error(section, err);
    }).then(function () { busy(button, false); });
  };

  Flow.prototype.submit_password = function (form) {
    var self = this, section = form.closest("[data-step]"), button = form.querySelector("button[type=submit]");
    this.clearErrors();
    this.email = form.email.value.trim();
    busy(button, true);
    api("/api/auth/login", { email: this.email, password: form.password.value, next: this.next() })
      .then(function (data) { self.done(data); }, function (err) {
        self.error(section, err);
        busy(button, false);
      });
  };

  Flow.prototype.action_email_code = function () {
    var email = $(this.root, "#af-login-email").value.trim() || this.email;
    $(this.root, "#af-email").value = email;
    this.show("email");
    if (email) this.submit_email($(this.root, '[data-form="email"]'));
  };

  Flow.prototype.action_resend = function (button) {
    var self = this, section = button.closest("[data-step]");
    this.clearErrors();
    busy(button, true);
    api("/api/auth/resend-otp", { email: this.email }).then(function (data) {
      self.startTimers(data.expires_in || 60, data.resend_in || 30);
      self.clearOtp();
      var box = $(section, ".flow-error");
      box.textContent = "A new code is on its way.";
      box.hidden = false;
      box.classList.add("is-info");
    }, function (err) { self.error(section, err); }).then(function () {
      busy(button, false);
      button.disabled = true;  // the countdown re-enables it
    });
  };

  Flow.prototype.submit_verify = function (form) {
    var self = this, section = form.closest("[data-step]"), button = form.querySelector("button[type=submit]");
    var code = this.otpValue();
    this.clearErrors();
    $(section, ".flow-error").classList.remove("is-info");
    if (code.length !== 6) { this.error(section, { message: "Enter the 6-digit code from the email." }); return; }
    busy(button, true);
    api("/api/auth/verify-email", { email: this.email, code: code, next: this.next() }).then(function (data) {
      if (data.step === "details") { self.stopTimers(); self.show("details"); busy(button, false); return; }
      self.done(data);
    }, function (err) {
      self.error(section, err);
      self.clearOtp();
      busy(button, false);
    });
  };

  Flow.prototype.submit_details = function (form) {
    var self = this, section = form.closest("[data-step]"), button = form.querySelector("button[type=submit]");
    this.clearErrors();
    var problems = {};
    if (!form.first_name.value.trim()) problems.first_name = "Enter your first name.";
    if (!form.last_name.value.trim()) problems.last_name = "Enter your last name.";
    if (form.password.value.length < 8) problems.password = "Use at least 8 characters.";
    if (form.password.value !== form.confirm_password.value) problems.confirm_password = "The passwords don't match.";
    if (Object.keys(problems).length) { this.error(section, { message: "Check the highlighted fields.", fields: problems }); return; }
    busy(button, true);
    api("/api/auth/register/complete", {
      first_name: form.first_name.value, last_name: form.last_name.value, password: form.password.value,
      confirm_password: form.confirm_password.value, next: this.next()
    }).then(function (data) {
      var go = $(self.root, "[data-continue]");
      go.href = data.redirect || self.next();
      self.show("created");
      busy(button, false);
    }, function (err) {
      if (err.code === "registration_expired") { self.show("email"); self.error($(self.root, '[data-step="email"]'), err); }
      else self.error(section, err);
      busy(button, false);
    });
  };

  Flow.prototype.submit_forgot = function (form) {
    var self = this, section = form.closest("[data-step]"), button = form.querySelector("button[type=submit]");
    this.clearErrors();
    busy(button, true);
    api("/api/auth/forgot-password", { email: form.email.value.trim() })
      .then(function () { self.show("forgot-sent"); }, function (err) { self.error(section, err); })
      .then(function () { busy(button, false); });
  };

  // ----- code boxes -----
  Flow.prototype.initOtp = function () {
    var boxes = $all(this.root, "[data-otp]"), self = this;
    if (!boxes.length) return;
    boxes.forEach(function (box, i) {
      box.addEventListener("input", function () {
        var digits = box.value.replace(/\D/g, "");
        if (digits.length > 1) { self.fillOtp(digits, i); return; }
        box.value = digits;
        if (digits && i < boxes.length - 1) boxes[i + 1].focus();
        if (self.otpValue().length === 6) self.submit_verify($(self.root, '[data-form="verify"]'));
      });
      box.addEventListener("keydown", function (e) {
        if (e.key === "Backspace" && !box.value && i > 0) { boxes[i - 1].focus(); boxes[i - 1].value = ""; e.preventDefault(); }
        else if (e.key === "ArrowLeft" && i > 0) { boxes[i - 1].focus(); e.preventDefault(); }
        else if (e.key === "ArrowRight" && i < boxes.length - 1) { boxes[i + 1].focus(); e.preventDefault(); }
      });
      box.addEventListener("paste", function (e) {
        var text = (e.clipboardData || window.clipboardData).getData("text") || "";
        e.preventDefault();
        self.fillOtp(text.replace(/\D/g, ""), 0);
      });
      box.addEventListener("focus", function () { box.select(); });
    });
  };
  Flow.prototype.fillOtp = function (digits, from) {
    var boxes = $all(this.root, "[data-otp]");
    for (var i = 0; i < digits.length && from + i < boxes.length; i++) boxes[from + i].value = digits[i];
    var next = Math.min(from + digits.length, boxes.length - 1);
    boxes[next].focus();
    if (this.otpValue().length === 6) this.submit_verify($(this.root, '[data-form="verify"]'));
  };
  Flow.prototype.otpValue = function () { return $all(this.root, "[data-otp]").map(function (b) { return b.value; }).join(""); };
  Flow.prototype.clearOtp = function () {
    $all(this.root, "[data-otp]").forEach(function (b) { b.value = ""; });
    var first = $(this.root, "[data-otp]");
    if (first && !$(this.root, '[data-step="verify"]').hidden) first.focus();
  };

  // ----- countdown + resend -----
  Flow.prototype.stopTimers = function () { this.timers.forEach(clearInterval); this.timers = []; };
  Flow.prototype.startTimers = function (expiresIn, resendIn) {
    var self = this, label = $(this.root, "[data-countdown]"), resend = $(this.root, '[data-action="resend"]');
    var expiresAt = Date.now() + expiresIn * 1000, resendAt = Date.now() + resendIn * 1000;
    this.stopTimers();
    function tick() {
      var left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
      var wait = Math.max(0, Math.round((resendAt - Date.now()) / 1000));
      label.textContent = left > 0 ? "Code expires in " + Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2)
                                   : "Code expired. Request a new one.";
      label.classList.toggle("expired", left === 0);
      if (!resend.classList.contains("is-loading")) {
        resend.disabled = wait > 0;
        resend.textContent = wait > 0 ? "Resend code in " + wait + "s" : "Resend code";
      }
      if (left === 0 && wait === 0) self.stopTimers();
    }
    tick();
    this.timers.push(setInterval(tick, 1000));
  };

  // ----- password strength -----
  Flow.prototype.initStrength = function () {
    var input = $(this.root, "#af-password");
    if (!input) return;
    var bar = $(this.root, "[data-strength-bar]"), text = $(this.root, "[data-strength-text]");
    input.addEventListener("input", function () {
      var v = input.value, kinds = 0;
      [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].forEach(function (r) { if (r.test(v)) kinds++; });
      var score = v.length < 8 ? 0 : (v.length >= 16 ? 3 : (v.length >= 12 || kinds >= 3 ? 2 : 1));
      var labels = ["Too short -- at least 8 characters", "Fair -- longer is stronger", "Good", "Strong"];
      bar.style.width = v ? [25, 50, 75, 100][score] + "%" : "0";
      bar.className = "s" + score;
      text.textContent = v ? labels[score] : "At least 8 characters.";
    });
  };

  // ---------- the dialog ----------
  var lastTrigger = null;
  function dialog() { return document.getElementById("auth-dialog"); }

  function openAuth(next, reason) {
    var d = dialog();
    if (!d) { window.location.assign("/login?next=" + encodeURIComponent(next || "/jobs")); return; }
    var flow = d.querySelector("[data-auth-flow]");
    if (!flow._flow) new Flow(flow);
    flow._flow.reset(next || "/jobs");
    var label = d.querySelector("[data-dialog-reason]");
    if (label) label.textContent = reason || "Sign in or create a free account to continue.";
    lastTrigger = document.activeElement;
    if (typeof d.showModal === "function") d.showModal(); else d.setAttribute("open", "");
    setTimeout(function () { var i = d.querySelector("#af-email"); if (i) i.focus(); }, 30);
  }
  window.openAuth = openAuth;

  document.addEventListener("click", function (e) {
    var close = e.target.closest("[data-dialog-close]");
    if (close) { var d = close.closest("dialog"); if (d) d.close(); return; }
    var d2 = dialog();
    if (d2 && e.target === d2) { d2.close(); return; }  // click on the backdrop

  });
  // Capture phase, so this runs before htmx's own link handling.
  document.addEventListener("click", function (e) {
    var trigger = e.target.closest("[data-auth-open]");
    if (trigger && !signedIn()) {
      e.preventDefault();
      e.stopPropagation();
      openAuth(trigger.dataset.next || trigger.getAttribute("href") || "/jobs", trigger.dataset.reason);
    }
  }, true);
  document.addEventListener("close", function (e) {
    if (e.target === dialog() && lastTrigger && lastTrigger.focus) lastTrigger.focus();
  }, true);

  // A search typed while signed out: keep it, ask to sign in, then run it.
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("[data-auth-search]");
    if (!form || signedIn()) return;
    e.preventDefault();
    e.stopPropagation();
    var params = new URLSearchParams(new FormData(form));
    openAuth(form.getAttribute("action") + "?" + params.toString(), "Sign in or create a free account to see these jobs.");
  }, true);

  // ---------- Show / Hide password ----------
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-password-toggle]");
    if (!b) return;
    var input = document.getElementById(b.dataset.passwordToggle);
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    b.textContent = show ? "Hide" : "Show";
    b.setAttribute("aria-pressed", show ? "true" : "false");
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
  });

  // ---------- menus: close on outside click / Escape ----------
  document.addEventListener("click", function (e) {
    $all(document, "details[data-dropdown][open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    $all(document, "details[data-dropdown][open]").forEach(function (d) { d.removeAttribute("open"); d.querySelector("summary").focus(); });
    var panel = document.querySelector(".results-layout.show-detail");
    if (panel) panel.classList.remove("show-detail");
  });

  // ---------- job results: details panel ----------
  document.addEventListener("click", function (e) {
    var view = e.target.closest("[data-view-job]");
    if (!view) {
      var back = e.target.closest("[data-detail-close]");
      if (back) { e.preventDefault(); back.closest(".results-layout").classList.remove("show-detail"); }
      return;
    }
    var ref = view.dataset.viewJob, tpl = document.getElementById("detail-" + ref);
    var panel = document.getElementById("job-detail");
    if (!tpl || !panel) return;  // no script data: follow the link
    e.preventDefault();
    panel.innerHTML = tpl.innerHTML;
    if (window.htmx) window.htmx.process(panel);
    $all(document, ".result-card.selected").forEach(function (c) { c.classList.remove("selected"); });
    var card = view.closest(".result-card") || document.querySelector('.result-card[data-ref="' + ref + '"]');
    if (card) card.classList.add("selected");
    var layout = panel.closest(".results-layout");
    if (layout) layout.classList.add("show-detail");
    panel.scrollTop = 0;
    var url = new URL(window.location.href);
    url.searchParams.set("view", ref);
    history.replaceState(history.state, "", url.toString());
    var heading = panel.querySelector("h2");
    if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus({ preventScroll: true }); }
  });

  // ---------- permissions (for showing and hiding things; never for security) ----------
  var access = { roles: [], permissions: [], routes: {} };
  function loadAccess() {
    var el = document.getElementById("access-state");
    if (!el) { access = { roles: [], permissions: [], routes: {} }; return; }
    try { access = JSON.parse(el.textContent); } catch (err) { /* keep the last state */ }
  }
  function hasPermission(name) { return access.permissions.indexOf(name) !== -1; }
  function hasAnyPermission(names) { return names.some(hasPermission); }
  function hasAllPermissions(names) { return names.every(hasPermission); }
  function canAccessRoute(path) { return access.routes[path] === true; }
  function applyAccess(root) {
    // <x data-requires-permission="a b"> shows only to users holding all of them.
    $all(root || document, "[data-requires-permission]").forEach(function (el) {
      el.hidden = !hasAllPermissions(el.dataset.requiresPermission.split(/\s+/).filter(Boolean));
    });
  }
  // Roles can change while a page is open (an admin edits them): re-read them when
  // the tab comes back, and reload the page so its menus match.
  function refreshAccess() {
    if (!signedIn() || document.visibilityState !== "visible") return;
    fetch("/api/auth/me", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (me) {
        if (!me) return;
        if (!me.authenticated) { window.location.reload(); return; }
        var before = access.permissions.slice().sort().join(" ");
        if ((me.permissions || []).slice().sort().join(" ") !== before && !document.querySelector("form[data-dirty]")) {
          window.location.reload();
        }
      })
      .catch(function () { /* offline: try again next time */ });
  }
  document.addEventListener("visibilitychange", refreshAccess);
  window.hirlumo = Object.assign(window.hirlumo || {}, {
    access: { hasPermission: hasPermission, hasAnyPermission: hasAnyPermission,
              hasAllPermissions: hasAllPermissions, canAccessRoute: canAccessRoute, refresh: refreshAccess,
              get roles() { return access.roles.slice(); }, get permissions() { return access.permissions.slice(); } }
  });

  // ---------- job search filters: apply on change (desktop) ----------
  // Filters narrow results already fetched, so applying them is free. On wide
  // screens they apply as soon as one changes; in the phone drawer the user
  // picks several and taps "Apply filters" (submitting would close the drawer).
  var filterTimer = null;
  var wideScreen = window.matchMedia("(min-width: 821px)");
  document.addEventListener("change", function (e) {
    var form = e.target.closest && e.target.closest("form[data-auto-apply]");
    if (!form || !wideScreen.matches) return;
    clearTimeout(filterTimer);  // ticking several boxes quickly makes one request
    filterTimer = setTimeout(function () {
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }, 250);
  });

  // ---------- admin: jump selects, confirm prompts, the permission matrix ----------
  document.addEventListener("change", function (e) {
    var select = e.target.closest && e.target.closest("select[data-nav-select]");
    if (select && select.value) window.location.href = select.value;
  });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest && e.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  function matrixState(form) {
    var boxes = $all(form, 'input[type="checkbox"][name="permissions"]');
    var changed = boxes.filter(function (b) { return (b.checked ? "1" : "0") !== b.dataset.initial; }).length;
    var on = boxes.filter(function (b) { return b.checked; }).length;
    var status = $(form, "[data-matrix-status]"), count = $(form, "[data-matrix-count]");
    if (count) count.textContent = on;
    if (status) status.textContent = changed ? changed + " unsaved change" + (changed === 1 ? "" : "s") + "." : "No unsaved changes.";
    if (changed) form.setAttribute("data-dirty", ""); else form.removeAttribute("data-dirty");
  }
  document.addEventListener("change", function (e) {
    var form = e.target.closest && e.target.closest("form[data-matrix]");
    if (form) matrixState(form);
  });
  document.addEventListener("reset", function (e) {
    var form = e.target.closest && e.target.closest("form[data-matrix]");
    if (form) setTimeout(function () { matrixState(form); }, 0);  // after the browser resets the boxes
  }, true);
  document.addEventListener("submit", function (e) {
    var form = e.target.closest && e.target.closest("form[data-matrix]");
    if (form) form.removeAttribute("data-dirty");
  });
  window.addEventListener("beforeunload", function (e) {
    if (document.querySelector("form[data-matrix][data-dirty]")) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---------- success messages ----------
  // A green ".flash" confirmation ("You've signed out.") fades after a few seconds,
  // waits while the pointer or keyboard focus is on it, and has a close button.
  // One marked data-persist (why you were signed out, on /login) stays. Either
  // way "?msg=" leaves the address bar, so a refresh doesn't show it again.
  var FLASH_MS = 5000;
  function dismissFlash(el) {
    if (el._gone) return;
    el._gone = true;
    el.classList.add("is-leaving");
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setTimeout(function () { el.remove(); }, reduce ? 0 : 300);
  }
  function setupFlash(el) {
    if (el._flash) return;
    el._flash = true;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "flash-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", function () { dismissFlash(el); });
    el.appendChild(close);
    if (el.hasAttribute("data-persist")) return;
    var timer = null, left = FLASH_MS, started = 0;
    function run() { started = Date.now(); timer = setTimeout(function () { dismissFlash(el); }, left); }
    function hold() { if (timer) { clearTimeout(timer); timer = null; left = Math.max(left - (Date.now() - started), 1500); } }
    el.addEventListener("mouseenter", hold);
    el.addEventListener("focusin", hold);
    el.addEventListener("mouseleave", function () { if (!timer && !el.contains(document.activeElement)) run(); });
    el.addEventListener("focusout", function () { if (!timer && !el.matches(":hover")) run(); });
    run();
  }
  function initFlashes() {
    var flashes = $all(document, ".flash");
    flashes.forEach(setupFlash);
    if (flashes.length && window.URLSearchParams && window.history.replaceState) {
      var params = new URLSearchParams(window.location.search);
      if (params.has("msg")) {
        params.delete("msg");
        var query = params.toString();
        window.history.replaceState(window.history.state, "",
          window.location.pathname + (query ? "?" + query : "") + window.location.hash);
      }
    }
  }

  // ---------- countdowns: how long the server really keeps something ----------
  // [data-hold-seconds="<seconds>"] ticks [data-hold-clock] down as m:ss; at zero it
  // hides [data-hold-live] and shows [data-hold-done]. The seconds come from
  // the server, so the clock never runs ahead of (or behind) what's actually held.
  function initCountdowns() {
    $all(document, "[data-hold-seconds]").forEach(function (el) {
      if (el._holdTimer) return;
      var ends = Date.now() + (parseInt(el.dataset.holdSeconds, 10) || 0) * 1000;
      function tick() {
        var left = Math.max(0, Math.round((ends - Date.now()) / 1000));
        $all(el, "[data-hold-clock]").forEach(function (clock) {
          clock.textContent = Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2);
        });
        if (left > 0) return;
        clearInterval(el._holdTimer);
        $all(el, "[data-hold-live]").forEach(function (part) { part.hidden = true; });
        $all(el, "[data-hold-done]").forEach(function (part) { part.hidden = false; });
        $all(document, "[data-hold-gone]").forEach(function (part) { part.hidden = true; });
      }
      el._holdTimer = setInterval(tick, 1000);
      tick();
    });
  }

  // ---------- init flows on the page (login / signup pages) ----------
  // The free resume check: ticks through its steps over about six seconds
  // before sending the form. The steps are worded indirectly, on purpose.
  var CHECK_WAIT_MS = 6000;
  function initCheckSteps() {
    $all(document, "form[data-check-steps]").forEach(function (form) {
      if (form._checkSteps) return;
      form._checkSteps = true;
      var list = form.querySelector(".check-steps");
      var button = form.querySelector("button[type=submit]");
      function reset() {
        if (list) list.hidden = true;
        $all(document, "[data-check-results]").forEach(function (el) { el.hidden = false; });
        $all(form, ".check-steps li").forEach(function (li) { li.className = ""; });
        button.disabled = false;
        button.lastChild.textContent = " Check my resume";
      }
      // Back from the results restores this page from cache mid-animation.
      window.addEventListener("pageshow", function (event) { if (event.persisted) reset(); });
      form.addEventListener("submit", function (event) {
        if (!list) return;
        event.preventDefault();
        if (button.disabled) return;
        button.disabled = true;
        button.lastChild.textContent = " Checking…";
        var steps = $all(form, ".check-steps li");
        var each = CHECK_WAIT_MS / steps.length;
        // A new resume: the last one's results go, so they can't be mistaken for these.
        $all(document, "[data-check-results]").forEach(function (el) { el.hidden = true; });
        list.hidden = false;
        steps.forEach(function (li, i) {
          setTimeout(function () {
            if (i > 0) steps[i - 1].className = "is-done";
            li.className = "is-active";
          }, i * each);
        });
        setTimeout(function () {
          steps[steps.length - 1].className = "is-done";
          form.submit();  // submit() skips this handler
        }, CHECK_WAIT_MS);
      });
    });
  }

  // ---------- analysis and tailoring: progress while it works ----------
  // A form (or its submit button) with data-work="<kind>" is sent in the
  // background while a panel shows the steps the server is going through; the
  // result page opens when it's done. Each step is something the request really
  // does (app/modules/matching/workflows.py). They advance over at least
  // PROGRESS_MIN_MS, so a quick answer doesn't flash past, and the last one
  // stays active until the answer arrives -- nothing waits longer than the work.
  var PROGRESS_MIN_MS = 5000, PROGRESS_SLOW_MS = 15000;
  var PROGRESS = {
    assess: { title: "Analyzing your match", hash: "#fit", steps: [
      "Getting the full job description", "Reading your resume",
      "Matching your skills and experience to the requirements", "Finding your strengths and gaps",
      "Preparing your match report"] },
    browse: { title: "Analyzing your match", hash: "#fit", steps: [
      "Saving the job to your list", "Getting the full job description",
      "Matching your skills and experience to the requirements", "Finding your strengths and gaps",
      "Preparing your match report"] },
    tailor: { title: "Analyzing and tailoring", hash: "#resume", slow: "Tailoring a resume can take up to a minute.", steps: [
      "Getting the full job description", "Checking your filters and match score",
      "Tailoring your resume, if it reaches your minimum score", "Checking every rewritten line",
      "Creating your Word and PDF files"] },
    force: { title: "Tailoring your resume", hash: "#resume", slow: "Tailoring a resume can take up to a minute.", steps: [
      "Getting the full job description", "Matching your resume to the requirements",
      "Rewriting your resume for this job", "Checking every rewritten line", "Creating your Word and PDF files"] },
    retailor: { title: "Re-tailoring your resume", hash: "#resume", slow: "Tailoring a resume can take up to a minute.", steps: [
      "Reading the job description", "Matching your resume to the requirements",
      "Rewriting your resume for this job", "Checking every rewritten line", "Creating your Word and PDF files"] }
  };
  var progressBusy = false;

  function progressPanel(spec, jobTitle) {
    var old = document.getElementById("work-progress");
    if (old) old.remove();
    var box = document.createElement("div");
    box.id = "work-progress";
    box.className = "work-progress";
    box.innerHTML = '<div class="work-card" role="status" aria-live="polite"><h2></h2><p class="muted work-job"></p>' +
      '<ol class="check-steps"></ol><p class="muted work-note" hidden></p>' +
      '<div class="work-error" hidden><p></p><button type="button" class="pill outline" data-work-close>Close</button></div></div>';
    box.querySelector("h2").textContent = spec.title;
    var job = box.querySelector(".work-job");
    if (jobTitle) job.textContent = jobTitle; else job.remove();
    var list = box.querySelector("ol");
    spec.steps.forEach(function (text) { var li = document.createElement("li"); li.textContent = text; list.appendChild(li); });
    document.body.appendChild(box);
    document.body.classList.add("work-open");
    return box;
  }
  function closeProgress() {
    var box = document.getElementById("work-progress");
    if (box) box.remove();
    document.body.classList.remove("work-open");
    progressBusy = false;
  }

  // The result page: the same page (a job's own actions) is swapped in place --
  // like the site's other page changes -- so it isn't loaded twice; another
  // page (a search result's "Analyze match") opens normally.
  function showResult(response, html, hash) {
    var target = new URL(response.url, window.location.href);
    target.hash = hash || "";
    var samePage = target.pathname + target.search === window.location.pathname + window.location.search ||
                   target.pathname === window.location.pathname;
    if (!samePage) { window.location.assign(target.href); return; }
    var doc = new DOMParser().parseFromString(html, "text/html");
    if (!doc.body) { window.location.assign(target.href); return; }
    document.title = doc.title;
    Array.prototype.slice.call(document.body.attributes).forEach(function (a) { document.body.removeAttribute(a.name); });
    Array.prototype.slice.call(doc.body.attributes).forEach(function (a) { document.body.setAttribute(a.name, a.value); });
    document.body.innerHTML = doc.body.innerHTML;
    history.replaceState(history.state, "", target.href);
    progressBusy = false;
    if (window.htmx) window.htmx.process(document.body);
    init();
    var anchor = hash && document.getElementById(hash.slice(1));
    if (anchor) anchor.scrollIntoView(); else window.scrollTo(0, 0);
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var button = event.submitter || null;
    var kind = (button && button.dataset.work) || form.dataset.work;
    var spec = kind && PROGRESS[kind];
    if (!spec || !window.fetch || !window.DOMParser || event.defaultPrevented) return;
    if (!form.checkValidity()) return;  // the browser says what's missing
    event.preventDefault();
    if (progressBusy) return;
    progressBusy = true;

    var data = new FormData(form);
    if (button && button.name) data.append(button.name, button.value);
    var titleEl = document.querySelector("#job-detail h2, .job-header h1, main h1");
    var box = progressPanel(spec, titleEl ? titleEl.textContent.trim() : "");
    var steps = $all(box, ".check-steps li");
    var each = PROGRESS_MIN_MS / steps.length;
    var timers = steps.map(function (li, i) {
      return setTimeout(function () {
        if (i > 0) steps[i - 1].className = "is-done";
        li.className = "is-active";
      }, i * each);
    });
    var slowTimer = spec.slow ? setTimeout(function () {
      var note = $(box, ".work-note"); note.textContent = "Still working. " + spec.slow; note.hidden = false;
    }, PROGRESS_SLOW_MS) : null;
    var minimum = new Promise(function (resolve) { setTimeout(resolve, PROGRESS_MIN_MS); });

    var request = fetch(form.action, { method: "POST", body: data, credentials: "same-origin" })
      .then(function (response) {
        return response.text().then(function (html) { return { response: response, html: html }; });
      });
    Promise.all([request, minimum]).then(function (results) {
      var got = results[0];
      if (!got.response.ok) throw new Error("HTTP " + got.response.status);
      timers.forEach(clearTimeout);
      if (slowTimer) clearTimeout(slowTimer);
      steps.forEach(function (li) { li.className = "is-done"; });
      setTimeout(function () { closeProgress(); showResult(got.response, got.html, spec.hash); }, 350);
    }).catch(function () {
      timers.forEach(clearTimeout);
      if (slowTimer) clearTimeout(slowTimer);
      $all(box, ".check-steps li.is-active").forEach(function (li) { li.className = ""; });
      var err = $(box, ".work-error");
      $(err, "p").textContent = "That didn't finish -- check your connection and try again. Nothing was lost.";
      err.hidden = false;
      $(err, "button").focus();
      progressBusy = false;
    });
  });
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-work-close]")) closeProgress();
  });
  // Back to a page restored from the browser's cache mid-work: no stale panel.
  window.addEventListener("pageshow", function (event) { if (event.persisted) closeProgress(); });

  // Choosing resume changes one by one (resume_canadian.html): a fix's own tick
  // box ticks all its changes, the toolbar ticks all, none, or a resume section's.
  var CHANGE_BOXES = 'input[name="change"], input[name="fix"]';
  function syncChanges(form) {
    $all(form, "[data-change-group]").forEach(function (group) {
      var toggle = $(group, "[data-group-toggle]");
      var boxes = $all(group, 'input[name="change"]');
      if (!toggle || !boxes.length) return;
      var on = boxes.filter(function (b) { return b.checked; }).length;
      toggle.checked = on === boxes.length;
      toggle.indeterminate = on > 0 && on < boxes.length;
    });
    $all(form, "[data-select-section]").forEach(function (chip) {
      var mine = $all(form, CHANGE_BOXES).filter(function (b) { return b.dataset.section === chip.dataset.selectSection; });
      var on = mine.filter(function (b) { return b.checked; }).length;
      chip.setAttribute("aria-pressed", on > 0 && on === mine.length ? "true" : "false");
    });
    var count = $all(form, CHANGE_BOXES).filter(function (b) { return b.checked; }).length;
    $all(form, "[data-change-count]").forEach(function (el) { el.textContent = count; });
  }
  document.addEventListener("change", function (event) {
    var form = event.target.closest && event.target.closest("[data-change-form]");
    if (!form) return;
    if (event.target.matches("[data-group-toggle]")) {
      $all(event.target.closest("[data-change-group]"), 'input[name="change"]').forEach(function (b) {
        b.checked = event.target.checked;
      });
    }
    syncChanges(form);
  });
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-select], [data-select-section]");
    var form = button && button.closest("[data-change-form]");
    if (!form) return;
    var boxes = $all(form, CHANGE_BOXES);
    if (button.dataset.select) {
      boxes.forEach(function (b) { b.checked = button.dataset.select === "all"; });
    } else {
      var on = button.getAttribute("aria-pressed") !== "true";
      boxes.forEach(function (b) { if (b.dataset.section === button.dataset.selectSection) b.checked = on; });
    }
    syncChanges(form);
  });
  function initChangeForms() {
    $all(document, "[data-change-form]").forEach(function (form) {
      $all(form, "[data-change-toolbar]").forEach(function (bar) { bar.hidden = false; });
      syncChanges(form);
    });
  }

  // Plans (/plans): arrows, dots and arrow keys move between plan cards; swiping scrolls natively.
  function initPlans() {
    $all(document, "[data-plans]").forEach(function (root) {
      if (root._plans) return;
      root._plans = true;
      var track = root.querySelector("[data-plans-track]");
      var cards = $all(root, "[data-plan]");
      var prev = root.querySelector("[data-plans-prev]"), next = root.querySelector("[data-plans-next]");
      var dots = $all(root, "[data-plans-dot]");
      if (!track || cards.length < 2) return;
      function current() { return Math.round(track.scrollLeft / Math.max(1, track.clientWidth)); }
      function go(i) {
        i = Math.max(0, Math.min(cards.length - 1, i));
        track.scrollTo({ left: cards[i].offsetLeft - track.offsetLeft, behavior: "smooth" });
      }
      function sync() {
        var i = current();
        if (prev) prev.disabled = i <= 0;
        if (next) next.disabled = i >= cards.length - 1;
        dots.forEach(function (d, n) { d.setAttribute("aria-selected", n === i ? "true" : "false"); });
      }
      if (prev) prev.addEventListener("click", function () { go(current() - 1); });
      if (next) next.addEventListener("click", function () { go(current() + 1); });
      dots.forEach(function (d) { d.addEventListener("click", function () { go(+d.dataset.plansDot); }); });
      track.addEventListener("keydown", function (e) {
        if (e.key === "ArrowLeft") { e.preventDefault(); go(current() - 1); }
        if (e.key === "ArrowRight") { e.preventDefault(); go(current() + 1); }
      });
      track.addEventListener("scroll", function () { window.requestAnimationFrame(sync); }, { passive: true });
      sync();
    });
  }

  // Review card (app/modules/feedback): comes up by itself after a few minutes
  // of real use -- the tab in view and someone clicking, typing or scrolling in
  // the last minute -- counted across pages in localStorage (a convenience: if
  // storage is blocked it just counts this page). "Rate Hirlumo" opens it any time.
  var REVIEW_TICK = 15, REVIEW_IDLE = 60;
  var reviewLastInput = Date.now(), reviewTimer = null, reviewMemory = {};
  ["pointerdown", "keydown", "scroll", "touchstart"].forEach(function (name) {
    document.addEventListener(name, function () { reviewLastInput = Date.now(); }, { passive: true, capture: true });
  });
  function reviewCard() { return document.getElementById("review-card"); }
  function reviewKey(card) { return "hirlumo.review." + (card.dataset.user || ""); }
  function reviewStore(card, value) {
    var key = reviewKey(card);
    try {
      if (value === undefined) return Number(window.localStorage.getItem(key)) || reviewMemory[key] || 0;
      window.localStorage.setItem(key, String(value));
    } catch (e) { /* storage blocked: keep it for this page only */ }
    if (value === undefined) return reviewMemory[key] || 0;
    reviewMemory[key] = value;
    return value;
  }
  function helpIsOpen() {
    var panel = document.getElementById("help-chat");
    return !!panel && !panel.hidden;
  }
  function showReview(card, manual) {
    if (manual && helpIsOpen()) closeHelp();  // they share the corner: one at a time
    card.hidden = false;
    card.classList.add("open");
    document.body.classList.add("review-open");
    var error = card.querySelector("[data-review-error]");
    if (error) error.hidden = true;
    if (manual) { var first = card.querySelector("input[type=radio]"); if (first) first.focus(); }
  }
  function hideReview(card) {
    card.classList.remove("open");
    card.hidden = true;
    document.body.classList.remove("review-open");
  }
  function reviewPost(url, data) {
    return fetch(url, { method: "POST", body: data, credentials: "same-origin",
                        headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); });
  }
  function reviewTick() {
    var card = reviewCard();
    if (!card || card.dataset.auto !== "1" || !card.hidden || helpIsOpen()) return;  // never over the help chat
    if (document.visibilityState !== "visible" || Date.now() - reviewLastInput > REVIEW_IDLE * 1000) return;
    var seconds = reviewStore(card) + REVIEW_TICK;
    reviewStore(card, seconds);
    if (seconds >= (Number(card.dataset.minutes) || 5) * 60) showReview(card, false);
  }
  function postponeReview(card, never) {
    var data = new FormData();
    if (never) data.append("never", "1");
    var page = card.querySelector("input[name=page]");
    if (page) data.append("page", page.value);
    card.dataset.auto = "0";
    reviewStore(card, 0);  // "later": another few minutes of use once it's due again
    hideReview(card);
    reviewPost("/feedback/later", data).catch(function () {});
  }
  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var open = event.target.closest("[data-review-open]");
    var card = reviewCard();
    if (open && card) {
      var menu = open.closest("details");
      if (menu) menu.open = false;
      var form = card.querySelector("[data-review-form]"), thanks = card.querySelector("[data-review-thanks]");
      if (form) form.hidden = false;
      if (thanks) thanks.hidden = true;
      showReview(card, true);
      return;
    }
    if (!card || !card.contains(event.target)) return;
    if (event.target.closest("[data-review-later]")) postponeReview(card, false);
    else if (event.target.closest("[data-review-never]")) postponeReview(card, true);
  });
  document.addEventListener("keydown", function (event) {
    var card = reviewCard();
    if (event.key === "Escape" && card && !card.hidden && card.contains(document.activeElement)) postponeReview(card, false);
  });
  document.addEventListener("submit", function (event) {
    var form = event.target.closest && event.target.closest("[data-review-form]");
    if (!form) return;
    event.preventDefault();
    var card = reviewCard(), error = form.querySelector("[data-review-error]");
    var button = form.querySelector("button[type=submit]");
    if (button) button.disabled = true;
    reviewPost(form.action, new FormData(form)).then(function (answer) {
      if (button) button.disabled = false;
      if (!answer.ok) {
        if (error) { error.textContent = answer.error || "That didn't go through. Please try again."; error.hidden = false; }
        return;
      }
      card.dataset.auto = "0";
      form.hidden = true;
      form.reset();
      var thanks = card.querySelector("[data-review-thanks]");
      if (thanks) thanks.hidden = false;
      window.setTimeout(function () { hideReview(card); }, 4000);  // the thank-you fades on its own
    }).catch(function () {
      if (button) button.disabled = false;
      if (error) { error.textContent = "That didn't go through. Please check your connection and try again."; error.hidden = false; }
    });
  });
  function initReview() {
    if (!reviewTimer && reviewCard()) reviewTimer = window.setInterval(reviewTick, REVIEW_TICK * 1000);
  }

  // Help assistant (app/modules/help): questions go to /help/ask, which answers
  // from the help articles. The conversation is kept in sessionStorage so it
  // carries across pages (a convenience: without storage it lasts the page).
  // It belongs to whoever was signed in: once that account signs out (or another
  // signs in) it's wiped, and so is any conversation left unused for HELP_IDLE_MS,
  // signed in or not, so the next person on a shared computer can't read it.
  var HELP_KEY = "hirlumo.help", HELP_IDLE_MS = 30 * 60 * 1000, helpMemory = null, helpTimer = null;
  function freshHelp() { return { log: [], open: false, context: "", at: Date.now() }; }
  function helpPanel() { return document.getElementById("help-chat"); }
  function helpState() {
    if (!helpMemory) {
      try { helpMemory = JSON.parse(window.sessionStorage.getItem(HELP_KEY) || "null"); } catch (e) { helpMemory = null; }
    }
    if (!helpMemory || !Array.isArray(helpMemory.log)) helpMemory = freshHelp();
    var panel = helpPanel(), owner = panel ? panel.dataset.owner || "" : "";
    var idle = helpMemory.log.length && Date.now() - (helpMemory.at || 0) > HELP_IDLE_MS;
    if (idle || (helpMemory.owner && helpMemory.owner !== owner)) {
      helpMemory = freshHelp();  // unused for a while, signed out, or someone else signed in
      try { window.sessionStorage.removeItem(HELP_KEY); } catch (e) { /* nothing kept */ }
    }
    if (owner) helpMemory.owner = owner;  // a visitor who signs in keeps their conversation
    return helpMemory;
  }
  function touchHelp() { helpState().at = Date.now(); }
  function saveHelp() {
    var state = helpState();
    state.log = state.log.slice(-30);
    try { window.sessionStorage.setItem(HELP_KEY, JSON.stringify(state)); } catch (e) { /* this page only */ }
  }
  function helpBubble(entry) {
    var item = document.createElement("div");
    item.className = "help-msg " + (entry.who === "me" ? "me" : "bot");
    var body = document.createElement("div");
    body.className = "help-msg-body";
    if (entry.who === "me") body.textContent = entry.text;
    else body.innerHTML = entry.html;  // our own article text, escaped on the server
    item.appendChild(body);
    if (entry.source) {
      var from = document.createElement("a");
      from.className = "help-source";
      from.href = entry.source.url;
      from.textContent = "From " + entry.source.article + (entry.source.heading ? " › " + entry.source.heading : "") + " →";
      item.appendChild(from);
    }
    return item;
  }
  function helpChips(list, label) {
    var box = helpPanel() && helpPanel().querySelector("[data-help-suggest]");
    if (!box) return;
    box.innerHTML = "";
    if (!list || !list.length) return;
    if (label) { var l = document.createElement("small"); l.textContent = label; box.appendChild(l); }
    list.forEach(function (text) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "chip-button"; b.dataset.helpAsk = text; b.textContent = text;
      box.appendChild(b);
    });
  }
  function renderHelp() {
    var panel = helpPanel();
    if (!panel) return;
    var log = panel.querySelector("[data-help-log]");
    log.innerHTML = "";
    helpState().log.forEach(function (entry) { log.appendChild(helpBubble(entry)); });
    var last = helpState().chips;
    helpChips(last ? last.list : [], last ? last.label : "");
    log.scrollTop = log.scrollHeight;
  }
  function openHelp(focus) {
    var panel = helpPanel();
    if (!panel) return;
    var review = reviewCard();
    if (review && !review.hidden) { hideReview(review); review.dataset.waiting = "1"; }  // back when the chat closes
    panel.hidden = false;
    panel.classList.add("open");
    $all(document, "[data-help-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", "true"); });
    var state = helpState();
    state.open = true;
    touchHelp();
    if (!state.log.length) {
      state.log.push({ who: "bot", html: "<p>Hi! I can explain how anything in Hirlumo works. Ask a question, or pick one below.</p>" });
      fetch("/help/ask/suggestions?page=" + encodeURIComponent(location.pathname), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) { state.chips = { list: data.suggestions || [], label: "" }; saveHelp(); renderHelp(); })
        .catch(function () {});
    }
    saveHelp();
    renderHelp();
    if (focus !== false) { var input = panel.querySelector("input[name=question]"); if (input) input.focus(); }
  }
  function closeHelp() {
    var panel = helpPanel();
    if (!panel) return;
    panel.classList.remove("open");
    panel.hidden = true;
    $all(document, "[data-help-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    helpState().open = false;
    saveHelp();
    var review = reviewCard();
    if (review && review.dataset.waiting === "1") { delete review.dataset.waiting; showReview(review, false); }
  }
  function askHelp(question) {
    question = (question || "").trim();
    var panel = helpPanel();
    if (!question || !panel) return;
    var state = helpState();
    touchHelp();
    state.log.push({ who: "me", text: question });
    state.chips = null;
    renderHelp();
    var log = panel.querySelector("[data-help-log]");
    var typing = document.createElement("div");
    typing.className = "help-msg bot typing";
    typing.innerHTML = '<div class="help-msg-body"><span></span><span></span><span></span></div>';
    log.appendChild(typing);
    log.scrollTop = log.scrollHeight;
    var data = new FormData();
    data.append("question", question);
    data.append("context", state.context || "");
    data.append("page", location.pathname);
    fetch("/help/ask", { method: "POST", body: data, credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (answer) {
        touchHelp();
        state.log.push({ who: "bot", html: answer.html, source: answer.source || null });
        if (answer.source) state.context = answer.source.slug;
        if (answer.related && answer.related.length) {
          state.chips = { list: answer.related.map(function (s) { return s.heading; }), label: "Related" };
        } else {
          state.chips = { list: answer.suggestions || [], label: answer.kind === "fallback" ? "Maybe one of these?" : "" };
        }
        saveHelp(); renderHelp();
      })
      .catch(function () {
        state.log.push({ who: "bot", html: "<p>Sorry, that didn't go through. Please check your connection and try again.</p>" });
        saveHelp(); renderHelp();
      });
    saveHelp();
  }
  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var ask = event.target.closest("[data-help-ask]");
    if (ask) { event.preventDefault(); openHelp(false); askHelp(ask.dataset.helpAsk); return; }
    if (event.target.closest("[data-help-open]")) { event.preventDefault(); openHelp(); return; }
    var toggle = event.target.closest("[data-help-toggle]");
    if (toggle) { var panel = helpPanel(); if (panel && !panel.hidden) closeHelp(); else openHelp(); return; }
    if (event.target.closest("[data-help-close]")) { closeHelp(); return; }
    // A link in the chat on a phone, where the chat fills the screen: close it so
    // the page it opens can be seen. (On a wider screen it stays open beside the page.)
    var link = event.target.closest("#help-chat a[href]");
    if (link && window.matchMedia("(max-width: 560px)").matches) { closeHelp(); return; }
    if (event.target.closest("[data-help-clear]")) {
      helpMemory = freshHelp();
      helpMemory.open = true;
      saveHelp(); openHelp();
    }
  });
  document.addEventListener("submit", function (event) {
    if (event.target.matches && event.target.matches("form[action='/logout']")) {
      helpMemory = freshHelp();
      try { window.sessionStorage.removeItem(HELP_KEY); } catch (e) { /* nothing kept */ }
      return;
    }
    var form = event.target.closest && event.target.closest("[data-help-form]");
    if (!form) return;
    event.preventDefault();
    var input = form.querySelector("input[name=question]");
    askHelp(input.value);
    input.value = "";
  });
  document.addEventListener("keydown", function (event) {
    var panel = helpPanel();
    if (event.key === "Escape" && panel && !panel.hidden && panel.contains(document.activeElement)) closeHelp();
  });
  function initHelpChat() {
    var panel = helpPanel();
    if (panel && helpState().open && panel.hidden) openHelp(false);  // still open after moving to another page
    if (!helpTimer && panel) {
      // A page left open: clear a conversation that has gone unused, even with nobody clicking.
      helpTimer = window.setInterval(function () {
        var before = helpMemory, shown = before && before.log.length;
        helpState();  // replaces the conversation if it has gone unused
        if (helpMemory !== before && shown) { closeHelp(); renderHelp(); }
      }, 60 * 1000);
    }
  }

  function init() {
    initFlashes();
    initPlans();
    initReview();
    initHelpChat();
    initCountdowns();
    initCheckSteps();
    initChangeForms();
    loadAccess();
    applyAccess(document);
    $all(document, "[data-auth-flow]").forEach(function (root) {
      if (root.closest("dialog") || root._flow) return;
      new Flow(root).reset(root.dataset.next);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("htmx:afterSettle", init);
})();
