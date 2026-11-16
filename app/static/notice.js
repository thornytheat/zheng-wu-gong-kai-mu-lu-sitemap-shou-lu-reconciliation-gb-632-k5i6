/* 整改通知：出单与复核按钮，走自有接口后刷新台账 */
(function () {
  const issue = document.getElementById("issue-btn");
  const recheck = document.getElementById("recheck-btn");
  const message = document.getElementById("notice-msg");
  const printButton = document.getElementById("print-btn");

  if (printButton) {
    printButton.addEventListener("click", () => window.print());
  }

  async function act(url, done) {
    if (SitemapAudit.state.busy) return;
    SitemapAudit.state.busy = true;
    try {
      const data = await SitemapAudit.json(url, { method: "POST", body: "{}" });
      SitemapAudit.notice(message, done(data), "ok");
      window.setTimeout(() => window.location.reload(), 700);
    } catch (err) {
      SitemapAudit.notice(message, err.message + "（" + err.code + "）", "bad");
    } finally {
      SitemapAudit.state.busy = false;
    }
  }

  if (issue) {
    issue.addEventListener("click", () => act("/api/notices/issue", (data) =>
      "出单 " + data.created + " 张 · 复用已有 " + data.reused + " 张 · 批次 " + data.run_id));
  }
  if (recheck) {
    recheck.addEventListener("click", () => act("/api/notices/recheck", (data) =>
      "复核 " + data.appended + " 条 · 跳过 " + data.skipped + " 条 · 批次 " + data.run_id));
  }
})();
