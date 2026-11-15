/* Makes the "Watch" and "Action needed" badges open exercises.html on the matching tab.
   Normal badges stay as they are. Nothing else on the dashboard is touched.
   An area can set data-exercise="<tab>" (or "none") on a container to choose its tab directly. */
(function () {
  var SEL = ".badge.watch,.badge.act";
  var MAP = [[/resist|strength|bone|leg/i, "legs"], [/skin|temperature|arms/i, "skin"], [/mood|head|brain/i, "head"], [/aerobic|cardio exercise|walk|blood|sugar|abdomen/i, "abdomen"], [/sleep/i, "sleep"], [/heart|chest/i, "chest"]];
  var st = document.createElement("style");
  st.textContent = ".badge[data-ex]{cursor:pointer}.badge[data-ex]:hover{background:rgba(255,255,255,.08)}" +
    ".badge[data-ex]:focus-visible{outline:2px solid #4a8bff;outline-offset:2px}";
  document.head.appendChild(st);
  function area(b) {
    var o = b.closest("[data-exercise]");
    if (o) { var v = o.getAttribute("data-exercise"); return v === "none" ? null : v; }
    var box = b.closest(".card,#panel"), h = box && box.querySelector("h3"), t = h ? h.textContent : "";
    var m = MAP.filter(function (x) { return x[0].test(t); })[0];
    return m && m[1];
  }
  function go(b) {
    var a = area(b); if (!a) return;
    var r = document.getElementById("v-results"), app = document.getElementById("app");
    try {
      if (r && !r.hidden && app && !app.hidden) sessionStorage.astraBack = "results";
      else sessionStorage.removeItem("astraBack");
    } catch (e) {}
    location.href = "exercises.html#" + a;
  }
  function prep() {
    document.querySelectorAll(SEL).forEach(function (b) {
      var a = area(b);
      if (!a) { b.removeAttribute("data-ex"); return; }
      if (b.dataset.ex === a) return;
      b.dataset.ex = a; b.tabIndex = 0; b.setAttribute("role", "link"); b.title = "Open exercises";
    });
  }
  document.addEventListener("click", function (e) { var b = e.target.closest(SEL); if (b) go(b); });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var b = e.target.closest && e.target.closest(SEL); if (b) go(b);
  });
  new MutationObserver(prep).observe(document.body, { childList: true, subtree: true });
  prep();
})();
