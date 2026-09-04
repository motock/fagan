// Maturity panel renderer
// Expects metrics and guard-liveness data objects
export function renderMaturity(metrics, guardLiveness) {
  // Minimal placeholder implementation
  const metricsHtml = metrics ? `<pre>${JSON.stringify(metrics, null, 2)}</pre>` : '<p>No metrics available.</p>';
  const guardHtml = guardLiveness ? `<pre>${JSON.stringify(guardLiveness, null, 2)}</pre>` : '<p>No guard data available.</p>';
  return `
    <div class="panel maturity-panel">
      <h3>Maturity</h3>
      <div class="panel-body">
        <h4>Metrics</h4>
        ${metricsHtml}
        <h4>Guard Liveness</h4>
        ${guardHtml}
      </div>
    </div>`;
}
