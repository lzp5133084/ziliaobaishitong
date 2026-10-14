/* ============================================================
   资料百事通 - 上传向导脚本（五步引导 + Canvas手动脱敏）
   作者：军哥懂保（微信：xunijiayuan）
============================================================ */
(function () {
  "use strict";
  var CFG = window.UPLOAD_CFG || { mode: "create", maxImages: 9 };
  var $ = function (id) { return document.getElementById(id); };

  /* ---------------- 状态 ---------------- */
  var staged = [];      // {file, url, img, rects:[], masked}
  var curStep = 1;
  var maskIdx = 0;      // 当前脱敏的图片索引
  var drawing = null;   // 正在拖拽的矩形

  var form = $("docForm");
  if (!form) return;

  /* ---------------- 第2步：图片暂存 ---------------- */
  var picker = $("filePicker");
  var upZone = $("upZone");
  var stage = $("imgStage");

  function validFile(f) {
    var ok = /\.(jpe?g|png|webp)$/i.test(f.name);
    if (!ok) { alert("不支持的格式：" + f.name + "（仅 JPG/PNG/WebP）"); return false; }
    if (f.size > 10 * 1024 * 1024) { alert("图片超过10MB：" + f.name); return false; }
    return true;
  }

  function addFiles(files) {
    var list = Array.prototype.slice.call(files);
    list.forEach(function (f) {
      if (!f || !validFile(f)) return;
      if (staged.length >= CFG.maxImages) { alert("最多上传" + CFG.maxImages + "张图片"); return; }
      var url = URL.createObjectURL(f);
      var im = new Image();
      im.onload = function () { renderStage(); };
      im.src = url;
      staged.push({ file: f, url: url, img: im, rects: [], masked: false });
    });
    renderStage();
  }

  function renderStage() {
    if (!stage) return;
    stage.innerHTML = "";
    staged.forEach(function (it, i) {
      var div = document.createElement("div");
      div.className = "stage-item" + (it.masked ? " masked" : "");
      div.innerHTML =
        '<span class="idx">' + (i + 1) + "</span>" +
        '<span class="mk">已脱敏</span>' +
        '<div class="thumb"><img src="' + it.url + '" alt="预览图"></div>' +
        '<div class="ops">' +
        '<button type="button" data-act="left">←</button>' +
        '<button type="button" data-act="right">→</button>' +
        '<button type="button" class="del" data-act="del">删除</button></div>';
      div.querySelector('[data-act="left"]').onclick = function () { swap(i, i - 1); };
      div.querySelector('[data-act="right"]').onclick = function () { swap(i, i + 1); };
      div.querySelector('[data-act="del"]').onclick = function () {
        URL.revokeObjectURL(it.url);
        staged.splice(i, 1);
        renderStage();
      };
      stage.appendChild(div);
    });
  }

  function swap(a, b) {
    if (b < 0 || b >= staged.length) return;
    var t = staged[a]; staged[a] = staged[b]; staged[b] = t;
    renderStage();
  }

  if (upZone && picker) {
    upZone.addEventListener("click", function () { picker.click(); });
    picker.addEventListener("change", function () { addFiles(this.files); picker.value = ""; });
    upZone.addEventListener("dragover", function (e) { e.preventDefault(); this.classList.add("drag"); });
    upZone.addEventListener("dragleave", function () { this.classList.remove("drag"); });
    upZone.addEventListener("drop", function (e) {
      e.preventDefault(); this.classList.remove("drag");
      addFiles(e.dataTransfer.files);
    });
  }

  /* ---------------- 第3步：Canvas 手动脱敏 ---------------- */
  var canvas = $("maskCanvas");
  var maskList = $("maskList");
  var ctx = canvas ? canvas.getContext("2d") : null;
  var MAX_SIDE = 2200; // 编辑器与导出最长边限制

  function scaledSize(w, h) {
    var s = Math.min(1, MAX_SIDE / Math.max(w, h));
    return [Math.round(w * s), Math.round(h * s)];
  }

  function initMaskView() {
    if (!canvas || !maskList) return;
    maskList.innerHTML = "";
    if (!staged.length) {
      var p = document.createElement("p");
      p.style.cssText = "font-size:13px;color:#93a3b5;padding:20px;text-align:center;";
      p.textContent = "本次没有新上传的图片需要脱敏，直接进入下一步即可。";
      canvas.parentNode.replaceChild(p, canvas); // 隐藏画布
      return;
    }
    maskIdx = Math.min(maskIdx, staged.length - 1);
    staged.forEach(function (it, i) {
      var el = document.createElement("div");
      el.className = "ml-item" + (it.masked ? " masked" : "") + (i === maskIdx ? " active" : "");
      el.innerHTML = '<img src="' + it.url + '" style="width:100%;height:54px;object-fit:cover;" alt="图' + (i + 1) + '">' +
        '<span class="mkflag">已脱敏</span>';
      el.onclick = function () { maskIdx = i; initMaskView(); };
      maskList.appendChild(el);
    });
    drawMaskCanvas();
  }

  function drawMaskCanvas() {
    var it = staged[maskIdx];
    if (!it || !ctx) return;
    var w, h;
    if (!it.cw) {
      var s = scaledSize(it.img.naturalWidth || it.img.width, it.img.naturalHeight || it.img.height);
      it.cw = s[0]; it.ch = s[1];
    }
    w = it.cw; h = it.ch;
    canvas.width = w; canvas.height = h;
    ctx.clearRect(0, 0, w, h);
    ctx.drawImage(it.img, 0, 0, w, h);
    ctx.fillStyle = "rgba(10,10,10,0.96)";
    it.rects.forEach(function (r) { ctx.fillRect(r.x, r.y, r.w, r.h); });
    if (drawing) ctx.fillRect(drawing.x, drawing.y, drawing.w, drawing.h);
  }

  function canvasPos(e) {
    var r = canvas.getBoundingClientRect();
    return {
      x: (e.clientX - r.left) * (canvas.width / r.width),
      y: (e.clientY - r.top) * (canvas.height / r.height)
    };
  }

  if (canvas) {
    canvas.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      canvas.setPointerCapture(e.pointerId);
      var p = canvasPos(e);
      drawing = { x0: p.x, y0: p.y, x: p.x, y: p.y, w: 0, h: 0 };
    });
    canvas.addEventListener("pointermove", function (e) {
      if (!drawing) return;
      var p = canvasPos(e);
      drawing.x = Math.min(drawing.x0, p.x);
      drawing.y = Math.min(drawing.y0, p.y);
      drawing.w = Math.abs(p.x - drawing.x0);
      drawing.h = Math.abs(p.y - drawing.y0);
      drawMaskCanvas();
    });
    function endDraw() {
      if (!drawing) return;
      if (drawing.w > 4 && drawing.h > 4) {
        staged[maskIdx].rects.push({ x: drawing.x, y: drawing.y, w: drawing.w, h: drawing.h });
        staged[maskIdx].masked = true;
        initMaskView();
        renderStage();
      } else {
        drawing = null;
        drawMaskCanvas();
      }
      drawing = null;
    }
    canvas.addEventListener("pointerup", endDraw);
    canvas.addEventListener("pointercancel", endDraw);

    var resetBtn = $("maskResetBtn");
    if (resetBtn) resetBtn.onclick = function () {
      var it = staged[maskIdx];
      if (it && it.rects.length) {
        it.rects = []; it.masked = false;
        initMaskView(); renderStage();
      }
    };
  }

  /* ---------------- 第5步：预览 ---------------- */
  function esc(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function val(name) {
    var el = form.querySelector('[name="' + name + '"]');
    return el ? el.value.trim() : "";
  }

  function buildPreview() {
    var box = $("previewBox");
    if (!box) return;
    var indSel = form.querySelector('[name="industry_id"]');
    var indName = indSel && indSel.selectedIndex > 0 ? indSel.options[indSel.selectedIndex].text : "未选择";
    var rows = [
      ["所属行业", indName], ["资料名称", val("title")], ["用途机构", val("org_name")], ["来源机构", val("source_org")],
      ["所在地区", val("province") + " " + val("city") + (val("county") ? " " + val("county") : "")],
      ["适用业务", val("business")], ["资料类型", val("doc_type")], ["关键词标签", val("tags")],
      ["资料用途", val("purpose")], ["获取方式", val("obtain_method")],
      ["注意事项", val("notes") || "（选填，未填写）"], ["来源说明", val("source") || "（选填，未填写）"],
      ["适用时间", val("valid_period") || "（选填，未填写）"]
    ];
    var html = rows.map(function (r) {
      return '<div class="pv-row"><span class="k">' + r[0] + '</span><span class="v">' + esc(r[1]) + "</span></div>";
    }).join("");
    // 图片预览（已脱敏的实时生成）
    var imgs = [];
    if (CFG.mode === "edit") {
      form.querySelectorAll('input[name="remove_img"]:checked').length;
      Array.prototype.forEach.call(document.querySelectorAll(".stage-item .thumb img"), function (im) {
        if (im.src.indexOf("blob:") !== 0) imgs.push({ src: im.src, masked: false, old: true });
      });
    }
    staged.forEach(function (it) {
      imgs.push({ src: it.masked ? maskThumb(it) : it.url, masked: it.masked });
    });
    html += '<div class="pv-row"><span class="k">参考图片(' + imgs.length + '张)</span><span class="v pv-imgs">' +
      imgs.map(function (im) {
        return '<img src="' + im.src + '" alt="预览"><span style="display:none;"></span>';
      }).join("") + "</span></div>";
    box.innerHTML = html;
  }

  function maskThumb(it) {
    var c = document.createElement("canvas");
    var s = scaledSize(it.img.naturalWidth || it.img.width, it.img.naturalHeight || it.img.height);
    c.width = s[0]; c.height = s[1];
    var cx = c.getContext("2d");
    cx.drawImage(it.img, 0, 0, s[0], s[1]);
    cx.fillStyle = "rgba(10,10,10,0.96)";
    it.rects.forEach(function (r) { cx.fillRect(r.x, r.y, r.w, r.h); });
    return c.toDataURL("image/jpeg", 0.8);
  }

  /* ---------------- 步骤切换 ---------------- */
  var WZ = {
    go: function (n) {
      if (n > curStep && !validateStep(curStep)) return;
      showStep(n);
    }
  };
  window.WZ = WZ;

  function validateStep(n) {
    if (n === 1) {
      var fields = form.querySelectorAll('[data-pane="1"] [required]');
      for (var i = 0; i < fields.length; i++) {
        if (!fields[i].value.trim()) {
          fields[i].focus();
          alert("请完整填写第1步的必填项：" + (fields[i].previousElementSibling ? fields[i].previousElementSibling.textContent.replace("*", "") : "必填项"));
          return false;
        }
      }
      return true;
    }
    if (n === 2) {
      if (CFG.mode === "create" && staged.length === 0) {
        alert("请至少上传1张参考图片"); return false;
      }
      var oldCnt = document.querySelectorAll('input[name="remove_img"]').length;
      var removed = document.querySelectorAll('input[name="remove_img"]:checked').length;
      if (CFG.mode === "edit" && oldCnt - removed + staged.length === 0) {
        alert("至少保留1张图片，或新上传图片"); return false;
      }
      return true;
    }
    return true;
  }

  function showStep(n) {
    curStep = n;
    document.querySelectorAll(".wz-pane").forEach(function (p) {
      p.classList.toggle("active", +p.dataset.pane === n);
    });
    document.querySelectorAll("#wizardHead .wz-step").forEach(function (s) {
      var i = +s.dataset.step;
      s.classList.toggle("active", i === n);
      s.classList.toggle("done", i < n);
      if (i < n) s.querySelector(".wz-dot").textContent = "✓";
      else s.querySelector(".wz-dot").textContent = i;
    });
    if (n === 3) initMaskView();
    if (n === 5) buildPreview();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /* ---------------- 提交：把暂存图片按顺序写入表单 ---------------- */
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var pc = $("privacyCheck");
    if (pc && !pc.checked) {
      alert("请先勾选隐私确认：确认已完成脱敏或图片不含敏感信息");
      showStep(4);
      pc.focus();
      return;
    }
    // 汇总导出文件（遮挡过的从Canvas导出=不可逆压平，未处理的用原图）
    var dt = new DataTransfer();
    staged.forEach(function (it) {
      if (!it.masked) { dt.items.add(it.file); return; }
      var c = document.createElement("canvas");
      var s = scaledSize(it.img.naturalWidth || it.img.width, it.img.naturalHeight || it.img.height);
      c.width = s[0]; c.height = s[1];
      var cx = c.getContext("2d");
      var isPng = /\.png$/i.test(it.file.name);
      if (!isPng) { cx.fillStyle = "#fff"; cx.fillRect(0, 0, c.width, c.height); }
      cx.drawImage(it.img, 0, 0, c.width, c.height);
      cx.fillStyle = "rgba(10,10,10,0.96)";
      it.rects.forEach(function (r) { cx.fillRect(r.x, r.y, r.w, r.h); });
      var mime = isPng ? "image/png" : "image/jpeg";
      var name = it.file.name.replace(/\.[^.]+$/, "") + "_已脱敏" + (isPng ? ".png" : ".jpg");
      try {
        var blob = c.toDataURL(mime, 0.92);
        var bin = atob(blob.split(",")[1]);
        var arr = new Uint8Array(bin.length);
        for (var k = 0; k < bin.length; k++) arr[k] = bin.charCodeAt(k);
        dt.items.add(new File([arr], name, { type: mime }));
      } catch (err) {
        alert("图片导出失败，请重试：" + err.message);
        dt = null;
        return;
      }
    });
    if (!dt) return;
    var input = $("imgInput");
    input.files = dt.files;
    var btn = $("submitBtn");
    btn.disabled = true;
    btn.textContent = "正在提交，请稍候…";
    form.submit(); // 原生提交，不再触发submit事件
  });
})();
