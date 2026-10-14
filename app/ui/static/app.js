/* Job Assistant -- page behaviour without a framework:
 *  - the sign-in / sign-up flow (the "Welcome" dialog and the /login, /signup pages),
 *    talking JSON to /api/auth/*; the session itself is an HttpOnly cookie, so
 *    nothing about it lives in JavaScript or localStorage;
 *  - "sign in to continue" for searches and actions that need an account;
 *  - Show/Hide on password fields, account / mobile menus, the job details panel;
 *  - window.jobAssistant.access: hasPermission / hasAnyPermission / hasAllPermissions /
 *    canAccessRoute for the signed-in user (UX only -- the server checks every request);
 *  - admin pages: the permission matrix's unsaved-changes state, confirm prompts.
 * Everything is attached by event delegation, so it keeps working after htmx swaps.
 */
(function () {
  "use strict";

  var signedIn = function () { return document.body && document.body.dataset.auth === "1"; };

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
    var google = $(this.root, "[data-google]");
    if (google) google.href = "/auth/google/start?next=" + encodeURIComponent(this.next());
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
    if (form.password.value.length < 12) problems.password = "Use at least 12 characters.";
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
      var score = v.length < 12 ? 0 : (v.length >= 20 ? 3 : (v.length >= 16 || kinds >= 3 ? 2 : 1));
      var labels = ["Too short -- at least 12 characters", "Fair -- longer is stronger", "Good", "Strong"];
      bar.style.width = v ? [25, 50, 75, 100][score] + "%" : "0";
      bar.className = "s" + score;
      text.textContent = v ? labels[score] : "At least 12 characters. A short phrase works well.";
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
  window.jobAssistant = Object.assign(window.jobAssistant || {}, {
    access: { hasPermission: hasPermission, hasAnyPermission: hasAnyPermission,
              hasAllPermissions: hasAllPermissions, canAccessRoute: canAccessRoute, refresh: refreshAccess,
              get roles() { return access.roles.slice(); }, get permissions() { return access.permissions.slice(); } }
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

  // ---------- init flows on the page (login / signup pages) ----------
  function init() {
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
