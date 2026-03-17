/**
 * @description  : Migration Dashboard LWC JavaScript controller.
 *                 Orchestrates apex wire/imperative calls, reactive state management,
 *                 auto-refresh, sorting, error handling, and batch job initiation.
 * @author       : Migration Platform Team
 * @last modified: 2026-03-16
 */
import { LightningElement, track, wire } from 'lwc';
import { ShowToastEvent }                from 'lightning/platformShowToastEvent';
import { refreshApex }                   from '@salesforce/apex';

// Apex imports
import getMigrationStats     from '@salesforce/apex/MigrationDashboardController.getMigrationStats';
import getObjectBreakdown    from '@salesforce/apex/MigrationDashboardController.getObjectBreakdown';
import getRecentErrors       from '@salesforce/apex/MigrationDashboardController.getRecentErrors';
import getRecentBatchRuns    from '@salesforce/apex/MigrationDashboardController.getRecentBatchRuns';
import runMigrationBatch     from '@salesforce/apex/MigrationDashboardController.runMigrationBatch';
import exportReconciliation  from '@salesforce/apex/MigrationDashboardController.exportReconciliationReport';

// Static labels / constants
const AUTO_REFRESH_INTERVAL_SEC = 30;
const ERROR_PAGE_SIZE           = 50;

const ERROR_COLUMNS = [
    { label: 'Timestamp',   fieldName: 'timestamp',   type: 'date',
      typeAttributes: { year:'numeric', month:'short', day:'2-digit',
                        hour:'2-digit', minute:'2-digit', second:'2-digit' },
      sortable: true },
    { label: 'Source',      fieldName: 'source',      type: 'text',  sortable: true },
    { label: 'Action',      fieldName: 'action',      type: 'text',  sortable: true },
    { label: 'Message',     fieldName: 'message',     type: 'text',  wrapText: true },
    { label: 'Record Id',   fieldName: 'recordUrl',   type: 'url',
      typeAttributes: { label: { fieldName: 'recordIdShort' }, target: '_blank' } },
    { label: 'User',        fieldName: 'userName',    type: 'text' }
];

const OBJECT_OPTIONS = [
    { label: 'Account',    value: 'Account' },
    { label: 'Contact',    value: 'Contact' },
    { label: 'Opportunity',value: 'Opportunity' },
    { label: 'Lead',       value: 'Lead' },
    { label: 'Case',       value: 'Case' }
];

export default class MigrationDashboard extends LightningElement {

    // ─── Reactive state ───────────────────────────────────────────────────────
    @track stats                = {};
    @track objectBreakdown      = [];
    @track errorRows            = [];
    @track recentBatchRuns      = [];
    @track isLoading            = false;
    @track isBatchRunning       = false;
    @track hasGlobalError       = false;
    @track globalErrorMessage   = '';
    @track showBatchModal       = false;
    @track autoRefreshEnabled   = true;
    @track allErrorsLoaded      = false;
    @track sortedBy             = 'timestamp';
    @track sortedDirection      = 'desc';
    @track batchConfig          = { objectName: 'Account', batchSize: 200, dryRun: false };

    // Wire result holders for refreshApex
    _wiredStatsResult;
    _wiredBreakdownResult;
    _wiredErrorsResult;
    _wiredBatchRunsResult;

    // Internal
    _autoRefreshTimer   = null;
    _errorOffset        = 0;
    _lastRefreshedTs    = null;

    // ─── Computed properties ──────────────────────────────────────────────────
    get lastRefreshed() {
        return this._lastRefreshedTs
            ? new Intl.DateTimeFormat('en-US', {
                hour: '2-digit', minute: '2-digit', second: '2-digit'
              }).format(this._lastRefreshedTs)
            : 'Never';
    }

    get autoRefreshInterval() { return AUTO_REFRESH_INTERVAL_SEC; }
    get errorColumns()        { return ERROR_COLUMNS; }
    get objectOptions()       { return OBJECT_OPTIONS; }
    get rowNumberOffset()     { return 0; }

    get hasObjectBreakdown()  { return this.objectBreakdown && this.objectBreakdown.length > 0; }
    get hasBatchRuns()        { return this.recentBatchRuns && this.recentBatchRuns.length > 0; }
    get hasErrors()           { return this.errorRows && this.errorRows.length > 0; }

    get errorCountLabel() {
        const c = this.errorRows ? this.errorRows.length : 0;
        return c > 0 ? `${c} errors` : 'No errors';
    }

    get runBatchLabel() {
        return this.isBatchRunning ? 'Batch Running...' : 'Run Batch';
    }

    get progressVariant() {
        const rate = this.stats.successRate || 0;
        if (rate >= 95) return 'success';
        if (rate >= 75) return 'base';
        return 'expired';
    }

    // ─── Wire adapters ────────────────────────────────────────────────────────
    @wire(getMigrationStats)
    wiredStats(result) {
        this._wiredStatsResult = result;
        if (result.data) {
            this.stats = this._processStats(result.data);
        } else if (result.error) {
            this._handleError('Failed to load migration statistics', result.error);
        }
    }

    @wire(getObjectBreakdown)
    wiredBreakdown(result) {
        this._wiredBreakdownResult = result;
        if (result.data) {
            this.objectBreakdown = this._processObjectBreakdown(result.data);
        } else if (result.error) {
            this._handleError('Failed to load object breakdown', result.error);
        }
    }

    @wire(getRecentBatchRuns)
    wiredBatchRuns(result) {
        this._wiredBatchRunsResult = result;
        if (result.data) {
            this.recentBatchRuns = this._processBatchRuns(result.data);
        } else if (result.error) {
            this._handleError('Failed to load batch run history', result.error);
        }
    }

    // ─── Lifecycle hooks ──────────────────────────────────────────────────────
    connectedCallback() {
        this._loadErrors();
        if (this.autoRefreshEnabled) {
            this._startAutoRefresh();
        }
    }

    disconnectedCallback() {
        this._stopAutoRefresh();
    }

    // ─── Event handlers ───────────────────────────────────────────────────────
    handleRefresh() {
        this._refreshAll();
    }

    handleExportReport() {
        this.isLoading = true;
        exportReconciliation()
            .then(result => {
                this._showToast('Report Generated',
                    'Reconciliation report is ready: ' + result, 'success');
            })
            .catch(error => {
                this._handleError('Failed to generate report', error);
            })
            .finally(() => {
                this.isLoading = false;
            });
    }

    handleRunBatch() {
        this.showBatchModal = true;
    }

    closeBatchModal() {
        this.showBatchModal = false;
    }

    handleBatchConfigChange(event) {
        const field = event.target.dataset.field;
        const value = event.target.type === 'toggle'
            ? event.target.checked : event.target.value;
        this.batchConfig = { ...this.batchConfig, [field]: value };
    }

    confirmRunBatch() {
        this.showBatchModal  = false;
        this.isBatchRunning  = true;

        runMigrationBatch({
            objectName: this.batchConfig.objectName,
            batchSize:  parseInt(this.batchConfig.batchSize, 10),
            dryRun:     this.batchConfig.dryRun
        })
        .then(jobId => {
            this._showToast(
                'Batch Launched',
                `Migration batch started. Job ID: ${jobId}`,
                'success'
            );
            // Refresh data after a short delay to pick up the new batch run
            setTimeout(() => { this._refreshAll(); }, 5000);
        })
        .catch(error => {
            this._handleError('Failed to launch migration batch', error);
        })
        .finally(() => {
            this.isBatchRunning = false;
        });
    }

    handleErrorSort(event) {
        this.sortedBy        = event.detail.fieldName;
        this.sortedDirection = event.detail.sortDirection;
        this.errorRows       = this._sortData(
            [...this.errorRows], this.sortedBy, this.sortedDirection);
    }

    handleLoadMoreErrors() {
        this._errorOffset += ERROR_PAGE_SIZE;
        this._loadErrors(true);
    }

    dismissGlobalError() {
        this.hasGlobalError     = false;
        this.globalErrorMessage = '';
    }

    toggleAutoRefresh(event) {
        this.autoRefreshEnabled = event.target.checked;
        if (this.autoRefreshEnabled) {
            this._startAutoRefresh();
            this._showToast('Auto-Refresh', 'Auto-refresh enabled.', 'info');
        } else {
            this._stopAutoRefresh();
            this._showToast('Auto-Refresh', 'Auto-refresh disabled.', 'warning');
        }
    }

    // ─── Private methods ──────────────────────────────────────────────────────
    _refreshAll() {
        this.isLoading = true;
        Promise.all([
            refreshApex(this._wiredStatsResult),
            refreshApex(this._wiredBreakdownResult),
            refreshApex(this._wiredBatchRunsResult)
        ])
        .then(() => {
            this._errorOffset = 0;
            return this._loadErrors();
        })
        .catch(err => {
            this._handleError('Refresh failed', err);
        })
        .finally(() => {
            this.isLoading       = false;
            this._lastRefreshedTs = new Date();
        });
    }

    _loadErrors(append = false) {
        return getRecentErrors({
            offsetCount: this._errorOffset,
            limitCount:  ERROR_PAGE_SIZE
        })
        .then(data => {
            const processed = this._processErrorRows(data);
            if (append) {
                this.errorRows = [...this.errorRows, ...processed];
            } else {
                this.errorRows = processed;
            }
            this.allErrorsLoaded = processed.length < ERROR_PAGE_SIZE;
            this._lastRefreshedTs = new Date();
        })
        .catch(error => {
            this._handleError('Failed to load errors', error);
        });
    }

    _startAutoRefresh() {
        this._stopAutoRefresh();
        this._autoRefreshTimer = setInterval(() => {
            this._refreshAll();
        }, AUTO_REFRESH_INTERVAL_SEC * 1000);
    }

    _stopAutoRefresh() {
        if (this._autoRefreshTimer) {
            clearInterval(this._autoRefreshTimer);
            this._autoRefreshTimer = null;
        }
    }

    // ─── Data transformation helpers ──────────────────────────────────────────
    _processStats(raw) {
        if (!raw) return {};
        const total    = raw.totalRecords   || 0;
        const migrated = raw.migratedRecords || 0;
        const failed   = raw.failedRecords  || 0;
        const pending  = raw.pendingRecords  || 0;
        const inProg   = raw.inProgressRecords || 0;

        return {
            totalRecords:        total,
            migratedRecords:     migrated,
            failedRecords:       failed,
            pendingRecords:      pending,
            inProgressRecords:   inProg,
            successRate:         total > 0 ? ((migrated / total) * 100).toFixed(1) : '0.0',
            failureRate:         total > 0 ? ((failed   / total) * 100).toFixed(1) : '0.0',
            estimatedCompletion: raw.estimatedCompletion || 'Calculating...'
        };
    }

    _processObjectBreakdown(data) {
        if (!data || !Array.isArray(data)) return [];
        return data.map(obj => {
            const pct     = obj.total > 0 ? Math.round((obj.migrated / obj.total) * 100) : 0;
            const variant = pct >= 100 ? 'success' : pct >= 50 ? 'base' : 'expired';
            return { ...obj, pct, variant };
        });
    }

    _processBatchRuns(data) {
        if (!data || !Array.isArray(data)) return [];
        return data.map(run => ({
            id:            run.id,
            label:         run.label || run.Name,
            processed:     run.processed || run.Total_Processed__c || 0,
            failed:        run.failed    || run.Total_Failed__c    || 0,
            duration:      run.duration  || 'N/A',
            statusIcon:    run.status === 'Success'
                               ? 'utility:success' : 'utility:error',
            statusVariant: run.status === 'Success' ? 'success' : 'error'
        }));
    }

    _processErrorRows(data) {
        if (!data || !Array.isArray(data)) return [];
        return data.map(err => ({
            id:           err.Id,
            timestamp:    err.Timestamp__c,
            source:       err.Source__c,
            action:       err.Action__c,
            message:      err.Message__c,
            userName:     err.User_Name__c,
            recordIdShort:err.Related_Record_Id__c
                ? err.Related_Record_Id__c.substring(0, 15) : '',
            recordUrl:    err.Related_Record_Id__c
                ? `/lightning/r/${err.Related_Record_Id__c}/view` : null
        }));
    }

    _sortData(data, fieldName, direction) {
        const multiplier = direction === 'asc' ? 1 : -1;
        return data.sort((a, b) => {
            const av = a[fieldName] || '';
            const bv = b[fieldName] || '';
            if (av < bv) return -1 * multiplier;
            if (av > bv) return  1 * multiplier;
            return 0;
        });
    }

    _handleError(context, error) {
        const msg = error && error.body
            ? error.body.message
            : (error && error.message ? error.message : 'Unknown error');
        this.hasGlobalError     = true;
        this.globalErrorMessage = `${context}: ${msg}`;
        console.error('[MigrationDashboard]', context, error);
    }

    _showToast(title, message, variant) {
        this.dispatchEvent(new ShowToastEvent({ title, message, variant }));
    }
}
