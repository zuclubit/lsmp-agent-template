import { LightningElement, api, wire, track } from 'lwc';
import { getRecord, getFieldValue } from 'lightning/uiRecordApi';

const ACCOUNT_FIELDS = [
    'Account.Legacy_ID__c',
    'Account.Migration_Status__c',
    'Account.Source_System__c',
    'Account.Migration_Batch__c',
];

const CONTACT_FIELDS = [
    'Contact.Legacy_ID__c',
    'Contact.Migration_Status__c',
];

const OPPORTUNITY_FIELDS = [
    'Opportunity.Legacy_ID__c',
    'Opportunity.Migration_Status__c',
];

export default class MigrationStatusBadge extends LightningElement {
    @api recordId;
    @api objectApiName;

    @track isLoading  = true;
    @track legacyId   = '—';
    @track migrationStatus = '—';
    @track sourceSystem    = '—';
    @track migrationBatch  = '—';

    get fields() {
        if (this.objectApiName === 'Account')     return ACCOUNT_FIELDS;
        if (this.objectApiName === 'Contact')     return CONTACT_FIELDS;
        if (this.objectApiName === 'Opportunity') return OPPORTUNITY_FIELDS;
        return ACCOUNT_FIELDS;
    }

    @wire(getRecord, { recordId: '$recordId', fields: '$fields' })
    wiredRecord({ data }) {
        this.isLoading = false;
        if (data) {
            this.legacyId        = getFieldValue(data, `${this.objectApiName}.Legacy_ID__c`)    || '—';
            this.migrationStatus = getFieldValue(data, `${this.objectApiName}.Migration_Status__c`) || '—';
            this.sourceSystem    = getFieldValue(data, `${this.objectApiName}.Source_System__c`)  || '—';
            this.migrationBatch  = getFieldValue(data, `${this.objectApiName}.Migration_Batch__c`) || '—';
        }
    }

    get statusClass() {
        const map = {
            'Migrated':    'slds-badge slds-badge_success',
            'Failed':      'slds-badge slds-badge_error',
            'In Progress': 'slds-badge slds-badge_lightest',
            'Pending':     'slds-badge',
        };
        return map[this.migrationStatus] || 'slds-badge';
    }
}
