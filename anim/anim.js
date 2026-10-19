/* 通用分步动画播放器：提供单步 / 播放 / 进度 / 键盘控制，舞台渲染交给各动画自己。
 *
 * 用法（每个动画页面在自己的 script 里）：
 *   Anim.create({
 *     steps: [...],                 // 每步任意结构，至少建议带 t(标题) d(说明) src(源码位置) log(一行日志)
 *     onStep(step, i, total) { ... } // 渲染舞台、变量面板、日志
 *   });
 */
(function () {
  "use strict";

  function create(cfg) {
    var steps = cfg.steps || [];
    var onStep = cfg.onStep || function () {};
    var total = steps.length;
    var i = 0;
    var timer = null;
    var delay = 1400;

    var el = {
      cap: document.getElementById("cap"),
      stage: document.getElementById("stage"),
      vars: document.getElementById("vars"),
      log: document.getElementById("log"),
      prev: document.getElementById("b-prev"),
      next: document.getElementById("b-next"),
      play: document.getElementById("b-play"),
      first: document.getElementById("b-first"),
      last: document.getElementById("b-last"),
      range: document.getElementById("range"),
      cnt: document.getElementById("cnt"),
      speed: document.getElementById("speed"),
    };

    function paint() {
      var s = steps[i];
      if (el.cap) {
        el.cap.innerHTML =
          '<h2><span class="no">' +
          (i + 1) +
          "</span>" +
          esc(s.t || "") +
          "</h2>" +
          (s.d ? "<p>" + esc(s.d) + "</p>" : "") +
          (s.src ? '<span class="src">' + esc(s.src) + "</span>" : "");
      }
      onStep(s, i, total);
      if (el.range) el.range.value = String(i);
      if (el.cnt) el.cnt.textContent = i + 1 + " / " + total;
      if (el.prev) el.prev.disabled = i === 0;
      if (el.next) el.next.disabled = i === total - 1;
      if (el.first) el.first.disabled = i === 0;
      if (el.last) el.last.disabled = i === total - 1;
    }

    function go(n) {
      i = Math.max(0, Math.min(total - 1, n));
      paint();
    }

    function stop() {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
      if (el.play) el.play.textContent = "▶ 播放";
    }

    function play() {
      if (timer) {
        stop();
        return;
      }
      if (i >= total - 1) go(0);
      if (el.play) el.play.textContent = "⏸ 暂停";
      timer = setInterval(function () {
        if (i >= total - 1) {
          stop();
          return;
        }
        go(i + 1);
      }, delay);
    }

    if (el.play) el.play.onclick = play;
    if (el.prev) el.prev.onclick = function () { stop(); go(i - 1); };
    if (el.next) el.next.onclick = function () { stop(); go(i + 1); };
    if (el.first) el.first.onclick = function () { stop(); go(0); };
    if (el.last) el.last.onclick = function () { stop(); go(total - 1); };
    if (el.range) {
      el.range.max = String(total - 1);
      el.range.oninput = function () { stop(); go(parseInt(el.range.value, 10)); };
    }
    if (el.speed) {
      el.speed.onchange = function () {
        delay = parseInt(el.speed.value, 10);
        if (timer) {
          stop();
          play();
        }
      };
    }

    document.addEventListener("keydown", function (e) {
      if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      if (e.key === "ArrowLeft") { stop(); go(i - 1); }
      else if (e.key === "ArrowRight") { stop(); go(i + 1); }
      else if (e.key === " ") { e.preventDefault(); play(); }
      else if (e.key === "Home") { stop(); go(0); }
      else if (e.key === "End") { stop(); go(total - 1); }
    });

    paint();
    return { go: go, play: play, stop: stop };
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /** 渲染变量面板：kv([[键, 值, 是否本步变化], ...]) */
  function kv(rows) {
    var el = document.getElementById("vars");
    if (!el) return;
    el.innerHTML =
      "<h3>状态</h3>" +
      rows
        .map(function (r) {
          return (
            '<div class="kv"><span>' +
            esc(r[0]) +
            '</span><b class="' +
            (r[2] ? "chg" : "") +
            '">' +
            esc(r[1]) +
            "</b></div>"
          );
        })
        .join("");
  }

  /** 渲染日志：steps[0..i] 的 log 字段 */
  function log(steps, i) {
    var el = document.getElementById("log");
    if (!el) return;
    var items = [];
    for (var k = 0; k <= i; k++) {
      if (!steps[k] || !steps[k].log) continue;
      items.push(
        '<li class="' + (k === i ? "cur" : "") + '">' + esc(steps[k].log) + "</li>"
      );
    }
    el.innerHTML = "<h3>事件流</h3><ol>" + items.join("") + "</ol>";
    el.scrollTop = el.scrollHeight;
  }

  window.Anim = { create: create, kv: kv, log: log, esc: esc };
})();
