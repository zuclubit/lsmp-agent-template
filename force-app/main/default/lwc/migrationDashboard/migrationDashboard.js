/**
 * @description  Migration Dashboard LWC — Controller
 *               Fetches migration stats from the LSMP API and renders
 *               the real-time dashboard with KPIs, object breakdown, and error log.
 * @author       Migration Platform Team
 * @lastModified 2026-03-17
 */
import { LightningElement, track, api } from 'lwc';
import { ShowToastEvent } from 'lightning/platformShowToastEvent';
import getMigrationStats     from '@salesforce/apex/MigrationDashboardController.getMigrationStats';
import getObjectBreakdown    from '@salesforce/apex/MigrationDashboardController.getObjectBreakdown';
import getRecentBatchRuns    from '@salesforce/apex/MigrationDashboardController.getRecentBatchRuns';
import getRecentErrors       from '@salesforce/apex/MigrationDashboardController.getRecentErrors';
import runMigrationBatch     from '@salesforce/apex/MigrationDashboardController.runMigrationBatch';

const ERROR_COLUMNS = [
    { label: 'Record ID',    fieldName: 'legacyId',    type: 'text',   sortable: true,  initialWidth: 160 },
    { label: 'Object',       fieldName: 'objectType',  type: 'text',   sortable: true,  initialWidth: 100 },
    { label: 'Error Code',   fieldName: 'errorCode',   type: 'text',   sortable: false, initialWidth: 160 },
    { label: 'Message',      fieldName: 'errorMessage',type: 'text',   sortable: false, wrapText: true },
    { label: 'Batch',        fieldName: 'batchId',     type: 'text',   sortable: true,  initialWidth: 160 },
    { label: 'Occurred At',  fieldName: 'createdAt',   type: 'date',   sortable: true,  initialWidth: 160,
      typeAttributes: { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
    },
    { label: 'Retryable',    fieldName: 'isRetryable', type: 'boolean',sortable: false, initialWidth: 90 },
];

export default class MigrationDashboard extends LightningElement {

    @api autoRefreshInterval = 30;

    // ── tracked state ──────────────────────────────────────────────────────
    @track stats             = {};
    @track objectBreakdown   = [];
    @track recentBatchRuns   = [];
    @track errorRows         = [];
    @track isLoading         = true;
    @track hasGlobalError    = false;
    @track globalErrorMessage = '';
    @track isBatchRunning    = false;
    @track showBatchModal    = false;
    @track autoRefreshEnabled = true;
    @track lastRefreshed     = '—';
    @track sortedBy          = 'createdAt';
    @track sortedDirection   = 'desc';
    @track rowNumberOffset   = 0;
    @track allErrorsLoaded   = false;

    @track batchConfig = {
        objectName: 'Account',
        batchSize:  200,
        dryRun:     true,
    };

    // wire results (kept for refreshApex)
    _wiredStats;
    _wiredBreakdown;
    _wiredBatchRuns;
    _wiredErrors;

    _refreshTimer = null;
    _errorPage    = 1;
    _errorPageSize = 25;

    // ── object options ──────────────────────────────────────────────────────
    get objectOptions() {
        return [
            { label: 'Account',     value: 'Account'     },
            { label: 'Contact',     value: 'Contact'     },
            { label: 'Opportunity', value: 'Opportunity' },
        ];
    }

    get errorColumns()        { return ERROR_COLUMNS; }
    get hasObjectBreakdown()  { return this.objectBreakdown && this.objectBreakdown.length > 0; }
    get hasBatchRuns()        { return this.recentBatchRuns && this.recentBatchRuns.length > 0; }
    get hasErrors()           { return this.errorRows && this.errorRows.length > 0; }
    get errorCountLabel()     { return `${this.errorRows.length} errors`; }
    get runBatchLabel()       { return this.isBatchRunning ? 'Batch Running…' : 'Run Batch'; }

    get progressVariant() {
        const pct = this.stats?.successRate || 0;
        if (pct >= 95) return 'success';
        if (pct >= 70) return '';
        return 'expired';
    }

    // ── lifecycle ───────────────────────────────────────────────────────────
    connectedCallback() {
        this._loadAll();
        this._startAutoRefresh();
    }

    disconnectedCallback() {
        this._stopAutoRefresh();
    }

    // ── data loading ────────────────────────────────────────────────────────
    async _loadAll() {
        this.isLoading = true;
        this.hasGlobalError = false;
        try {
            await Promise.all([
                this._loadStats(),
                this._loadObjectBreakdown(),
                this._loadBatchRuns(),
                this._loadErrors(),
            ]);
            this.lastRefreshed = new Intl.DateTimeFormat('en-US', {
                hour: '2-digit', minute: '2-digit', second: '2-digit',
            }).format(new Date());
        } catch (err) {
            this.hasGlobalError = true;
            this.globalErrorMessage = err?.body?.message || err?.message || 'Unknown error';
        } finally {
            this.isLoading = false;
        }
    }

    async _loadStats() {
        const data = await getMigrationStats();
        this.stats = data ? { ...data } : {};
    }

    async _loadObjectBreakdown() {
        const data = await getObjectBreakdown();
        if (data) {
            this.objectBreakdown = data.map(obj => ({
                ...obj,
                pct:     obj.total > 0 ? Math.round((obj.migrated / obj.total) * 100) : 0,
                variant: obj.failed > 0 ? 'expired' : '',
            }));
        }
    }

    async _loadBatchRuns() {
        const data = await getRecentBatchRuns({ maxRecords: 8 });
        if (data) {
            this.recentBatchRuns = data.map(run => ({
                ...run,
                statusIcon:    this._statusIcon(run.status),
                statusVariant: this._statusVariant(run.status),
            }));
        }
    }

    async _loadErrors(append = false) {
        const data = await getRecentErrors({
            pageNumber: this._errorPage,
            pageSize:   this._errorPageSize,
        });
        if (data) {
            const rows = data.records || data;
            this.errorRows       = append ? [...this.errorRows, ...rows] : rows;
            this.allErrorsLoaded = rows.length < this._errorPageSize;
        }
    }

    // ── auto-refresh ────────────────────────────────────────────────────────
    _startAutoRefresh() {
        if (this._refreshTimer) return;
        this._refreshTimer = setInterval(() => {
            if (this.autoRefreshEnabled) { this._loadAll(); }
        }, this.autoRefreshInterval * 1000);
    }

    _stopAutoRefresh() {
        if (this._refreshTimer) {
            clearInterval(this._refreshTimer);
            this._refreshTimer = null;
        }
    }

    // ── event handlers ──────────────────────────────────────────────────────
    handleRefresh() {
        this._loadAll();
    }

    handleExportReport() {
        // Build CSV from errorRows
        if (!this.errorRows.length) {
            this.dispatchEvent(new ShowToastEvent({ title: 'No errors to export', variant: 'info' }));
            return;
        }
        const headers = ERROR_COLUMNS.map(c => c.label).join(',');
        const rows    = this.errorRows.map(r =>
            [r.legacyId, r.objectType, r.errorCode, `"${(r.errorMessage||'').replace(/"/g,'""')}"`,
             r.batchId, r.createdAt, r.isRetryable].join(',')
        );
        const blob    = new Blob([[headers, ...rows].join('\n')], { type: 'text/csv' });
        const url     = URL.createObjectURL(blob);
        const anchor  = document.createElement('a');
        anchor.href   = url;
        anchor.download = `migration-errors-${Date.now()}.csv`;
        anchor.click();
        URL.revokeObjectURL(url);
    }

    handleRunBatch() {
        this.showBatchModal = true;
    }

    closeBatchModal() {
        this.showBatchModal = false;
    }

    handleBatchConfigChange(event) {
        const field = event.target.dataset.field;
        const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
        this.batchConfig = { ...this.batchConfig, [field]: value };
    }

    async confirmRunBatch() {
        this.showBatchModal = false;
        this.isBatchRunning = true;
        try {
            const result = await runMigrationBatch({
                objectName: this.batchConfig.objectName,
                batchSize:  parseInt(this.batchConfig.batchSize, 10),
                dryRun:     this.batchConfig.dryRun,
            });
            this.dispatchEvent(new ShowToastEvent({
                title:   'Batch Launched',
                message: `Job ID: ${result}`,
                variant: 'success',
            }));
            setTimeout(() => this._loadAll(), 5000);
        } catch (err) {
            this.dispatchEvent(new ShowToastEvent({
                title:   'Batch Failed',
                message: err?.body?.message || err?.message || 'Unknown error',
                variant: 'error',
                mode:    'sticky',
            }));
        } finally {
            this.isBatchRunning = false;
        }
    }

    toggleAutoRefresh(event) {
        this.autoRefreshEnabled = event.target.checked;
    }

    dismissGlobalError() {
        this.hasGlobalError = false;
    }

    handleErrorSort(event) {
        this.sortedBy        = event.detail.fieldName;
        this.sortedDirection = event.detail.sortDirection;
        this.errorRows = [...this.errorRows].sort((a, b) => {
            const v1 = a[this.sortedBy] || '';
            const v2 = b[this.sortedBy] || '';
            return this.sortedDirection === 'asc'
                ? v1 > v2 ? 1 : -1
                : v1 < v2 ? 1 : -1;
        });
    }

    handleLoadMoreErrors() {
        this._errorPage++;
        this._loadErrors(true);
    }

    // ── helpers ─────────────────────────────────────────────────────────────
    _statusIcon(status) {
        const map = {
            'Success':                'utility:success',
            'Completed with Errors':  'utility:warning',
            'Failed':                 'utility:error',
            'Running':                'utility:spinner',
            'Queued':                 'utility:clock',
            'Aborted':                'utility:ban',
        };
        return map[status] || 'utility:info';
    }

    _statusVariant(status) {
        const map = {
            'Success':               'success',
            'Completed with Errors': 'warning',
            'Failed':                'error',
        };
        return map[status] || '';
    }
}
