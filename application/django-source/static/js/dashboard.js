// PayWay dashboard charts (Chart.js 4, served locally)
(function () {
    if (typeof Chart === 'undefined') return;

    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
    const C = {
        brand: token('--pw-brand', '#047857'),
        text: token('--pw-text', '#09090b'),
        muted: token('--pw-muted', '#71717a'),
        border: token('--pw-border', '#e4e4e7'),
        blue: '#3b82f6',
        amber: '#f59e0b',
        violet: '#8b5cf6',
        out: '#cbd5e1',
    };
    const TONES = { brand: C.brand, blue: C.blue, amber: C.amber, violet: C.violet };
    const money = v => '$' + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
    const moneyFull = v => '$' + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const compact = v => {
        const n = Math.abs(v);
        if (n >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
        if (n >= 1e3) return '$' + (v / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'K';
        return '$' + Math.round(v);
    };

    Chart.defaults.font.family = token('--pw-font', 'Geist, system-ui, sans-serif');
    Chart.defaults.font.size = 11;
    Chart.defaults.color = C.muted;
    Chart.defaults.animation.duration = 500;

    const tooltip = {
        backgroundColor: '#ffffff',
        titleColor: C.muted,
        bodyColor: C.text,
        borderColor: C.border,
        borderWidth: 1,
        padding: 10,
        cornerRadius: 10,
        displayColors: false,
        titleFont: { size: 11, weight: '500' },
        bodyFont: { size: 13, weight: '600' },
        caretSize: 0,
    };

    function fade(ctx, area, hex) {
        const g = ctx.createLinearGradient(0, area.top, 0, area.bottom);
        g.addColorStop(0, hex + '33');
        g.addColorStop(1, hex + '00');
        return g;
    }

    // ---------- sparklines ----------
    document.querySelectorAll('canvas.pw-spark').forEach(el => {
        let data = [];
        try { data = JSON.parse(el.dataset.spark || '[]'); } catch (e) { /* ignore */ }
        if (!data.length) return;
        const color = TONES[el.dataset.tone] || C.brand;
        new Chart(el, {
            type: 'line',
            data: {
                labels: data.map((_, i) => i),
                datasets: [{
                    data,
                    borderColor: color,
                    borderWidth: 1.75,
                    cubicInterpolationMode: 'monotone',
                    pointRadius: 0,
                    fill: true,
                    backgroundColor: c => c.chart.chartArea ? fade(c.chart.ctx, c.chart.chartArea, color) : 'transparent',
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
                scales: { x: { display: false }, y: { display: false } },
                events: [],
            },
        });
    });

    const dataEl = document.getElementById('pw-dashboard-data');
    if (!dataEl) return;
    const D = JSON.parse(dataEl.textContent);

    const axis = {
        x: { grid: { display: false }, border: { display: false }, ticks: { maxTicksLimit: 6, maxRotation: 0 } },
        y: { grid: { color: C.border, drawTicks: false }, border: { display: false }, ticks: { padding: 8, callback: compact, maxTicksLimit: 5 } },
    };

    // ---------- balance area ----------
    const balanceEl = document.getElementById('balanceChart');
    if (balanceEl) {
        new Chart(balanceEl, {
            type: 'line',
            data: {
                labels: D.labels,
                datasets: [{
                    data: D.balance,
                    borderColor: C.brand,
                    borderWidth: 2,
                    cubicInterpolationMode: 'monotone',
                    fill: true,
                    backgroundColor: c => c.chart.chartArea ? fade(c.chart.ctx, c.chart.chartArea, C.brand) : 'transparent',
                    pointRadius: ctx => ctx.dataIndex === D.balance.length - 1 ? 4 : 0,
                    pointBackgroundColor: '#fff',
                    pointBorderColor: C.brand,
                    pointBorderWidth: 2,
                    pointHoverRadius: 5,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { display: false }, tooltip: { ...tooltip, callbacks: { label: c => moneyFull(c.parsed.y) } } },
                scales: axis,
            },
        });
    }

    // ---------- cash flow combo ----------
    const flowEl = document.getElementById('flowChart');
    if (flowEl) {
        new Chart(flowEl, {
            data: {
                labels: D.combo.labels,
                datasets: [
                    { type: 'bar', label: 'Money in', data: D.combo.in, backgroundColor: C.brand, borderRadius: 6, barPercentage: 0.7, categoryPercentage: 0.6, yAxisID: 'y', order: 2 },
                    { type: 'bar', label: 'Money out', data: D.combo.out, backgroundColor: C.out, borderRadius: 6, barPercentage: 0.7, categoryPercentage: 0.6, yAxisID: 'y', order: 2 },
                    { type: 'line', label: 'Transactions', data: D.combo.count, borderColor: C.blue, borderWidth: 2, cubicInterpolationMode: 'monotone', pointRadius: 3, pointBackgroundColor: '#fff', pointBorderColor: C.blue, pointBorderWidth: 2, yAxisID: 'y1', order: 1 },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        ...tooltip,
                        displayColors: true,
                        boxWidth: 8, boxHeight: 8, usePointStyle: true,
                        bodyFont: { size: 12, weight: '500' },
                        callbacks: { label: c => ` ${c.dataset.label}: ${c.dataset.yAxisID === 'y1' ? c.parsed.y : moneyFull(c.parsed.y)}` },
                    },
                },
                scales: {
                    x: axis.x,
                    y: axis.y,
                    y1: { position: 'right', grid: { display: false }, border: { display: false }, beginAtZero: true, ticks: { precision: 0, maxTicksLimit: 5 } },
                },
            },
        });
    }

    // ---------- spending mix donut ----------
    const mixEl = document.getElementById('mixChart');
    if (mixEl) {
        const has = D.mix.length > 0;
        new Chart(mixEl, {
            type: 'doughnut',
            data: {
                labels: has ? D.mix.map(m => m.label) : ['None'],
                datasets: [{
                    data: has ? D.mix.map(m => m.value) : [1],
                    backgroundColor: has ? D.mix.map(m => m.color) : [C.border],
                    borderWidth: 0,
                    spacing: has && D.mix.length > 1 ? 3 : 0,
                    borderRadius: has && D.mix.length > 1 ? 6 : 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '76%',
                plugins: {
                    legend: { display: false },
                    tooltip: has ? { ...tooltip, callbacks: { label: c => `${c.label}: ${money(c.parsed)}` } } : { enabled: false },
                },
            },
        });
    }
})();

document.querySelectorAll('[data-pw-print]').forEach(btn => btn.addEventListener('click', () => window.print()));
