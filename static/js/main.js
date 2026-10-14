/* ============================================================
   资料百事通 - 公共脚本（灯箱等）
   作者：军哥懂保（微信：xunijiayuan）
============================================================ */
(function () {
  "use strict";
  var lb = document.getElementById("lightbox");
  if (!lb) return;
  var lbImg = document.getElementById("lbImg");
  var lbCount = document.getElementById("lbCount");
  var list = [];
  var idx = 0;

  function open(i) {
    idx = (i + list.length) % list.length;
    lbImg.src = "/uploads/" + list[idx];
    lbCount.textContent = (idx + 1) + " / " + list.length;
    lb.classList.add("open");
    document.body.style.overflow = "hidden";
  }
  function close() {
    lb.classList.remove("open");
    document.body.style.overflow = "";
  }

  document.getElementById("lbClose").onclick = close;
  document.getElementById("lbPrev").onclick = function (e) { e.stopPropagation(); open(idx - 1); };
  document.getElementById("lbNext").onclick = function (e) { e.stopPropagation(); open(idx + 1); };
  lb.addEventListener("click", function (e) { if (e.target === lb) close(); });
  document.addEventListener("keydown", function (e) {
    if (!lb.classList.contains("open")) return;
    if (e.key === "Escape") close();
    if (e.key === "ArrowLeft") open(idx - 1);
    if (e.key === "ArrowRight") open(idx + 1);
  });

  /* 详情页注册图片列表 */
  window.initLightbox = function (imgs, getIdx, setIdx) {
    list = imgs;
    lbImg.addEventListener("load", function () {}, false);
    window.openLightbox = function (i) { open(i); };
  };
})();
