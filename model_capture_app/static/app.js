(function () {
    let boot = {};
    const bootEl = document.getElementById('model-capture-boot');
    if (bootEl && bootEl.textContent) {
        try {
            boot = JSON.parse(bootEl.textContent);
        } catch (err) {
            boot = {};
        }
    }
    const latestFromServer = boot.latestCapture || null;
    const adminMode = Boolean(boot.adminMode);

    const els = {
        settingsForm: document.getElementById('settings-form'),
        settingsStatus: document.getElementById('settings-status'),
        captureEmpty: document.getElementById('capture-empty'),
        confirmForm: document.getElementById('confirm-form'),
        confirmStatus: document.getElementById('confirm-status'),
        orderName: document.getElementById('order-name'),
        orderLink: document.getElementById('order-link'),
        orderWeight: document.getElementById('order-weight'),
        orderProfile: document.getElementById('order-profile'),
        orderColor: document.getElementById('order-color'),
        orderQuantity: document.getElementById('order-quantity'),
        targetUserId: document.getElementById('target-user-id'),
        orderOwnerUserId: document.getElementById('order-owner-user-id')
    };

    let lastCaptureId = '';

    function setStatus(target, text, isError) {
        if (!target) return;
        target.textContent = text || '';
        target.style.color = isError ? '#a13030' : '#1a6a41';
    }

    function applyLatestCapture(latest) {
        if (!latest || !latest.suggested_order) {
            els.captureEmpty.classList.remove('hidden');
            els.confirmForm.classList.add('hidden');
            return;
        }

        const marker = String(latest.captured_at || latest.model?.captured_at || latest.model?.model_url || '');
        if (marker && marker === lastCaptureId) {
            return;
        }
        lastCaptureId = marker;

        const suggestion = latest.suggested_order;
        els.orderName.value = suggestion.name || '';
        els.orderLink.value = suggestion.link || '';
        els.orderWeight.value = Number(suggestion.print_weight_g || 0);
        els.orderProfile.value = suggestion.profile || '1';
        els.orderColor.value = suggestion.color || 'Bamboo Green PLA';
        els.orderQuantity.value = Number(suggestion.quantity || 1);

        els.captureEmpty.classList.add('hidden');
        els.confirmForm.classList.remove('hidden');

        const title = latest.model?.title || suggestion.name || 'Captured model';
        setStatus(els.confirmStatus, 'Ready to confirm: ' + title, false);
    }

    async function pollLatestCapture() {
        try {
            const res = await fetch('/model-capture-app/api/latest', {cache: 'no-store'});
            const payload = await res.json();
            if (!res.ok || !payload.ok) {
                throw new Error(payload.error || 'Could not read latest capture');
            }
            applyLatestCapture(payload.latest);
        } catch (err) {
            setStatus(els.confirmStatus, (err && err.message) || 'Capture polling failed', true);
        }
    }

    if (latestFromServer) {
        applyLatestCapture(latestFromServer);
    }

    setInterval(pollLatestCapture, 3000);

    els.settingsForm?.addEventListener('submit', async (evt) => {
        evt.preventDefault();
        const formData = new FormData(els.settingsForm);
        const payload = {
            default_profile: String(formData.get('default_profile') || '').trim(),
            default_filament: String(formData.get('default_filament') || '').trim(),
            default_quantity: Number(formData.get('default_quantity') || 1),
            open_cart_after_confirm: Boolean(formData.get('open_cart_after_confirm'))
        };
        if (adminMode && els.targetUserId) {
            payload.target_user_id = String(els.targetUserId.value || '').trim();
        }

        try {
            const res = await fetch('/model-capture-app/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (!res.ok || !data.ok) {
                throw new Error(data.error || 'Failed to save settings');
            }
            setStatus(els.settingsStatus, 'Settings saved.', false);
        } catch (err) {
            setStatus(els.settingsStatus, (err && err.message) || 'Failed to save settings', true);
        }
    });

    els.confirmForm?.addEventListener('submit', async (evt) => {
        evt.preventDefault();
        const payload = {
            name: els.orderName.value,
            link: els.orderLink.value,
            print_weight_g: Number(els.orderWeight.value || 0),
            profile: els.orderProfile.value,
            color: els.orderColor.value,
            quantity: Number(els.orderQuantity.value || 1)
        };
        if (adminMode && els.orderOwnerUserId) {
            payload.owner_user_id = String(els.orderOwnerUserId.value || '').trim();
        }

        try {
            const res = await fetch('/model-capture-app/api/confirm', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (!res.ok || !data.ok) {
                throw new Error(data.error || 'Could not confirm order');
            }
            setStatus(els.confirmStatus, 'Order #' + data.order_id + ' added to cart.', false);
            if (data.redirect_url) {
                setTimeout(() => {
                    window.location.href = data.redirect_url;
                }, 900);
            }
        } catch (err) {
            setStatus(els.confirmStatus, (err && err.message) || 'Confirm failed', true);
        }
    });
})();
