(function () {
  var state = { token: "", threadId: "", runId: "", traceId: "", approvalId: "", actionHash: "", argumentsHash: "", controller: null };
  var tokenInput = document.getElementById("token");
  var events = document.getElementById("events");
  var approvalBox = document.getElementById("approval");
  var stateBox = document.getElementById("state");
  var traceBox = document.getElementById("trace");
  var costBox = document.getElementById("cost");
  var traceInput = document.getElementById("traceid");
  var btnApprove = document.getElementById("btn-approve");
  var btnReject = document.getElementById("btn-reject");
  var params = new URLSearchParams(window.location.search);
  if (params.get("token")) { tokenInput.value = params.get("token"); }
  state.token = tokenInput.value;
  tokenInput.addEventListener("input", function () { state.token = tokenInput.value.trim(); });

  function headers(mutating) {
    var h = { "X-PQ-Token": state.token };
    if (mutating) { h["Content-Type"] = "application/json"; }
    return h;
  }

  function show(box, value) {
    box.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function append(kind, text) {
    var line = document.createElement("span");
    line.className = "ev ev-" + kind;
    line.textContent = "[" + kind + "] " + text;
    events.appendChild(line);
    events.scrollTop = events.scrollHeight;
  }

  function fail(label, payload) {
    append("error", label + " => " + JSON.stringify(payload));
  }

  function setApproval(payload) {
    state.approvalId = payload.approval_id || "";
    state.actionHash = payload.action_hash || "";
    state.argumentsHash = payload.arguments_hash || "";
    state.threadId = payload.thread_id || state.threadId;
    var ready = !!state.approvalId;
    btnApprove.disabled = !ready;
    btnReject.disabled = !ready;
    show(approvalBox, payload);
  }

  function readSSE(response) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    function pump() {
      return reader.read().then(function (result) {
        if (result.done) { return; }
        buffer += decoder.decode(result.value, { stream: true });
        var blocks = buffer.split("\n\n");
        buffer = blocks.pop();
        blocks.forEach(handleBlock);
        return pump();
      });
    }
    return pump();
  }

  function handleBlock(block) {
    if (!block.trim()) { return; }
    var name = "message";
    var dataLines = [];
    block.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) { name = line.slice(6).trim(); }
      else if (line.indexOf("data:") === 0) { dataLines.push(line.slice(5).trim()); }
    });
    var payload = {};
    try { payload = dataLines.length ? JSON.parse(dataLines.join("\n")) : {}; } catch (err) { payload = { raw: dataLines.join(" ") }; }
    onEvent(name, payload);
  }

  function onEvent(name, payload) {
    if (name === "node") { append(name, payload.node + " -> " + (payload.fields || []).join(",")); return; }
    if (name === "meta") { state.threadId = payload.thread_id; state.runId = payload.run_id; state.traceId = payload.trace_id; traceInput.value = payload.trace_id; append(name, JSON.stringify(payload)); return; }
    if (name === "approval_required") { append(name, JSON.stringify(payload)); setApproval(payload); return; }
    if (name === "done") { append(name, JSON.stringify(payload)); if (payload.trace_id) { traceInput.value = payload.trace_id; } return; }
    if (name === "state") { append(name, JSON.stringify(payload)); return; }
    append(name, JSON.stringify(payload));
  }

  document.getElementById("btn-send").addEventListener("click", function () {
    if (!state.token) { fail("send", "请先填 token"); return; }
    var query = document.getElementById("query").value;
    var asof = document.getElementById("asof").value;
    events.textContent = "";
    approvalBox.textContent = "";
    stateBox.textContent = "";
    btnApprove.disabled = true;
    btnReject.disabled = true;
    state.controller = new AbortController();
    append("meta", "POST /api/chat");
    fetch("/api/chat", {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({ query: query, as_of: asof, thread_id: state.threadId || null }),
      signal: state.controller.signal,
      credentials: "same-origin"
    }).then(function (resp) {
      if (!resp.ok) { return resp.json().then(function (body) { fail("chat", body); }); }
      return readSSE(resp);
    }).catch(function (err) {
      if (err && err.name === "AbortError") { append("aborted", "客户端已断开（服务端会把运行标记为 aborted）"); return; }
      fail("chat", String(err));
    });
  });

  document.getElementById("btn-abort").addEventListener("click", function () {
    if (state.controller) { state.controller.abort(); }
  });

  function decide(approved) {
    if (!state.token || !state.approvalId) { return; }
    append("approval", (approved ? "批准 " : "拒绝 ") + state.approvalId);
    fetch("/api/approve", {
      method: "POST",
      headers: headers(true),
      credentials: "same-origin",
      body: JSON.stringify({
        thread_id: state.threadId,
        approval_id: state.approvalId,
        action_hash: state.actionHash,
        arguments_hash: state.argumentsHash,
        approved: approved,
        reason: approved ? "面板批准" : "面板拒绝"
      })
    }).then(function (resp) { return resp.json().then(function (body) {
      if (!resp.ok) { fail("approve", body); return; }
      state.threadId = body.thread_id || state.threadId;
      show(approvalBox, body);
      append("approval", "status=" + body.status + " interrupted=" + body.interrupted);
      btnApprove.disabled = true;
      btnReject.disabled = true;
      refreshState();
    }); }).catch(function (err) { fail("approve", String(err)); });
  }

  btnApprove.addEventListener("click", function () { decide(true); });
  btnReject.addEventListener("click", function () { decide(false); });

  function refreshState() {
    if (!state.token || !state.threadId) { return; }
    fetch("/api/state/" + encodeURIComponent(state.threadId), { headers: headers(false), credentials: "same-origin" })
      .then(function (resp) { return resp.json().then(function (body) { if (!resp.ok) { fail("state", body); return; } show(stateBox, body); }); })
      .catch(function (err) { fail("state", String(err)); });
  }
  document.getElementById("btn-state").addEventListener("click", refreshState);

  document.getElementById("btn-trace").addEventListener("click", function () {
    var id = traceInput.value.trim();
    if (!state.token || !id) { return; }
    fetch("/api/trace/" + encodeURIComponent(id), { headers: headers(false), credentials: "same-origin" })
      .then(function (resp) { return resp.json().then(function (body) { show(traceBox, body); }); })
      .catch(function (err) { fail("trace", String(err)); });
  });

  document.getElementById("btn-cost").addEventListener("click", function () {
    if (!state.token) { return; }
    fetch("/api/cost", { headers: headers(false), credentials: "same-origin" })
      .then(function (resp) { return resp.json().then(function (body) { show(costBox, body); }); })
      .catch(function (err) { fail("cost", String(err)); });
  });

  document.getElementById("btn-health").addEventListener("click", function () {
    if (!state.token) { return; }
    fetch("/api/health", { headers: headers(false), credentials: "same-origin" })
      .then(function (resp) { return resp.json().then(function (body) { show(stateBox, body); }); })
      .catch(function (err) { fail("health", String(err)); });
  });

  // ---- APM 视图：数据全部来自服务端，前端不做换算 ----
  function spanRows(detail) {
    var shape = (detail && detail.shape) || {};
    return (shape.spans) || [];
  }

  function renderTopology(detail) {
    var rows = spanRows(detail);
    if (!rows.length) { show(document.getElementById("topology"), "没有 span（可能这台机器没接 tracer）"); return; }
    var children = {};
    rows.forEach(function (item) {
      var parent = item.parent_id || "";
      if (!children[parent]) { children[parent] = []; }
      children[parent].push(item);
    });
    var lines = [];
    function walk(parent, depth) {
      (children[parent] || []).forEach(function (item) {
        lines.push(new Array(depth + 1).join("  ") + (depth ? "- " : "") + item.name + "  [" + item.status + "]" + (item.error_code ? " " + item.error_code : ""));
        walk(item.span_id, depth + 1);
      });
    }
    walk("", 0);
    show(document.getElementById("topology"), lines.join("\n"));
  }

  function renderWaterfall(detail) {
    var rows = spanRows(detail);
    var total = 0;
    rows.forEach(function (item) { total = Math.max(total, Number(item.duration_ms) || 0); });
    var lines = rows.map(function (item) {
      var ms = Number(item.duration_ms) || 0;
      var width = total > 0 ? Math.round((ms / total) * 20) : 0;
      return String(ms).padStart(6) + "ms |" + new Array(width + 1).join("=") + (item.status === "error" ? " x " : "   ") + item.name;
    });
    show(document.getElementById("waterfall"), lines.join("\n") || "没有 span");
  }

  function renderLogs(detail) {
    var rows = (detail && detail.logs) || [];
    var lines = rows.map(function (item) {
      return (item.occurred_at || "") + " [" + item.severity + "] " + item.event + " trace=" + String(item.trace_id || "").slice(0, 16) + " span=" + String(item.span_id || "").slice(0, 8) + " " + (item.message || "");
    });
    show(document.getElementById("logs"), lines.join("\n") || "没有日志");
  }

  function loadDetail() {
    var id = traceInput.value.trim();
    if (!state.token || !id) { return; }
    fetch("/api/trace/" + encodeURIComponent(id) + "/detail", { headers: headers(false), credentials: "same-origin" })
      .then(function (resp) { return resp.json().then(function (body) {
        if (!resp.ok) { fail("detail", body); return; }
        var detail = body.detail;
        if (!detail) {
          document.getElementById("detail-note").textContent = "服务端未接 trace recorder：" + (body.detail_reason || "");
          show(document.getElementById("topology"), "无详情");
          show(document.getElementById("waterfall"), "无详情");
          show(document.getElementById("logs"), "无详情");
          return;
        }
        var shape = detail.shape || {};
        document.getElementById("detail-note").textContent = "span 数 " + (shape.span_count || 0) + "；缺口 " + JSON.stringify(shape.missing_spans || []) + "；层级错误 " + JSON.stringify(detail.hierarchy_errors || []) + "；recorder 丢弃 " + (detail.recorder_dropped || 0);
        renderTopology(detail);
        renderWaterfall(detail);
        renderLogs(detail);
      }); })
      .catch(function (err) { fail("detail", String(err)); });
  }

  function loadApm() {
    if (!state.token) { return; }
    fetch("/api/apm", { headers: headers(false), credentials: "same-origin" })
      .then(function (resp) { return resp.json().then(function (body) { show(document.getElementById("apm"), body); }); })
      .catch(function (err) { fail("apm", String(err)); });
  }

  document.getElementById("btn-detail").addEventListener("click", loadDetail);
  document.getElementById("btn-apm").addEventListener("click", loadApm);
})();