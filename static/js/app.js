let modelData = [];
let modelConfig = [];
let myChart = null;
let progressTimer = null;
const DISABLED_MODELS_KEY = 'poe_disabled_models';
const CHART_HIDDEN_MODELS_KEY = 'poe_chart_hidden_models';
let disabledModels = new Set();
let chartHiddenModels = new Set();
let currentSort = { key: null, asc: true };
let chartSort = { key: 'none', asc: false };
let groupByPrefixEnabled = false;

async function init() {
    loadDisabledModels();
    loadChartHiddenModels();
    await loadConfig();
    await loadData();
    setupEventListeners();
}

// Config Management
async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        modelConfig = await response.json();
        syncDisabledModelsWithConfig();
        renderModelConfig();
    } catch (err) { console.error('Load config failed'); }
}

function renderModelConfig() {
    const list = document.getElementById('modelList');
    list.innerHTML = modelConfig.map(m => {
        const encoded = encodeURIComponent(m);
        const enabled = isModelEnabled(m);
        const stateText = enabled ? '禁用' : '启用';
        const toggleClass = enabled ? 'enabled' : 'disabled';
        const tagClass = enabled ? '' : 'disabled';
        return `
        <span class="model-tag ${tagClass}">
            <a class="model-name" href="https://poe.com/${encoded}" target="_blank" rel="noopener noreferrer">${escapeHtml(m)}</a>
            <button class="toggle-btn ${toggleClass}" data-action="toggle" data-handle="${encoded}">${stateText}</button>
            <button data-action="delete" data-handle="${encoded}">×</button>
        </span>
    `;
    }).join('');
}

async function addModel() {
    const input = document.getElementById('newModelInput');
    const handle = input.value.trim();
    if (!handle) return;
    const response = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ handle })
    });
    modelConfig = await response.json();
    syncDisabledModelsWithConfig();
    renderModelConfig();
    refreshUI();
    input.value = '';
}

async function deleteModel(handle) {
    const response = await fetch(`/api/config/${handle}`, { method: 'DELETE' });
    modelConfig = await response.json();
    syncDisabledModelsWithConfig();
    renderModelConfig();
    refreshUI();
}

// Data Handling
async function loadData() {
    try {
        const response = await fetch('/api/data');
        modelData = await response.json();
        if (modelData.length > 0) refreshUI();
    } catch (err) { console.log('No data yet'); }
}

async function updateData() {
    const btn = document.getElementById('updateBtn');
    const msg = document.getElementById('loadingMsg');
    const progressWrap = document.getElementById('progressWrap');
    const enabledHandles = modelConfig.filter(isModelEnabled);

    if (enabledHandles.length === 0) {
        setProgress(0, '没有启用模型，请先启用至少一个模型');
        progressWrap.classList.remove('hidden');
        return;
    }

    btn.disabled = true;
    msg.classList.remove('hidden');
    progressWrap.classList.remove('hidden');
    btn.innerText = '更新中...';
    setProgress(0, '准备开始...');

    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(pollProgress, 500);

    try {
        const params = new URLSearchParams();
        enabledHandles.forEach(h => params.append('handles', h));
        const response = await fetch(`/api/update?${params.toString()}`);
        if (!response.ok) throw new Error('update request failed');
        modelData = await response.json();
        refreshUI();
        await pollProgress();
        setProgress(100, '数据更新成功');
    } catch (err) {
        await pollProgress();
        setProgress(0, '更新失败，请检查服务端日志');
    } finally {
        if (progressTimer) {
            clearInterval(progressTimer);
            progressTimer = null;
        }
        btn.disabled = false;
        msg.classList.add('hidden');
        btn.innerText = '🔄 立即更新数据';
    }
}

function setProgress(percent, text) {
    const bar = document.getElementById('updateProgressBar');
    const label = document.getElementById('progressText');
    const safePercent = Math.max(0, Math.min(100, percent));
    bar.style.width = `${safePercent}%`;
    label.innerText = text || `${Math.round(safePercent)}%`;
}

async function pollProgress() {
    try {
        const response = await fetch('/api/update/status');
        if (!response.ok) return;
        const status = await response.json();
        if (!status.total || status.total <= 0) {
            setProgress(0, '等待服务端任务...');
            return;
        }

        const percent = (status.completed / status.total) * 100;
        const runningText = status.current
            ? `进度 ${status.completed}/${status.total}（正在处理 ${status.current}）`
            : `进度 ${status.completed}/${status.total}`;

        if (status.error) {
            setProgress(percent, `失败：${status.error}`);
            return;
        }

        if (status.running) {
            setProgress(percent, runningText);
        } else {
            setProgress(percent, `完成 ${status.completed}/${status.total}`);
        }
    } catch (err) {
        // Keep UI responsive even if status polling fails intermittently.
    }
}

function refreshUI() {
    const activeData = getActiveData();
    syncChartHiddenModelsWithData(activeData);
    const chartData = applyChartView(activeData);
    renderChart(chartData);
    renderChartModelLegend(chartData);
    renderTable(applyView(activeData));
}

function extractNum(s) {
    const match = s.match(/[\$]?([\d\.]+)/);
    return match ? parseFloat(match[1]) : 0;
}

function renderChart(data) {
    const ctx = document.getElementById('priceChart').getContext('2d');
    const labels = data.map(m => m.handle);
    const inputPrices = data.map(m => chartHiddenModels.has(m.handle) ? null : extractNum(m.input.usd));
    const outputPrices = data.map(m => chartHiddenModels.has(m.handle) ? null : extractNum(m.output.usd));
    if (myChart) myChart.destroy();
    myChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                { label: 'Input (USD/1M)', data: inputPrices, backgroundColor: 'rgba(46, 204, 113, 0.6)', borderColor: '#2ecc71', borderWidth: 1 },
                { label: 'Output (USD/1M)', data: outputPrices, backgroundColor: 'rgba(52, 152, 219, 0.6)', borderColor: '#3498db', borderWidth: 1 }
            ]
        },
        options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true } }, plugins: { legend: { position: 'bottom' } } }
    });
}

function renderChartModelLegend(data) {
    const wrap = document.getElementById('chartModelLegend');
    if (!wrap) return;
    wrap.innerHTML = data.map(m => {
        const hidden = chartHiddenModels.has(m.handle);
        const cls = hidden ? 'chart-model-chip is-hidden' : 'chart-model-chip';
        return `<button type="button" class="${cls}" data-handle="${encodeURIComponent(m.handle)}">${escapeHtml(m.handle)}</button>`;
    }).join('');
}

function renderTable(data) {
    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = data.map(m => `
        <tr>
            <td><a class="table-model-link" href="https://poe.com/${encodeURIComponent(m.handle)}" target="_blank" rel="noopener noreferrer">${escapeHtml(m.handle)}</a></td>
            <td><div class="price-usd">${m.input.usd}</div><div class="price-pts">${m.input.points}</div></td>
            <td><div class="price-usd">${m.output.usd}</div><div class="price-pts">${m.output.points}</div></td>
            <td><span class="cache-txt">${m.cache_discount}</span></td>
        </tr>
    `).join('');
}

function setupEventListeners() {
    document.getElementById('updateBtn').addEventListener('click', updateData);
    document.getElementById('addModelBtn').addEventListener('click', addModel);
    document.getElementById('modelList').addEventListener('click', async (e) => {
        const btn = e.target.closest('button[data-action]');
        if (!btn) return;
        const handle = decodeURIComponent(btn.dataset.handle || '');
        if (!handle) return;
        const action = btn.dataset.action;
        if (action === 'toggle') {
            toggleModelDisabled(handle);
            return;
        }
        if (action === 'delete') {
            await deleteModel(handle);
        }
    });
    document.getElementById('filterInput').addEventListener('input', (e) => {
        const activeData = getActiveData();
        renderTable(applyView(activeData));
    });
    document.getElementById('groupByPrefixToggle').addEventListener('change', (e) => {
        groupByPrefixEnabled = Boolean(e.target.checked);
        const activeData = getActiveData();
        renderTable(applyView(activeData));
    });
    document.querySelectorAll('th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
            const key = th.dataset.sort;
            if (currentSort.key === key) currentSort.asc = !currentSort.asc;
            else currentSort = { key, asc: true };
            const activeData = getActiveData();
            renderTable(applyView(activeData));
        });
    });
    document.getElementById('chartSortKey').addEventListener('change', (e) => {
        chartSort.key = e.target.value || 'none';
        refreshUI();
    });
    document.getElementById('chartSortDirBtn').addEventListener('click', () => {
        chartSort.asc = !chartSort.asc;
        updateChartSortDirText();
        refreshUI();
    });
    document.getElementById('chartModelLegend').addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-handle]');
        if (!btn) return;
        const handle = decodeURIComponent(btn.dataset.handle || '');
        if (!handle) return;
        toggleChartModelHidden(handle);
    });
    updateChartSortDirText();
}

function loadDisabledModels() {
    try {
        const raw = localStorage.getItem(DISABLED_MODELS_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        disabledModels = new Set(Array.isArray(parsed) ? parsed : []);
    } catch (err) {
        disabledModels = new Set();
    }
}

function saveDisabledModels() {
    localStorage.setItem(DISABLED_MODELS_KEY, JSON.stringify(Array.from(disabledModels)));
}

function loadChartHiddenModels() {
    try {
        const raw = localStorage.getItem(CHART_HIDDEN_MODELS_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        chartHiddenModels = new Set(Array.isArray(parsed) ? parsed : []);
    } catch (err) {
        chartHiddenModels = new Set();
    }
}

function saveChartHiddenModels() {
    localStorage.setItem(CHART_HIDDEN_MODELS_KEY, JSON.stringify(Array.from(chartHiddenModels)));
}

function syncDisabledModelsWithConfig() {
    const cfgSet = new Set(modelConfig);
    const next = new Set();
    disabledModels.forEach(handle => {
        if (cfgSet.has(handle)) next.add(handle);
    });
    disabledModels = next;
    saveDisabledModels();
}

function syncChartHiddenModelsWithData(data) {
    const dataSet = new Set(data.map(m => m.handle));
    const next = new Set();
    chartHiddenModels.forEach(handle => {
        if (dataSet.has(handle)) next.add(handle);
    });
    chartHiddenModels = next;
    saveChartHiddenModels();
}

function isModelEnabled(handle) {
    return !disabledModels.has(handle);
}

function toggleModelDisabled(handle) {
    if (disabledModels.has(handle)) disabledModels.delete(handle);
    else disabledModels.add(handle);
    saveDisabledModels();
    renderModelConfig();
    refreshUI();
}

function toggleChartModelHidden(handle) {
    if (chartHiddenModels.has(handle)) chartHiddenModels.delete(handle);
    else chartHiddenModels.add(handle);
    saveChartHiddenModels();
    const activeData = getActiveData();
    const chartData = applyChartView(activeData);
    renderChart(chartData);
    renderChartModelLegend(chartData);
}

function getActiveData() {
    return modelData.filter(m => isModelEnabled(m.handle));
}

function applyView(data) {
    const term = document.getElementById('filterInput').value.trim().toLowerCase();
    let out = term ? data.filter(m => m.handle.toLowerCase().includes(term)) : [...data];
    if (groupByPrefixEnabled) {
        out.sort(compareGroupedModels);
        return out;
    }
    const key = currentSort.key;
    if (!key) return out;
    out.sort((a, b) => compareBySortKey(a, b, key, currentSort.asc));
    return out;
}

function compareGroupedModels(a, b) {
    const prefixCompare = compareValues(getModelPrefix(a.handle), getModelPrefix(b.handle));
    if (prefixCompare !== 0) return prefixCompare;

    const priceKey = currentSort.key === 'output_usd' ? 'output_usd' : 'input_usd';
    const priceAsc = currentSort.key ? currentSort.asc : true;
    const priceCompare = compareBySortKey(a, b, priceKey, priceAsc);
    if (priceCompare !== 0) return priceCompare;

    return compareValues(a.handle.toLowerCase(), b.handle.toLowerCase());
}

function compareBySortKey(a, b, key, asc) {
    let valA;
    let valB;
    if (key === 'handle') {
        valA = a.handle.toLowerCase();
        valB = b.handle.toLowerCase();
    } else if (key === 'input_usd') {
        valA = extractNum(a.input.usd);
        valB = extractNum(b.input.usd);
    } else if (key === 'output_usd') {
        valA = extractNum(a.output.usd);
        valB = extractNum(b.output.usd);
    } else {
        return 0;
    }
    const compare = compareValues(valA, valB);
    return asc ? compare : -compare;
}

function compareValues(a, b) {
    if (a > b) return 1;
    if (a < b) return -1;
    return 0;
}

function getModelPrefix(handle) {
    const normalized = handle.trim();
    const match = normalized.match(/^[A-Za-z][A-Za-z0-9]*/);
    if (match) return match[0].toUpperCase();
    const token = normalized.split(/[-_\s]+/).find(Boolean);
    return (token || normalized).toUpperCase();
}

function applyChartView(data) {
    const out = [...data];
    const key = chartSort.key;
    if (!key || key === 'none') return out;
    out.sort((a, b) => {
        let valA;
        let valB;
        if (key === 'handle') {
            valA = a.handle;
            valB = b.handle;
        } else if (key === 'input_usd') {
            valA = extractNum(a.input.usd);
            valB = extractNum(b.input.usd);
        } else if (key === 'output_usd') {
            valA = extractNum(a.output.usd);
            valB = extractNum(b.output.usd);
        } else {
            return 0;
        }
        const compare = valA > valB ? 1 : (valA < valB ? -1 : 0);
        return chartSort.asc ? compare : -compare;
    });
    return out;
}

function updateChartSortDirText() {
    const btn = document.getElementById('chartSortDirBtn');
    if (!btn) return;
    btn.innerText = chartSort.asc ? '升序' : '降序';
}

function escapeHtml(str) {
    return str
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

init();
