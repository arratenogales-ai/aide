// Aide panel: live updates with server events, clocks, confirmations
// and the interactive parts of 2.134 (tabs that slide and are remembered, cards for the points of the
// charts and the calendar days, smooth dropdowns). Nothing inline: everything goes through
// listeners and data-* attributes, and the computed styles (the position of a popover or of the
// tab background) are set with element.style, which the content policy allows.
(function () {
  "use strict";
  var content = document.getElementById("content");
  var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function save(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* no storage */ } }
  function read(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function near(el, selector) { return el && el.closest ? el.closest(selector) : null; }

  // Confirmation of irreversible actions (forms with data-confirm).
  document.addEventListener("submit", function (e) {
    var text = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (text && !window.confirm(text)) e.preventDefault();
  });
  // Uploading documents: when the files are chosen, the form submits itself.
  document.addEventListener("change", function (e) {
    var f = near(e.target, "form[data-upload]");
    if (f && e.target.type === "file" && e.target.files && e.target.files.length) f.submit();
  });

  // Elapsed time of running tasks (elements with data-since, ISO time). It is
  // recalculated every second, also in the content that arrives with the live update.
  function duration(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds < 60) return seconds + " s";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + " min";
    var hours = Math.floor(minutes / 60);
    minutes = minutes % 60;
    if (hours < 48) return hours + " h " + (minutes < 10 ? "0" : "") + minutes + " min";
    return Math.floor(hours / 24) + " days " + (hours % 24) + " h";
  }
  function clocks() {
    var now = Date.now();
    var elements = document.querySelectorAll("[data-since]");
    for (var i = 0; i < elements.length; i++) {
      var since = Date.parse(elements[i].getAttribute("data-since"));
      if (!isNaN(since)) elements[i].textContent = duration((now - since) / 1000);
    }
  }
  clocks();
  setInterval(clocks, 1000);

  // --- The small popover for anything with data-tooltip (points, bars, days) ---------------
  var tooltip = document.createElement("div");
  tooltip.className = "tooltip";
  tooltip.setAttribute("role", "tooltip");
  document.body.appendChild(tooltip);
  var withTooltip = null;
  function place(floating, anchor, gap) {
    var r = anchor.getBoundingClientRect();
    var f = floating.getBoundingClientRect();
    var x = r.left + r.width / 2 - f.width / 2;
    x = Math.max(8, Math.min(x, window.innerWidth - f.width - 8));
    var y = r.top - f.height - gap;
    if (y < 8) y = r.bottom + gap;
    if (y + f.height > window.innerHeight - 8) y = Math.max(8, window.innerHeight - f.height - 8);
    floating.style.left = Math.round(x) + "px";
    floating.style.top = Math.round(y) + "px";
  }
  function showTooltip(el) {
    var text = el.getAttribute("data-tooltip");
    if (!text || popover) return;
    if (withTooltip && withTooltip !== el) withTooltip.classList.remove("marked");
    withTooltip = el;
    el.classList.add("marked");
    tooltip.textContent = text;
    place(tooltip, el, 8);
    tooltip.classList.add("visible");
  }
  function hideTooltip() {
    if (withTooltip) withTooltip.classList.remove("marked");
    withTooltip = null;
    tooltip.classList.remove("visible");
  }
  document.addEventListener("pointerover", function (e) {
    var el = near(e.target, "[data-tooltip]");
    if (el) showTooltip(el);
    else if (withTooltip) hideTooltip();
  });
  document.addEventListener("focusin", function (e) {
    var el = near(e.target, "[data-tooltip]");
    if (el) showTooltip(el);
  });
  document.addEventListener("focusout", function (e) { if (near(e.target, "[data-tooltip]")) hideTooltip(); });
  window.addEventListener("scroll", function () { hideTooltip(); closePopover(); }, { passive: true });
  window.addEventListener("resize", function () { hideTooltip(); closePopover(); placeIndicators(false); });

  // --- A calendar day's popover: its date, its phase and the button ----------------------
  // Without JavaScript, tapping a day notes or removes it (the button submits its form). With it,
  // the popover comes up first, and the popover's button submits that same form.
  var popover = null;
  var popoverDay = null;
  function closePopover() {
    if (!popover) return;
    popover.parentNode.removeChild(popover);
    popover = null;
    if (popoverDay) { popoverDay.classList.remove("chosen"); popoverDay.removeAttribute("aria-expanded"); }
    popoverDay = null;
    retry();
  }
  function openPopover(day) {
    var same = day === popoverDay;
    closePopover();
    hideTooltip();
    if (same) return;
    popoverDay = day;
    day.classList.add("chosen");
    day.setAttribute("aria-expanded", "true");
    popover = document.createElement("div");
    popover.className = "day-popover";
    popover.setAttribute("role", "dialog");
    popover.setAttribute("aria-label", day.getAttribute("data-date") || "Day");
    var close = document.createElement("button");
    close.type = "button";
    close.className = "close";
    close.setAttribute("aria-label", "Close");
    close.textContent = "×";
    close.addEventListener("click", function () { var d = popoverDay; closePopover(); if (d) d.focus(); });
    var date = document.createElement("span");
    date.className = "popover-date";
    date.textContent = day.getAttribute("data-date") || "";
    popover.appendChild(close);
    popover.appendChild(date);
    var phase = day.getAttribute("data-phase");
    var mark = document.createElement("span");
    if (phase) {
      var kinds = ["period", "expected", "fertile", "ovulation"];
      var kind = "";
      for (var i = 0; i < kinds.length; i++) if (day.classList.contains(kinds[i])) kind = kinds[i];
      mark.className = "chip phase-chip " + kind;
      mark.textContent = phase;
    } else {
      mark.className = "nothing";
      mark.textContent = "Nothing logged or expected.";
    }
    popover.appendChild(mark);
    var action = day.getAttribute("data-action");
    var form = near(day, "form");
    var button = null;
    if (action && form) {
      button = document.createElement("button");
      button.type = "button";
      button.className = day.classList.contains("period") ? "secondary" : "";
      button.textContent = action;
      button.addEventListener("click", function () {
        button.disabled = true;
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      });
      popover.appendChild(button);
    }
    document.body.appendChild(popover);
    place(popover, day, 10);
    if (button) button.focus({ preventScroll: true });
  }
  document.addEventListener("click", function (e) {
    var day = near(e.target, ".calendar .day[data-date]");
    if (day) { e.preventDefault(); openPopover(day); return; }
    if (popover && !near(e.target, ".day-popover")) closePopover();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && popover) { var d = popoverDay; closePopover(); if (d) d.focus(); return; }
    var day = near(e.target, ".calendar span.day[tabindex]");
    if (day && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openPopover(day); }
  });

  // --- Dropdowns: their content eases in when opened with a tap --------------------------
  document.addEventListener("click", function (e) {
    var summary = near(e.target, "summary");
    if (!summary || still) return;
    var collapsible = summary.parentNode;
    if (!collapsible || collapsible.open) return;
    collapsible.classList.add("opening");
    setTimeout(function () { collapsible.classList.remove("opening"); }, 400);
  });

  // --- Tabs: the active one's background slides; the last one of each section is remembered ---
  function placeIndicator(nav, animar) {
    var active = nav.querySelector("a.active");
    var indicator = nav.querySelector(".indicator");
    if (!indicator) {
      indicator = document.createElement("span");
      indicator.className = "indicator";
      indicator.setAttribute("aria-hidden", "true");
      nav.insertBefore(indicator, nav.firstChild);
    }
    if (!active) { nav.classList.remove("with-indicator"); return; }
    if (!animar) nav.classList.remove("animated");
    indicator.style.width = active.offsetWidth + "px";
    indicator.style.height = active.offsetHeight + "px";
    indicator.style.transform = "translate(" + active.offsetLeft + "px, " + active.offsetTop + "px)";
    nav.classList.add("with-indicator");
    if (!animar) { void indicator.offsetWidth; nav.classList.add("animated"); }
  }
  function placeIndicators(animar) {
    var navs = document.querySelectorAll("nav.section-tabs, nav.subnav");
    for (var i = 0; i < navs.length; i++) placeIndicator(navs[i], animar);
  }
  function rememberTabs() {
    var navs = document.querySelectorAll("nav[data-remember]");
    for (var i = 0; i < navs.length; i++) {
      var active = navs[i].querySelector("a.active");
      if (active) save("aide:tab:" + navs[i].getAttribute("data-remember"), active.getAttribute("href"));
    }
    // The menu leads to the last tab you opened in each section.
    var links = document.querySelectorAll(".side a.section");
    for (var j = 0; j < links.length; j++) {
      var path = links[j].getAttribute("data-path") || links[j].getAttribute("href");
      links[j].setAttribute("data-path", path);
      var last = read("aide:tab:" + path);
      if (last && last.indexOf(path) === 0) links[j].setAttribute("href", last);
    }
  }
  function entry() {
    // The rings are drawn on arriving at the page or another tab, not on every update.
    if (still || !content) return;
    content.classList.add("entering");
    setTimeout(function () { content.classList.remove("entering"); }, 1200);
  }
  function start(animar) {
    placeIndicators(animar);
    rememberTabs();
    clocks();
  }
  start(false);
  entry();
  // On mobile the menu is a sliding row: the open section stays in view.
  var side = document.querySelector(".side");
  var active = side && side.querySelector(".section.active");
  if (side && active && side.scrollWidth > side.clientWidth) {
    side.scrollLeft = Math.max(0, active.offsetLeft - (side.clientWidth - active.offsetWidth) / 2);
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { placeIndicators(false); });

  if (!window.fetch || !content) return;

  // --- New content without reloading the page ---------------------------------------------
  var last = null;
  var pending = false;
  var pressed = false;
  function request(address) {
    return fetch(address || location.href, { headers: { "X-Partial": "1" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; });
  }
  function busy() {
    var a = document.activeElement;
    return pressed || popover !== null || (a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT" || a.tagName === "SELECT"));
  }
  function put(html, keep) {
    var openBefore = [];
    if (keep) {
      var before = content.querySelectorAll("details");
      for (var i = 0; i < before.length; i++) if (before[i].open) openBefore.push(i);
    }
    content.innerHTML = html;
    if (keep) {
      // Each collapsible stays as you had it: open or closed.
      var everything = content.querySelectorAll("details");
      for (var j = 0; j < everything.length; j++) everything[j].open = openBefore.indexOf(j) >= 0;
    }
    start(false);
  }

  // Switching tab (or month, or exercise): only the content is requested, the tab background
  // slides to the new one and the address changes, so that back and the live update
  // stay on the same tab. If something fails, the whole page opens.
  function changeTo(address, isNew) {
    closePopover();
    hideTooltip();
    content.classList.add("loading");
    var wait = new Promise(function (done) { setTimeout(done, still ? 0 : 200); });
    Promise.all([request(address), wait]).then(function (r) {
      var html = r[0];
      if (html === null) { location.href = address; return; }
      if (isNew) history.pushState({ partial: true }, "", address);
      where = location.pathname + location.search;
      last = html;
      put(html, false);
      content.classList.remove("loading");
      entry();
    }).catch(function () { location.href = address; });
  }
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var link = near(e.target, "a[data-tab], nav.section-tabs a");
    if (!link || !window.history || !history.pushState) return;
    var destination = new URL(link.getAttribute("href"), location.href);
    if (destination.origin !== location.origin) return;
    e.preventDefault();
    var nav = near(link, "nav.section-tabs");
    if (nav) {
      var other = nav.querySelectorAll("a");
      for (var i = 0; i < other.length; i++) { other[i].classList.remove("active"); other[i].removeAttribute("aria-current"); }
      link.classList.add("active");
      link.setAttribute("aria-current", "page");
      placeIndicator(nav, true);
    }
    changeTo(destination.href, destination.href !== location.href);
  });
  // Marked query forms (the chart's exercise) work the same; with data-auto,
  // when the list changes.
  function query(form) {
    var destination = new URL(form.getAttribute("action") || location.pathname, location.href);
    var fields = new FormData(form);
    destination.search = new URLSearchParams(fields).toString();
    if (document.activeElement) document.activeElement.blur();
    changeTo(destination.href, true);
  }
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f.hasAttribute || !f.hasAttribute("data-tab") || (f.method || "get").toLowerCase() !== "get") return;
    e.preventDefault();
    query(f);
  });
  document.addEventListener("change", function (e) {
    var f = near(e.target, "form[data-auto]");
    if (f && e.target.tagName === "SELECT") query(f);
  });
  // Back and forward between tabs (a change of only the anchor, like “#profile”, does not count).
  var where = location.pathname + location.search;
  window.addEventListener("popstate", function () {
    var now = location.pathname + location.search;
    if (now === where) return;
    where = now;
    changeTo(location.href, false);
  });

  if (!window.EventSource) return;

  // The server announces each change and the content is requested again. It is only replaced if it
  // has really changed and never while typing, with the mouse pressed or with a day popover
  // open, so as not to lose a click or what is being typed.
  function reload() {
    if (busy()) { pending = true; return; }
    request().then(function (html) {
      if (html === null || html === last) return;
      if (busy()) { pending = true; return; }
      last = html;
      put(html, true);
    });
  }
  function retry() {
    if (pending && !busy()) { pending = false; setTimeout(reload, 300); }
  }
  // Initial reference: the content as it was served, so as not to replace it without changes.
  request().then(function (html) { if (last === null) last = html; });
  document.addEventListener("mousedown", function () { pressed = true; });
  document.addEventListener("mouseup", function () { pressed = false; retry(); });
  document.addEventListener("focusout", retry);
  new EventSource("/events").addEventListener("change", reload);
})();
