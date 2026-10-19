// Shared check-library and per-server panels, using the dashboard's existing forms.
function privacyChecksPanel() {
    const match = window.location.pathname.match(/^\/targets\/([^/]+)\/tools$/);
    const scope = match ? decodeURIComponent(match[1]) : null;
    return {
        scope, providers: [], checks: [], assignments: [], tools: [], loading: true, busy: false,
        error: '', notice: '', health: {}, selectedCheck: '', selectedTool: '*',
        modal: '', editing: null, previewText: 'Sarah Johnson sent a message to Alex Example.', previewResult: '',
        providerForm: {}, checkForm: {}, entitiesText: '', pathsText: '',
        api() { return this.scope ? `/api/targets/${encodeURIComponent(this.scope)}/privacy` : '/api/privacy'; },
        async request(url, body, method = 'POST') {
            const response = await fetch(url, body === undefined ? undefined : {
                method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
            });
            const data = await response.json();
            if (!response.ok) {
                const detail = (data.details || []).map(d => `${d.field}: ${d.message}`).join('; ');
                throw new Error(detail || data.error || 'Request failed');
            }
            return data;
        },
        async init() {
            await this.load();
            if (this.scope) {
                try {
                    const data = await this.request(`/api/targets/${encodeURIComponent(this.scope)}/tools`);
                    this.tools = data.tools || [];
                } catch (error) { this.error = error.message; }
            }
        },
        async load() {
            try {
                const data = await this.request(this.api());
                this.providers = data.providers; this.checks = data.checks; this.assignments = data.assignments;
            } catch (error) { this.error = error.message; }
            finally { this.loading = false; }
        },
        async perform(action, message) {
            if (this.busy) return;
            this.busy = true; this.error = ''; this.notice = '';
            try { await action(); await this.load(); this.notice = message; }
            catch (error) { this.error = error.message; }
            finally { this.busy = false; }
        },
        get ownChecks() { return this.checks.filter(c => c.scope === this.scope); },
        get reusableChecks() { return this.checks.filter(c => c.scope === null); },
        check(id) { return this.checks.find(c => c.id === id); },
        providerName(id) { return this.providers.find(p => p.id === id)?.name || 'Unavailable analyzer'; },
        scopeLabel(item) { return item.scope === null ? 'Shared' : 'Only this server'; },
        openProvider(provider) {
            this.editing = provider?.id || null;
            this.providerForm = provider ? {name: provider.name, adapter: provider.adapter, endpoint: provider.endpoint}
                : {name: '', adapter: 'presidio', endpoint: 'http://127.0.0.1:5002'};
            this.error = ''; this.modal = 'provider';
        },
        openCheck(check) {
            this.editing = check?.id || null;
            this.checkForm = check ? {
                name: check.name, provider_id: check.provider_id, language: check.language,
                threshold: check.threshold, action: check.action
            } : {name: '', provider_id: this.providers[0]?.id || '', language: 'en', threshold: 0.7, action: 'mask'};
            this.entitiesText = check ? check.entities.join(', ') : 'PERSON';
            this.pathsText = check ? check.field_paths.join(', ') : '';
            this.error = ''; this.previewResult = ''; this.modal = 'check';
        },
        checkPayload() {
            return {...this.checkForm, threshold: Number(this.checkForm.threshold),
                entities: this.entitiesText.split(/[,\n]/).map(s => s.trim()).filter(Boolean),
                field_paths: this.pathsText.split(/[,\n]/).map(s => s.trim()).filter(Boolean)};
        },
        saveProvider() {
            return this.perform(async () => {
                const path = this.editing ? `providers/${this.editing}/update` : 'providers/create';
                await this.request(`${this.api()}/${path}`, this.providerForm); this.modal = '';
            }, 'Analyzer saved');
        },
        saveCheck() {
            return this.perform(async () => {
                const isNew = !this.editing;
                const path = this.editing ? `checks/${this.editing}/update` : 'checks/create';
                const check = await this.request(`${this.api()}/${path}`, this.checkPayload());
                // A newly created server-only check is enabled on that server immediately.
                if (this.scope && isNew) await this.request(`${this.api()}/assignments/create`, {
                    check_id: check.id, tool_name: this.selectedTool, active: true
                });
                this.modal = '';
            }, this.scope ? 'Server check saved' : 'Shared check saved');
        },
        attach() {
            if (!this.selectedCheck) return;
            return this.perform(() => this.request(`${this.api()}/assignments/create`, {
                check_id: this.selectedCheck, tool_name: this.selectedTool, active: true
            }), 'Check added to this server');
        },
        toggle(binding) {
            return this.perform(() => this.request(`${this.api()}/assignments/${binding.id}/update`, {
                check_id: binding.check_id, tool_name: binding.tool_name, active: !binding.active
            }), binding.active ? 'Check paused on this server' : 'Check enabled on this server');
        },
        detach(binding) {
            if (!confirm('Remove this check from this server? Its saved definition will remain.')) return;
            return this.perform(() => this.request(`${this.api()}/assignments/${binding.id}/delete`, {}), 'Check removed from this server');
        },
        remove(kind, item) {
            if (!confirm(`Delete ${item.name}? Checks in use must be removed from their servers first.`)) return;
            return this.perform(() => this.request(`${this.api()}/${kind}/${item.id}/delete`, {}), 'Definition deleted');
        },
        async preview() {
            this.busy = true; this.error = ''; this.previewResult = '';
            try {
                const data = await this.request(`${this.api()}/preview`, {check: this.checkPayload(), text: this.previewText});
                this.previewResult = data.text;
            } catch (error) { this.error = error.message; }
            finally { this.busy = false; }
        },
        async connection(provider) {
            this.busy = true; this.error = '';
            try {
                await this.request(`${this.api()}/preview`, {check: {
                    name: 'Connection check', provider_id: provider.id, entities: ['PERSON'], language: 'en'
                }, text: 'Alex Example sent a message.'});
                this.health[provider.id] = 'Connected';
            } catch (error) { this.health[provider.id] = 'Unavailable'; this.error = error.message; }
            finally { this.busy = false; }
        }
    };
}
