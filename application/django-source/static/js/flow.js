// PayWay form flows: multi-step forms, live summaries, file pickers.
(function () {
    'use strict';

    const money = (v) => {
        const n = parseFloat(v);
        return isNaN(n) ? '—' : '$' + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    function displayValue(form, name) {
        const els = form.querySelectorAll(`[name="${name}"]`);
        if (!els.length) return '';
        const el = els[0];
        if (el.type === 'radio') {
            const checked = form.querySelector(`[name="${name}"]:checked`);
            return checked ? (checked.dataset.label || checked.closest('label')?.textContent.trim().replace(/\s+/g, ' ') || checked.value) : '';
        }
        if (el.type === 'checkbox') return el.checked ? 'Yes' : 'No';
        if (el.tagName === 'SELECT') return el.value ? el.options[el.selectedIndex].text : '';
        if (el.type === 'file') return el.files && el.files[0] ? el.files[0].name : '';
        if (el.type === 'password') return el.value ? '•'.repeat(Math.min(el.value.length, 12)) : '';
        return el.value.trim();
    }

    // ---------- live bindings: [data-pw-bind="field"] mirrors an input ----------
    function initBindings(form) {
        const targets = document.querySelectorAll('[data-pw-bind]');
        if (!targets.length) return;
        const update = () => {
            targets.forEach((t) => {
                let v = displayValue(form, t.dataset.pwBind);
                if (t.dataset.pwFormat === 'money') v = v ? money(v) : '';
                if (t.dataset.pwFormat === 'card') v = v ? v.replace(/\D/g, '').replace(/(.{4})/g, '$1 ').trim() : '';
                if (t.dataset.pwFormat === 'mask' && v) v = '•••• ' + v.replace(/\D/g, '').slice(-4);
                t.textContent = v || t.dataset.pwEmpty || '—';
                t.classList.toggle('is-empty', !v);
            });
            form.dispatchEvent(new CustomEvent('pw:change'));
        };
        form.addEventListener('input', update);
        form.addEventListener('change', update);
        update();
    }

    // ---------- multi-step ----------
    function initSteps(form) {
        const steps = [...form.querySelectorAll('.pw-step')];
        if (steps.length < 2) return;
        const stepper = document.querySelector('[data-pw-stepper]');
        const offset = parseInt(form.dataset.pwStepOffset || '0', 10); // stepper items before the client steps
        let i = 0;

        const firstErrorStep = steps.findIndex((s) => s.querySelector('.is-invalid, .has-error, .errorlist'));
        if (firstErrorStep > -1) i = firstErrorStep;

        function render() {
            steps.forEach((s, idx) => {
                s.hidden = idx !== i;
                s.classList.toggle('is-active', idx === i);
            });
            form.querySelectorAll('[data-pw-back]').forEach((b) => (b.hidden = i === 0));
            form.querySelectorAll('[data-pw-next]').forEach((b) => (b.hidden = i === steps.length - 1));
            form.querySelectorAll('[data-pw-submit]').forEach((b) => (b.hidden = i !== steps.length - 1));
            form.querySelectorAll('[data-pw-count]').forEach((el) => (el.textContent = `Step ${i + 1} of ${steps.length}`));
            if (stepper) {
                const items = [...stepper.querySelectorAll('.pw-stepper-item')];
                const active = i + offset;
                items.forEach((li, idx) => {
                    li.classList.toggle('is-done', idx < active);
                    li.classList.toggle('is-current', idx === active);
                    li.classList.toggle('is-todo', idx > active);
                    li.toggleAttribute('aria-current', idx === active);
                    const dot = li.querySelector('.pw-stepper-dot');
                    dot.innerHTML = idx < active ? '<i class="fas fa-check"></i>' : String(idx + 1);
                });
                const pct = items.length > 1 ? (active / (items.length - 1)) * 100 : 0;
                stepper.style.setProperty('--pw-progress', pct + '%');
            }
            form.dispatchEvent(new CustomEvent('pw:step', { detail: { index: i, total: steps.length } }));
        }

        function valid(step) {
            const fields = [...step.querySelectorAll('input, select, textarea')].filter((f) => !f.disabled && f.type !== 'hidden');
            for (const f of fields) {
                if (!f.checkValidity()) {
                    f.classList.add('is-invalid');
                    f.reportValidity();
                    f.focus();
                    return false;
                }
                f.classList.remove('is-invalid');
            }
            return true;
        }

        function go(to) {
            i = Math.max(0, Math.min(steps.length - 1, to));
            render();
            const top = form.getBoundingClientRect().top + window.scrollY - 96;
            if (window.scrollY > top) window.scrollTo({ top, behavior: 'smooth' });
            const first = steps[i].querySelector('input:not([type=hidden]), select, textarea');
            if (first) setTimeout(() => first.focus({ preventScroll: true }), 50);
        }

        form.addEventListener('click', (e) => {
            if (e.target.closest('[data-pw-next]')) { e.preventDefault(); if (valid(steps[i])) go(i + 1); }
            if (e.target.closest('[data-pw-back]')) { e.preventDefault(); go(i - 1); }
            const jump = e.target.closest('[data-pw-goto]');
            if (jump) { e.preventDefault(); go(parseInt(jump.dataset.pwGoto, 10)); }
        });
        // Enter on a non-final step advances instead of submitting
        form.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && e.target.tagName === 'INPUT' && i < steps.length - 1) {
                e.preventDefault();
                if (valid(steps[i])) go(i + 1);
            }
        });
        form.addEventListener('submit', (e) => {
            for (let k = 0; k < steps.length; k++) {
                if (!valid(steps[k])) { e.preventDefault(); go(k); return; }
            }
        });
        render();
    }

    // ---------- file pickers ----------
    function initFiles(root) {
        root.querySelectorAll('.pw-dropzone input[type=file]').forEach((input) => {
            const zone = input.closest('.pw-dropzone');
            const name = zone.querySelector('[data-pw-filename]');
            const show = () => {
                const f = input.files && input.files[0];
                zone.classList.toggle('has-file', !!f);
                if (name) name.textContent = f ? `${f.name} · ${(f.size / 1024).toFixed(0)} KB` : '';
            };
            input.addEventListener('change', show);
            ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('is-drag'); }));
            ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove('is-drag')));
            zone.addEventListener('drop', (e) => {
                e.preventDefault();
                if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); }
            });
        });
    }

    // ---------- password reveal + strength ----------
    function initPasswords(root) {
        root.querySelectorAll('[data-pw-reveal]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const input = document.getElementById(btn.dataset.pwReveal);
                if (!input) return;
                const show = input.type === 'password';
                input.type = show ? 'text' : 'password';
                btn.querySelector('i').className = show ? 'far fa-eye-slash' : 'far fa-eye';
                btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
            });
        });
        root.querySelectorAll('[data-pw-strength]').forEach((meter) => {
            const input = document.getElementById(meter.dataset.pwStrength);
            if (!input) return;
            const label = meter.querySelector('[data-pw-strength-label]');
            input.addEventListener('input', () => {
                const v = input.value;
                let score = 0;
                if (v.length >= 8) score++;
                if (v.length >= 12) score++;
                if (/[A-Z]/.test(v) && /[a-z]/.test(v)) score++;
                if (/\d/.test(v)) score++;
                if (/[^A-Za-z0-9]/.test(v)) score++;
                const level = !v ? 0 : score <= 2 ? 1 : score <= 3 ? 2 : 3;
                meter.dataset.level = level;
                if (label) label.textContent = ['', 'Weak', 'Good', 'Strong'][level];
            });
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('form[data-pw-flow]').forEach((form) => {
            initSteps(form);
            initBindings(form);
        });
        initFiles(document);
        initPasswords(document);
        document.querySelectorAll('[data-pw-copy]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const src = document.getElementById(btn.dataset.pwCopy);
                if (!src || !navigator.clipboard) return;
                const text = src.children.length ? [...src.children].map((c) => c.textContent.trim()).join('\n') : src.textContent.trim();
                navigator.clipboard.writeText(text).then(() => {
                    const old = btn.innerHTML;
                    btn.innerHTML = '<i class="fas fa-check"></i>';
                    setTimeout(() => (btn.innerHTML = old), 1500);
                });
            });
        });
    });
})();
